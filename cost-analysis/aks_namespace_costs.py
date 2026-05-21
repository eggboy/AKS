#!/usr/bin/env python3
"""
AKS Namespace-Level Cost Analysis

Uses the Generate Cost Details Report API to get namespace-level costs.
The AKS cost analysis add-on must be enabled on your cluster(s).

Usage:
    export SUBSCRIPTION_ID=<your-sub-id>
    python3 aks_namespace_costs.py

    # With custom date range:
    python3 aks_namespace_costs.py --start 2026-04-01 --end 2026-04-22

    # Filter to a specific cluster:
    python3 aks_namespace_costs.py --cluster my-aks-cluster
"""

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta

MAX_POLL_ATTEMPTS = 30
INITIAL_RETRY_SECONDS = 20
RETRY_SECONDS = 10
SUBPROCESS_TIMEOUT = 120
DEBUG_TRUNCATE_LEN = 200
ERROR_TRUNCATE_LEN = 500


class CostReportError(Exception):
    """Raised when cost report generation or retrieval fails."""


def az_rest(method: str, url: str, body_file: str | None = None) -> dict:
    """Execute an Azure CLI REST call and return parsed JSON.

    Args:
        method: HTTP method (get, post, etc.).
        url: Full Azure Management API URL.
        body_file: Optional path to a JSON file for the request body.

    Raises:
        CostReportError: If the az CLI command fails.
    """
    cmd = ["az", "rest", "--method", method, "--url", url, "-o", "json"]
    if body_file:
        cmd += ["--headers", "Content-Type=application/json", "--body", f"@{body_file}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
    if result.returncode != 0:
        raise CostReportError(f"az rest failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _extract_blob_url(data: dict) -> str | None:
    """Extract download URL from cost details response (manifest format)."""
    # Format: manifest.blobs[].blobLink
    try:
        blobs = data.get("manifest", {}).get("blobs", [])
        if blobs:
            return blobs[0]["blobLink"]
    except (KeyError, IndexError, AttributeError):
        pass
    # Fallback: properties.downloadUrl (older API versions)
    try:
        return data["properties"]["downloadUrl"]
    except (KeyError, TypeError):
        return None


def _extract_polling_url(stderr: str) -> str | None:
    """Extract the costDetailsOperationResults polling URL from az CLI verbose output."""
    for line in stderr.splitlines():
        if "'Location'" in line or "'location'" in line:
            parts = line.split("'")
            for p in parts:
                if "management.azure.com" in p and "costDetailsOperationResults" in p:
                    return p.strip()

    match = re.search(
        r"(https://management\.azure\.com/[^\s'\"]+costDetailsOperationResults[^\s'\"]+)",
        stderr,
    )
    return match.group(1) if match else None


def _dump_debug_info(result: subprocess.CompletedProcess) -> None:
    """Print debug information when polling URL extraction fails."""
    print("Could not get operation URL. Dumping stderr for debug:")
    for line in result.stderr.splitlines():
        if "ocation" in line or "costDetail" in line:
            print(f"  {line[:DEBUG_TRUNCATE_LEN]}")
    print(f"\nstdout ({len(result.stdout)} bytes): {result.stdout[:DEBUG_TRUNCATE_LEN]}")
    print(f"stderr lines: {len(result.stderr.splitlines())}")


def _poll_for_report(location: str) -> str:
    """Poll the operation URL until the cost details report is ready.

    Raises:
        CostReportError: If the report generation fails or times out.
    """
    for attempt in range(MAX_POLL_ATTEMPTS):
        retry_after = INITIAL_RETRY_SECONDS if attempt < 3 else RETRY_SECONDS
        print(f"  Polling... (attempt {attempt + 1}, wait {retry_after}s)")
        time.sleep(retry_after)
        poll_result = subprocess.run(
            ["az", "rest", "--method", "get", "--url", location, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        try:
            poll_data = json.loads(poll_result.stdout)
            blob_url = _extract_blob_url(poll_data)
            if blob_url:
                return blob_url
            if poll_data.get("status") == "Failed":
                raise CostReportError(
                    f"Report generation failed: {json.dumps(poll_data, indent=2)[:ERROR_TRUNCATE_LEN]}"
                )
        except (json.JSONDecodeError, ValueError):
            continue

    raise CostReportError("Timed out waiting for cost details report")


def generate_cost_details(sub_id: str, start: str, end: str) -> str:
    """Request a cost details report and return the download URL.

    Args:
        sub_id: Azure subscription ID.
        start: Start date in YYYY-MM-DD format.
        end: End date in YYYY-MM-DD format.

    Returns:
        Blob download URL for the generated CSV report.

    Raises:
        CostReportError: If report generation fails or times out.
    """
    body = {
        "metric": "ActualCost",
        "timePeriod": {
            "start": f"{start}T00:00:00Z",
            "end": f"{end}T23:59:59Z",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        body_path = f.name

    try:
        url = (
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/providers/Microsoft.CostManagement/generateCostDetailsReport"
            f"?api-version=2024-08-01"
        )

        print(f"Requesting cost details report for {start} → {end} ...")

        cmd = [
            "az", "rest",
            "--method", "post",
            "--url", url,
            "--headers", "Content-Type=application/json",
            "--body", f"@{body_path}",
            "-o", "json",
            "--verbose",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)

        try:
            data = json.loads(result.stdout)
            blob_url = _extract_blob_url(data)
            if blob_url:
                return blob_url
        except (json.JSONDecodeError, ValueError):
            pass

        location = _extract_polling_url(result.stderr)

        if not location:
            _dump_debug_info(result)
            raise CostReportError("Could not extract operation polling URL from az CLI output")

        return _poll_for_report(location)
    finally:
        os.unlink(body_path)


def _fallback_infra_costs(raw_csv: str, cluster_filter: str | None = None) -> dict[str, dict[str, float]]:
    """Show AKS infrastructure costs grouped by cluster resource + meter.

    Used when Kubernetes namespace columns aren't yet available in the billing data.
    """
    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(raw_csv))

    col_map = {}
    for col in reader.fieldnames or []:
        col_map[col.lower().lstrip("\ufeff")] = col

    rid_col = col_map.get("resourceid", "ResourceId")
    cat_col = col_map.get("metercategory", "meterCategory")
    meter_col = col_map.get("metername", "meterName")
    cost_col = col_map.get("costinbillingcurrency", "costInBillingCurrency")
    svc_col = col_map.get("consumedservice", "consumedService")

    for row in reader:
        svc = (row.get(svc_col, "") or "").lower()
        cat = (row.get(cat_col, "") or "").lower()
        rid = row.get(rid_col, "") or ""

        is_aks = "kubernetes" in svc or "kubernetes" in cat or "containerservice" in svc
        if cluster_filter:
            is_aks = is_aks or cluster_filter.lower() in rid.lower()

        if not is_aks:
            continue

        cluster_name = rid.rsplit("/", 1)[-1]
        meter = f"{row.get(cat_col, '')} / {row.get(meter_col, '')}"
        cost = float(row.get(cost_col, 0) or 0)
        costs[cluster_name][meter] += cost

    return costs


def download_and_parse(url: str, cluster_filter: str | None = None) -> dict[str, dict[str, float]]:
    """Download the cost details CSV and aggregate by cluster + namespace.

    Args:
        url: Blob download URL for the CSV report.
        cluster_filter: Optional cluster name substring to filter results.

    Returns:
        Nested dict mapping cluster → namespace/meter → total cost.
    """
    print("Downloading cost details CSV...")
    with urllib.request.urlopen(url) as response:  # noqa: S310
        raw_csv = response.read().decode("utf-8")

    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(raw_csv))

    col_map = {}
    for col in reader.fieldnames or []:
        col_map[col.lower().lstrip("\ufeff")] = col

    k8s_cluster_col = col_map.get("x_kubernetesclustername") or col_map.get("kubernetesclustername")
    k8s_ns_col = col_map.get("x_kubernetesnamespace") or col_map.get("kubernetesnamespace")
    cost_col = col_map.get("costinbillingcurrency")

    if not k8s_cluster_col or not k8s_ns_col:
        print("\n⚠  Kubernetes namespace columns not found in cost details CSV yet.")
        print("   The add-on may need 24-48h to populate billing data.")
        print("   Falling back to AKS infrastructure-level cost breakdown...\n")
        return _fallback_infra_costs(raw_csv, cluster_filter)

    print(f"  Found K8s columns: cluster={k8s_cluster_col}, namespace={k8s_ns_col}")

    for row in reader:
        cluster = row.get(k8s_cluster_col, "")
        namespace = row.get(k8s_ns_col, "")
        cost = float(row.get(cost_col, 0) or 0)

        if not cluster or not namespace:
            continue
        if cluster_filter and cluster_filter.lower() not in cluster.lower():
            continue

        costs[cluster][namespace] += cost

    return costs


def print_report(costs: dict[str, dict[str, float]], namespace_mode: bool = True) -> None:
    """Print a formatted cost breakdown table to stdout."""
    label = "Namespace" if namespace_mode else "Meter"
    print(f"\n{'='*74}")
    print(f"{'Cluster/Resource':<30} {label:<30} {'Cost':>12}")
    print(f"{'='*74}")

    grand_total = 0.0
    for cluster in sorted(costs):
        cluster_total = sum(costs[cluster].values())
        grand_total += cluster_total
        for ns in sorted(costs[cluster], key=lambda x: costs[cluster][x], reverse=True):
            ns_cost = costs[cluster][ns]
            print(f"{cluster:<30} {ns:<30} ${ns_cost:>10,.2f}")
        print(f"{'':30} {'── cluster total ──':<30} ${cluster_total:>10,.2f}")
        print()

    print(f"{'='*74}")
    print(f"{'':30} {'GRAND TOTAL':<30} ${grand_total:>10,.2f}")


def main() -> None:
    """CLI entry point for AKS namespace-level cost analysis."""
    parser = argparse.ArgumentParser(description="AKS namespace-level cost analysis")
    parser.add_argument("--start", default=(datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    parser.add_argument("--cluster", help="Filter to a specific cluster name (contains match)")
    parser.add_argument("--subscription", default=os.environ.get("SUBSCRIPTION_ID"))
    args = parser.parse_args()

    if not args.subscription:
        print("Set SUBSCRIPTION_ID env var or use --subscription", file=sys.stderr)
        sys.exit(1)

    try:
        download_url = generate_cost_details(args.subscription, args.start, args.end)
    except CostReportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Report ready. Downloading...")
    costs = download_and_parse(download_url, args.cluster)

    if not costs:
        print("\nNo Kubernetes cost data found.")
        print("Ensure the AKS cost analysis add-on is enabled and data has been collected (up to 48h).")
        sys.exit(0)

    # Detect if we got namespace-level data or fallback infra data
    first_inner = next(iter(costs.values()))
    sample_keys = list(first_inner.keys())
    namespace_mode = not any("/" in k for k in sample_keys)
    print_report(costs, namespace_mode=namespace_mode)


if __name__ == "__main__":
    main()

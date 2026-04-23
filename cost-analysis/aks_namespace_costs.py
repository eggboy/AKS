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
import json
import subprocess
import sys
import time
import os
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def az_rest(method: str, url: str, body_file: str | None = None) -> dict:
    cmd = ["az", "rest", "--method", method, "--url", url, "-o", "json"]
    if body_file:
        cmd += ["--headers", "Content-Type=application/json", "--body", f"@{body_file}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _extract_blob_url(data: dict) -> str | None:
    """Extract download URL from cost details response (manifest format)."""
    # Format: manifest.blobs[].blobLink
    try:
        blobs = data.get("manifest", {}).get("blobs", [])
        if blobs:
            return blobs[0]["blobLink"]
    except (KeyError, IndexError):
        pass
    # Fallback: properties.downloadUrl (older API versions)
    try:
        return data["properties"]["downloadUrl"]
    except (KeyError, TypeError):
        return None


def generate_cost_details(sub_id: str, start: str, end: str) -> str:
    """Request a cost details report and return the download URL."""
    body = {
        "metric": "ActualCost",
        "timePeriod": {
            "start": f"{start}T00:00:00Z",
            "end": f"{end}T23:59:59Z",
        },
    }
    body_path = "/tmp/cost_details_body.json"
    with open(body_path, "w") as f:
        json.dump(body, f)

    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        f"/providers/Microsoft.CostManagement/generateCostDetailsReport"
        f"?api-version=2024-08-01"
    )

    print(f"Requesting cost details report for {start} → {end} ...")

    # POST returns 202 with Location header; use --verbose to capture it
    cmd = [
        "az", "rest", "--method", "post", "--url", url,
        "--headers", "Content-Type=application/json",
        "--body", f"@{body_path}",
        "-o", "json", "--verbose",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # If response body has data immediately, extract blob URL
    try:
        data = json.loads(result.stdout)
        blob_url = _extract_blob_url(data)
        if blob_url:
            return blob_url
    except (json.JSONDecodeError, ValueError):
        pass

    # Extract the Location header from verbose stderr for polling
    location = None
    for line in result.stderr.splitlines():
        if "'Location'" in line or "'location'" in line:
            parts = line.split("'")
            for p in parts:
                if "management.azure.com" in p and "costDetailsOperationResults" in p:
                    location = p.strip()
                    break
    # Fallback: look for any costDetailsOperationResults URL in stderr
    if not location:
        import re
        match = re.search(r"(https://management\.azure\.com/[^\s'\"]+costDetailsOperationResults[^\s'\"]+)", result.stderr)
        if match:
            location = match.group(1)

    if not location:
        print("Could not get operation URL. Dumping stderr for debug:")
        # Print lines containing 'Location' or 'costDetails'
        for line in result.stderr.splitlines():
            if "ocation" in line or "costDetail" in line:
                print(f"  {line[:200]}")
        print(f"\nstdout ({len(result.stdout)} bytes): {result.stdout[:200]}")
        print(f"stderr lines: {len(result.stderr.splitlines())}")
        sys.exit(1)

    # Poll until the report is ready
    for attempt in range(30):
        retry_after = 20 if attempt < 3 else 10
        print(f"  Polling... (attempt {attempt + 1}, wait {retry_after}s)")
        time.sleep(retry_after)
        poll_result = subprocess.run(
            ["az", "rest", "--method", "get", "--url", location, "-o", "json"],
            capture_output=True, text=True,
        )
        try:
            poll_data = json.loads(poll_result.stdout)
            blob_url = _extract_blob_url(poll_data)
            if blob_url:
                return blob_url
            status = poll_data.get("status", "")
            if status == "Failed":
                print(f"Report generation failed: {json.dumps(poll_data, indent=2)[:500]}", file=sys.stderr)
                sys.exit(1)
        except (json.JSONDecodeError, ValueError):
            continue

    print("Timed out waiting for report.", file=sys.stderr)
    sys.exit(1)


def _fallback_infra_costs(reader_unused, raw_csv: str, cluster_filter: str | None = None):
    """Show AKS infrastructure costs grouped by cluster resource + meter when K8s columns aren't available yet."""
    costs = defaultdict(lambda: defaultdict(float))
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
        # Also match resources under the AKS node resource group
        if cluster_filter:
            is_aks = is_aks or cluster_filter.lower() in rid.lower()

        if not is_aks:
            continue

        cluster_name = rid.rsplit("/", 1)[-1] if "managedclusters" in rid.lower() else rid.rsplit("/", 1)[-1]
        meter = f"{row.get(cat_col, '')} / {row.get(meter_col, '')}"
        cost = float(row.get(cost_col, 0) or 0)
        costs[cluster_name][meter] += cost

    return costs


def download_and_parse(url: str, cluster_filter: str | None = None):
    """Download the CSV and aggregate by cluster + namespace."""
    print("Downloading cost details CSV...")
    result = subprocess.run(["curl", "-sL", url], capture_output=True, text=True)

    costs = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(result.stdout))

    k8s_cluster_col = None
    k8s_ns_col = None

    # Build a case-insensitive column map
    col_map = {}
    for col in reader.fieldnames or []:
        col_map[col.lower().lstrip("\ufeff")] = col

    # Find K8s columns and cost column (names vary by case/BOM)
    k8s_cluster_col = col_map.get("x_kubernetesclustername") or col_map.get("kubernetesclustername")
    k8s_ns_col = col_map.get("x_kubernetesnamespace") or col_map.get("kubernetesnamespace")
    cost_col = col_map.get("costinbillingcurrency")

    if not k8s_cluster_col or not k8s_ns_col:
        print("\n⚠  Kubernetes namespace columns not found in cost details CSV yet.")
        print("   The add-on may need 24-48h to populate billing data.")
        print("   Falling back to AKS infrastructure-level cost breakdown...\n")
        return _fallback_infra_costs(reader, result.stdout, cluster_filter)

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


def print_report(costs: dict, namespace_mode: bool = True):
    label = "Namespace" if namespace_mode else "Meter"
    print(f"\n{'='*74}")
    print(f"{'Cluster/Resource':<30} {label:<30} {'Cost':>12}")
    print(f"{'='*74}")

    grand_total = 0.0
    for cluster in sorted(costs):
        cluster_total = sum(costs[cluster].values())
        grand_total += cluster_total
        for ns in sorted(costs[cluster], key=lambda x: costs[cluster][x], reverse=True):
            c = costs[cluster][ns]
            print(f"{cluster:<30} {ns:<30} ${c:>10,.2f}")
        print(f"{'':30} {'── cluster total ──':<30} ${cluster_total:>10,.2f}")
        print()

    print(f"{'='*74}")
    print(f"{'':30} {'GRAND TOTAL':<30} ${grand_total:>10,.2f}")


def main():
    parser = argparse.ArgumentParser(description="AKS namespace-level cost analysis")
    parser.add_argument("--start", default=(datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    parser.add_argument("--cluster", help="Filter to a specific cluster name (contains match)")
    parser.add_argument("--subscription", default=os.environ.get("SUBSCRIPTION_ID"))
    args = parser.parse_args()

    if not args.subscription:
        print("Set SUBSCRIPTION_ID env var or use --subscription", file=sys.stderr)
        sys.exit(1)

    download_url = generate_cost_details(args.subscription, args.start, args.end)
    print(f"Report ready. Downloading...")
    costs = download_and_parse(download_url, args.cluster)

    if not costs:
        print("\nNo Kubernetes cost data found.")
        print("Ensure the AKS cost analysis add-on is enabled and data has been collected (up to 48h).")
        sys.exit(0)

    # Detect if we got namespace-level data or fallback infra data
    sample_keys = list(list(costs.values())[0].keys()) if costs else []
    namespace_mode = not any("/" in k for k in sample_keys)
    print_report(costs, namespace_mode=namespace_mode)


if __name__ == "__main__":
    main()

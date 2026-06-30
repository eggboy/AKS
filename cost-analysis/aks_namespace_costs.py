#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
AKS Namespace-Level Cost Analysis

Primary path: the Cost Management *query* API (``provider=Microsoft.ContainerService``),
the same invoice-reconciled source the Azure portal "Kubernetes namespaces" view uses.
It returns real per-namespace costs (including the portal's idle/system/service/
unallocated buckets) for any date range, so it works for monthly reports.

Fallback path (``--mode infra``, or automatic when no namespace data is returned):
the Generate Cost Details Report API, which has no namespace columns, so costs are
attributed at the cluster level via node-resource-group mapping.

Both paths require the AKS cost analysis add-on enabled on your cluster(s); the
namespace view additionally requires an EA or MCA subscription.

Usage:
    export SUBSCRIPTION_ID=<your-sub-id>

    # Supply an ARM bearer token (no Azure CLI dependency at runtime). The token's
    # audience must be https://management.azure.com. Generate one however you like;
    # for local use the Azure CLI is the easiest:
    export AZURE_ACCESS_TOKEN=$(az account get-access-token \
        --resource https://management.azure.com --query accessToken -o tsv)

    # The file is a PEP 723 single-file script (stdlib only), so it runs
    # standalone with no project or virtualenv:
    uv run aks_namespace_costs.py

    # ...or with plain Python:
    python3 aks_namespace_costs.py

    # With custom date range (great for a monthly report):
    uv run aks_namespace_costs.py --start 2026-06-01 --end 2026-06-30

    # Filter to a specific cluster:
    uv run aks_namespace_costs.py --cluster my-aks-cluster

    # Force cluster-level infrastructure attribution instead of namespaces:
    uv run aks_namespace_costs.py --mode infra

    # The token may also be passed explicitly:
    uv run aks_namespace_costs.py --token "$AZURE_ACCESS_TOKEN"
"""

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta

MAX_POLL_ATTEMPTS = 30
INITIAL_RETRY_SECONDS = 20
RETRY_SECONDS = 10
HTTP_TIMEOUT = 120
ERROR_TRUNCATE_LEN = 500
MANAGED_CLUSTERS_API_VERSION = "2024-09-01"
MANAGED_CLUSTER_MARKER = "/managedclusters/"

# Azure Resource Manager endpoint and the env var the script reads the bearer
# token from. The token's audience/resource must be the ARM endpoint below.
ARM_ENDPOINT = "https://management.azure.com"
ACCESS_TOKEN_ENV = "AZURE_ACCESS_TOKEN"  # noqa: S105 (env-var name, not a secret)

# Preview Query API that exposes the Kubernetes-specific grouping dimensions
# (Namespace/Cluster/ServiceCategory). This is the exact api-version the Azure
# portal "Kubernetes namespaces" view calls; the stable versions reject the
# grouping. Preview => undocumented and subject to change, but it tracks the
# portal feature.
COST_QUERY_API_VERSION = "2023-04-01-preview"
KUBERNETES_PROVIDER = "Microsoft.ContainerService"
MAX_QUERY_PAGES = 50


class CostReportError(Exception):
    """Raised when cost report generation or retrieval fails."""


def arm_request(
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
) -> tuple[int, dict[str, str], dict]:
    """Make an authenticated Azure Resource Manager REST call.

    Pure HTTP via ``urllib`` with an ``Authorization: Bearer`` header — no Azure
    CLI or SDK dependency.

    Args:
        method: HTTP method (get, post, ...).
        url: Full Azure Resource Manager URL.
        token: OAuth2 bearer token for the ARM audience.
        body: Optional JSON request body.

    Returns:
        Tuple of ``(status_code, lowercased_response_headers, parsed_json_body)``.
        The body is an empty dict when the response has no content.

    Raises:
        CostReportError: If the request fails or returns a non-2xx status.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method.upper())  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
            status = response.status
            headers = {k.lower(): v for k, v in response.headers.items()}
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:ERROR_TRUNCATE_LEN]
        raise CostReportError(f"ARM {method.upper()} {url} failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise CostReportError(f"ARM {method.upper()} {url} failed: {exc.reason}") from exc

    parsed = json.loads(raw) if raw.strip() else {}
    return status, headers, parsed


def arm_json(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """Make an ARM REST call and return only the parsed JSON body.

    Raises:
        CostReportError: If the request fails or returns a non-2xx status.
    """
    _, _, parsed = arm_request(method, url, token, body)
    return parsed


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


def _poll_for_report(location: str, token: str) -> str:
    """Poll the operation URL until the cost details report is ready.

    Raises:
        CostReportError: If the report generation fails or times out.
    """
    for attempt in range(MAX_POLL_ATTEMPTS):
        retry_after = INITIAL_RETRY_SECONDS if attempt < 3 else RETRY_SECONDS
        print(f"  Polling... (attempt {attempt + 1}, wait {retry_after}s)")
        time.sleep(retry_after)
        _, _, poll_data = arm_request("get", location, token)
        blob_url = _extract_blob_url(poll_data)
        if blob_url:
            return blob_url
        if poll_data.get("status") == "Failed":
            raise CostReportError(f"Report generation failed: {json.dumps(poll_data, indent=2)[:ERROR_TRUNCATE_LEN]}")

    raise CostReportError("Timed out waiting for cost details report")


def generate_cost_details(sub_id: str, start: str, end: str, token: str) -> str:
    """Request a cost details report and return the download URL.

    The ``generateCostDetailsReport`` API is asynchronous: the initial POST
    returns either HTTP 200 with the manifest, or HTTP 202 with a ``Location``
    header pointing at an operation-results URL to poll.

    Args:
        sub_id: Azure subscription ID.
        start: Start date in YYYY-MM-DD format.
        end: End date in YYYY-MM-DD format.
        token: ARM bearer token.

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
    url = (
        f"{ARM_ENDPOINT}/subscriptions/{sub_id}"
        f"/providers/Microsoft.CostManagement/generateCostDetailsReport"
        f"?api-version=2024-08-01"
    )

    print(f"Requesting cost details report for {start} → {end} ...")
    status, headers, data = arm_request("post", url, token, body)

    blob_url = _extract_blob_url(data)
    if blob_url:
        return blob_url

    location = headers.get("location")
    if status == 202 and location:
        return _poll_for_report(location, token)

    raise CostReportError(f"Unexpected cost details response (HTTP {status}); no manifest or Location header")


def _build_node_rg_map(clusters_data: dict) -> dict[str, str]:
    """Map lowercased AKS node resource group name -> cluster name.

    The node resource group (``MC_<rg>_<cluster>_<region>``) holds the cluster's
    VMs, disks, load balancers, and public IPs. Those resources are billed under
    ``Microsoft.Compute``/``Microsoft.Network`` rather than
    ``Microsoft.ContainerService``, so this map is what lets us attribute that
    spend back to the owning cluster.
    """
    mapping: dict[str, str] = {}
    for cluster in clusters_data.get("value", []) or []:
        name = cluster.get("name")
        node_rg = (cluster.get("properties") or {}).get("nodeResourceGroup")
        if name and node_rg:
            mapping[node_rg.lower()] = name
    return mapping


def fetch_cluster_node_rg_map(sub_id: str, token: str) -> dict[str, str]:
    """List managed clusters and return their node-resource-group -> name map.

    Returns an empty map (best-effort attribution) if the clusters can't be
    listed, so cost reporting still works without the mapping.
    """
    url = (
        f"{ARM_ENDPOINT}/subscriptions/{sub_id}"
        f"/providers/Microsoft.ContainerService/managedClusters"
        f"?api-version={MANAGED_CLUSTERS_API_VERSION}"
    )
    try:
        data = arm_json("get", url, token)
    except CostReportError as exc:
        print(f"  ⚠  Could not list managed clusters for node-RG attribution: {exc}")
        return {}
    return _build_node_rg_map(data)


def _resolve_cluster(rid: str, rg_name: str, node_rg_map: dict[str, str]) -> str | None:
    """Resolve the AKS cluster a cost row belongs to, or None if not AKS.

    Attribution order:
      1. The managed cluster resource itself (ResourceId ``.../managedClusters/<name>``).
      2. Any resource in the cluster's ``MC_*`` node resource group (VMs, disks,
         load balancers, public IPs) via the node-RG -> cluster map.
      3. Resources in an ``MC_*`` node resource group for a cluster missing from the
         map (e.g. deleted clusters) -> labelled by the node RG name (lowercased so
         ``MC_...`` and ``mc_...`` collapse to one).
    """
    low_rid = rid.lower()
    if MANAGED_CLUSTER_MARKER in low_rid:
        idx = low_rid.index(MANAGED_CLUSTER_MARKER) + len(MANAGED_CLUSTER_MARKER)
        return rid[idx:].split("/")[0]

    rg_lower = rg_name.lower()
    if rg_lower in node_rg_map:
        return node_rg_map[rg_lower]
    if rg_lower.startswith("mc_"):
        return rg_lower
    return None


def _cluster_short_name(cluster_value: str) -> str:
    """Return the cluster short name from a ``Cluster`` dimension value.

    The ``Cluster`` dimension is the full managed-cluster ARM ID
    (e.g. ``/subscriptions/.../managedclusters/rbac-cluster``). Return the trailing
    resource name, or the original value unchanged if it is not an ARM ID.
    """
    if not cluster_value:
        return ""
    low = cluster_value.lower()
    if MANAGED_CLUSTER_MARKER in low:
        idx = low.index(MANAGED_CLUSTER_MARKER) + len(MANAGED_CLUSTER_MARKER)
        return cluster_value[idx:].split("/")[0]
    return cluster_value


def _build_namespace_query_body(start: str, end: str) -> dict:
    """Build the Cost Management query body for AKS namespace costs.

    Mirrors the request the Azure portal "Kubernetes namespaces" view issues. The
    top-level ``provider`` field is what unlocks the Kubernetes-specific
    ``Namespace``/``Cluster``/``ServiceCategory`` grouping dimensions, which the
    stable Query API rejects without it.

    Args:
        start: Start date in YYYY-MM-DD format (inclusive).
        end: End date in YYYY-MM-DD format (inclusive).
    """
    return {
        "type": "ActualCost",
        "dataSet": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
                "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
            },
            "sorting": [{"direction": "descending", "name": "Cost"}],
            "grouping": [
                {"type": "Dimension", "name": "Namespace"},
                {"type": "Dimension", "name": "Cluster"},
                {"type": "Dimension", "name": "ServiceCategory"},
            ],
        },
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start}T00:00:00Z",
            "to": f"{end}T23:59:59Z",
        },
        "provider": KUBERNETES_PROVIDER,
    }


def _parse_namespace_query(
    response: dict,
    cluster_filter: str | None = None,
) -> dict[str, dict[str, float]]:
    """Parse a namespace query response into ``cluster → namespace → cost``.

    Costs are summed across the ``ServiceCategory`` dimension (compute, network,
    storage, service), so each namespace shows a single total. Columns are looked
    up by name, so result column ordering does not matter.

    Args:
        response: Parsed JSON from the Cost Management query API.
        cluster_filter: Optional cluster name substring to filter results.
    """
    props = response.get("properties") or {}
    columns = props.get("columns") or []
    rows = props.get("rows") or []

    name_to_idx = {col.get("name"): i for i, col in enumerate(columns)}
    cost_idx = name_to_idx.get("Cost")
    ns_idx = name_to_idx.get("Namespace")
    cluster_idx = name_to_idx.get("Cluster")
    if cost_idx is None or ns_idx is None or cluster_idx is None:
        return {}
    required_idx = max(cost_idx, ns_idx, cluster_idx)

    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= required_idx:
            continue
        cluster = _cluster_short_name(str(row[cluster_idx] or ""))
        namespace = str(row[ns_idx] or "")
        if not cluster or not namespace:
            continue
        if cluster_filter and cluster_filter.lower() not in cluster.lower():
            continue
        try:
            cost = float(row[cost_idx] or 0)
        except (TypeError, ValueError):
            cost = 0.0
        if not math.isfinite(cost):
            cost = 0.0
        costs[cluster][namespace] += cost

    return costs


def fetch_namespace_costs(
    sub_id: str,
    start: str,
    end: str,
    token: str,
    cluster_filter: str | None = None,
) -> dict[str, dict[str, float]]:
    """Fetch invoice-reconciled AKS namespace costs via the Cost Management query API.

    Uses the same preview API the Azure portal "Kubernetes namespaces" view calls.
    Requires the AKS cost analysis add-on enabled on the cluster(s) and an EA/MCA
    subscription. Follows ``nextLink`` pagination so large subscriptions return in
    full.

    Args:
        sub_id: Azure subscription ID.
        start: Start date in YYYY-MM-DD format (inclusive).
        end: End date in YYYY-MM-DD format (inclusive).
        token: ARM bearer token.
        cluster_filter: Optional cluster name substring to filter results.

    Returns:
        Nested ``cluster → namespace → cost`` map (empty if no namespace data).

    Raises:
        CostReportError: If the query API call fails.
    """
    url = (
        f"{ARM_ENDPOINT}/subscriptions/{sub_id}"
        f"/providers/Microsoft.CostManagement/query"
        f"?api-version={COST_QUERY_API_VERSION}"
    )
    body = _build_namespace_query_body(start, end)

    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    next_url: str | None = url
    seen_urls: set[str] = set()
    pages = 0
    while next_url:
        if pages >= MAX_QUERY_PAGES:
            raise CostReportError(f"Namespace query exceeded {MAX_QUERY_PAGES} pages; results incomplete")
        if next_url in seen_urls:
            raise CostReportError("Namespace query returned a repeated nextLink; aborting to avoid double-counting")
        seen_urls.add(next_url)

        response = arm_json("post", next_url, token, body)
        for cluster, ns_map in _parse_namespace_query(response, cluster_filter).items():
            for namespace, cost in ns_map.items():
                costs[cluster][namespace] += cost
        next_url = (response.get("properties") or {}).get("nextLink")
        pages += 1
    return costs


def _fallback_infra_costs(
    raw_csv: str,
    cluster_filter: str | None = None,
    node_rg_map: dict[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Attribute AKS costs to clusters grouped by meter.

    Used when Kubernetes namespace columns aren't available in the billing data
    (the Generate Cost Details Report API never includes them). All resources in
    a cluster's ``MC_*`` node resource group — compute, network, and storage — are
    attributed to the owning cluster, not just ``Microsoft.ContainerService`` meters.
    """
    node_rg_map = node_rg_map or {}
    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(raw_csv))

    col_map = {}
    for col in reader.fieldnames or []:
        col_map[col.lower().lstrip("\ufeff")] = col

    rid_col = col_map.get("resourceid", "ResourceId")
    rg_col = col_map.get("resourcegroupname", "resourceGroupName")
    cat_col = col_map.get("metercategory", "meterCategory")
    meter_col = col_map.get("metername", "meterName")
    cost_col = col_map.get("costinbillingcurrency", "costInBillingCurrency")
    svc_col = col_map.get("consumedservice", "consumedService")

    for row in reader:
        rid = row.get(rid_col, "") or ""
        rg_name = row.get(rg_col, "") or ""
        cluster = _resolve_cluster(rid, rg_name, node_rg_map)

        if cluster is None:
            # Meter-based detection for managed-cluster service charges (Uptime
            # SLA, Defender, ACNS) and, when filtering, anything whose ID matches.
            svc = (row.get(svc_col, "") or "").lower()
            cat = (row.get(cat_col, "") or "").lower()
            is_aks = "kubernetes" in svc or "kubernetes" in cat or "containerservice" in svc
            if cluster_filter and cluster_filter.lower() in rid.lower():
                is_aks = True
            if not is_aks:
                continue
            cluster = rid.rsplit("/", 1)[-1]

        if cluster_filter and cluster_filter.lower() not in cluster.lower():
            continue

        meter = f"{row.get(cat_col, '')} / {row.get(meter_col, '')}"
        cost = float(row.get(cost_col, 0) or 0)
        costs[cluster][meter] += cost

    return costs


def download_and_parse(
    url: str,
    cluster_filter: str | None = None,
    node_rg_map: dict[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Download the cost details CSV and aggregate by cluster + namespace.

    Args:
        url: Blob download URL for the CSV report.
        cluster_filter: Optional cluster name substring to filter results.
        node_rg_map: Optional lowercased node-resource-group -> cluster-name map
            used to attribute node resource group costs when namespace columns
            are unavailable.

    Returns:
        Nested dict mapping cluster → namespace/meter → total cost.
    """
    print("Downloading cost details CSV...")
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
            raw_csv = response.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CostReportError(f"Failed to download cost details CSV: {reason}") from exc

    costs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    reader = csv.DictReader(io.StringIO(raw_csv))

    col_map = {}
    for col in reader.fieldnames or []:
        col_map[col.lower().lstrip("\ufeff")] = col

    k8s_cluster_col = col_map.get("x_kubernetesclustername") or col_map.get("kubernetesclustername")
    k8s_ns_col = col_map.get("x_kubernetesnamespace") or col_map.get("kubernetesnamespace")
    cost_col = col_map.get("costinbillingcurrency")

    if not k8s_cluster_col or not k8s_ns_col:
        print("\nℹ  Kubernetes namespace columns are not present in this cost details report.")
        print("   Namespace-level splits are only available in the portal Kubernetes views")
        print("   (Cost Management) or FOCUS-format exports — not the Cost Details API.")
        print("   Attributing costs to clusters instead, including node resource group")
        print("   (compute, network, storage) spend.\n")
        return _fallback_infra_costs(raw_csv, cluster_filter, node_rg_map)

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
    print(f"\n{'=' * 74}")
    print(f"{'Cluster/Resource':<30} {label:<30} {'Cost':>12}")
    print(f"{'=' * 74}")

    grand_total = 0.0
    for cluster in sorted(costs):
        cluster_total = sum(costs[cluster].values())
        grand_total += cluster_total
        for ns in sorted(costs[cluster], key=lambda x: costs[cluster][x], reverse=True):
            ns_cost = costs[cluster][ns]
            print(f"{cluster:<30} {ns:<30} ${ns_cost:>10,.2f}")
        print(f"{'':30} {'── cluster total ──':<30} ${cluster_total:>10,.2f}")
        print()

    print(f"{'=' * 74}")
    print(f"{'':30} {'GRAND TOTAL':<30} ${grand_total:>10,.2f}")


def _run_namespace_mode(sub_id: str, start: str, end: str, cluster: str | None, token: str) -> bool:
    """Run the namespace query path. Returns True if a report was printed.

    Raises:
        CostReportError: If the query API call fails.
    """
    print(f"Querying namespace costs for {start} → {end} (Cost Management query API)...")
    costs = fetch_namespace_costs(sub_id, start, end, token, cluster)
    if not costs:
        return False
    print_report(costs, namespace_mode=True)
    return True


def _run_infra_mode(sub_id: str, start: str, end: str, cluster: str | None, token: str) -> None:
    """Run the cluster-level infrastructure attribution path (Cost Details API)."""
    download_url = generate_cost_details(sub_id, start, end, token)
    print("Report ready. Downloading...")
    node_rg_map = fetch_cluster_node_rg_map(sub_id, token)
    costs = download_and_parse(download_url, cluster, node_rg_map)

    if not costs:
        print("\nNo Kubernetes cost data found.")
        print("Ensure the AKS cost analysis add-on is enabled and data has been collected (up to 48h).")
        return

    # Detect if we got namespace-level data or fallback infra data
    first_inner = next(iter(costs.values()))
    sample_keys = list(first_inner.keys())
    namespace_mode = not any("/" in k for k in sample_keys)
    print_report(costs, namespace_mode=namespace_mode)


def main() -> None:
    """CLI entry point for AKS namespace-level cost analysis."""
    parser = argparse.ArgumentParser(description="AKS namespace-level cost analysis")
    parser.add_argument("--start", default=(datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d"))
    parser.add_argument("--end", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    parser.add_argument("--cluster", help="Filter to a specific cluster name (contains match)")
    parser.add_argument("--subscription", default=os.environ.get("SUBSCRIPTION_ID"))
    parser.add_argument(
        "--token",
        default=os.environ.get(ACCESS_TOKEN_ENV),
        help=f"ARM bearer token (audience {ARM_ENDPOINT}). Defaults to the {ACCESS_TOKEN_ENV} env var. "
        "Generate one with: az account get-access-token "
        f"--resource {ARM_ENDPOINT} --query accessToken -o tsv",
    )
    parser.add_argument(
        "--mode",
        choices=["namespace", "infra"],
        default="namespace",
        help="namespace (default): per-namespace costs via the Cost Management query API, "
        "automatically falling back to infra attribution if no namespace data is available. "
        "infra: cluster-level attribution via the Cost Details API only.",
    )
    args = parser.parse_args()

    if not args.subscription:
        print("Set SUBSCRIPTION_ID env var or use --subscription", file=sys.stderr)
        sys.exit(1)

    if not args.token:
        print(
            f"Set {ACCESS_TOKEN_ENV} env var or use --token with an ARM bearer token.\n"
            f"  Generate one with: az account get-access-token "
            f"--resource {ARM_ENDPOINT} --query accessToken -o tsv",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if args.mode == "namespace":
            try:
                if _run_namespace_mode(args.subscription, args.start, args.end, args.cluster, args.token):
                    return
                reason = "No namespace data returned (add-on not enabled, non-EA/MCA subscription, or no data yet)."
            except CostReportError as exc:
                reason = f"Namespace query failed: {exc}"
            print(f"\nℹ  {reason}")
            print("   Falling back to cluster-level infrastructure attribution.\n")
        _run_infra_mode(args.subscription, args.start, args.end, args.cluster, args.token)
    except CostReportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

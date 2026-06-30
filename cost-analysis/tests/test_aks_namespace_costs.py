"""Tests for AKS namespace cost analysis."""

import csv
import io

import pytest

from aks_namespace_costs import (
    COST_QUERY_API_VERSION,
    KUBERNETES_PROVIDER,
    CostReportError,
    _build_namespace_query_body,
    _build_node_rg_map,
    _cluster_short_name,
    _extract_blob_url,
    _fallback_infra_costs,
    _parse_namespace_query,
    _resolve_cluster,
    fetch_namespace_costs,
)


def _build_csv(rows: list[dict], fieldnames: list[str] | None = None) -> str:
    """Helper to build CSV strings for testing."""
    if not fieldnames and rows:
        fieldnames = list(rows[0].keys())
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames or [])
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


# Column order the portal returns; tests deliberately vary it to prove the parser
# is order-independent.
_DEFAULT_QUERY_COLUMNS = ["Cost", "CostUSD", "Namespace", "Cluster", "ServiceCategory", "Currency"]


def _cluster_id(name: str) -> str:
    """Build a realistic lowercased managed-cluster ARM ID for a cluster name."""
    return (
        f"/subscriptions/6535fca9-4fa4-43ee-9320-b2f34de09589/resourcegroups/rg-aks"
        f"/providers/microsoft.containerservice/managedclusters/{name}"
    )


def _build_query_response(
    rows: list[list],
    columns: list[str] | None = None,
    next_link: str | None = None,
) -> dict:
    """Build a Cost Management query API response payload for testing.

    Args:
        rows: Row value lists, ordered to match ``columns``.
        columns: Column names; defaults to the portal's standard set.
        next_link: Optional pagination link to embed in ``properties.nextLink``.
    """
    col_names = columns or _DEFAULT_QUERY_COLUMNS
    props: dict = {
        "columns": [{"name": name, "type": "String"} for name in col_names],
        "rows": rows,
    }
    if next_link:
        props["nextLink"] = next_link
    return {"properties": props}


class TestCostReportError:
    """Tests for CostReportError custom exception."""

    def test_is_exception(self):
        assert issubclass(CostReportError, Exception)

    def test_message_preserved(self):
        err = CostReportError("something went wrong")
        assert str(err) == "something went wrong"


class TestExtractBlobUrl:
    """Tests for _extract_blob_url."""

    def test_manifest_format(self):
        data = {"manifest": {"blobs": [{"blobLink": "https://example.com/blob.csv"}]}}
        assert _extract_blob_url(data) == "https://example.com/blob.csv"

    def test_manifest_multiple_blobs_returns_first(self):
        data = {
            "manifest": {
                "blobs": [
                    {"blobLink": "https://example.com/first.csv"},
                    {"blobLink": "https://example.com/second.csv"},
                ]
            }
        }
        assert _extract_blob_url(data) == "https://example.com/first.csv"

    def test_properties_format(self):
        data = {"properties": {"downloadUrl": "https://example.com/download.csv"}}
        assert _extract_blob_url(data) == "https://example.com/download.csv"

    def test_empty_dict(self):
        assert _extract_blob_url({}) is None

    def test_empty_blobs_list(self):
        data = {"manifest": {"blobs": []}}
        assert _extract_blob_url(data) is None

    def test_missing_blob_link_key(self):
        data = {"manifest": {"blobs": [{"otherKey": "value"}]}}
        assert _extract_blob_url(data) is None

    def test_manifest_preferred_over_properties(self):
        data = {
            "manifest": {"blobs": [{"blobLink": "https://manifest.com/blob.csv"}]},
            "properties": {"downloadUrl": "https://properties.com/download.csv"},
        }
        assert _extract_blob_url(data) == "https://manifest.com/blob.csv"

    @pytest.mark.parametrize(
        "data",
        [
            {"manifest": None},
            {"properties": None},
            {"properties": {"other": "value"}},
            {"manifest": {"blobs": None}},
        ],
        ids=["manifest-none", "properties-none", "properties-missing-url", "blobs-none"],
    )
    def test_malformed_inputs_return_none(self, data):
        assert _extract_blob_url(data) is None


class TestFallbackInfraCosts:
    """Tests for _fallback_infra_costs."""

    def test_aggregates_aks_resources(self):
        rows = [
            {
                "ResourceId": "/subscriptions/sub1/providers/Microsoft.ContainerService/managedClusters/my-cluster",
                "consumedService": "Microsoft.ContainerService",
                "meterCategory": "Azure Kubernetes Service",
                "meterName": "Standard Node",
                "costInBillingCurrency": "100.50",
            },
            {
                "ResourceId": "/subscriptions/sub1/providers/Microsoft.ContainerService/managedClusters/my-cluster",
                "consumedService": "Microsoft.ContainerService",
                "meterCategory": "Azure Kubernetes Service",
                "meterName": "Standard Node",
                "costInBillingCurrency": "50.25",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv)

        assert "my-cluster" in costs
        assert costs["my-cluster"]["Azure Kubernetes Service / Standard Node"] == pytest.approx(150.75)

    def test_filters_non_aks_resources(self):
        rows = [
            {
                "ResourceId": "/providers/Microsoft.Storage/storageAccounts/sa1",
                "consumedService": "Microsoft.Storage",
                "meterCategory": "Storage",
                "meterName": "LRS Data Stored",
                "costInBillingCurrency": "10.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv)

        assert len(costs) == 0

    def test_cluster_filter_broadens_aks_detection(self):
        """cluster_filter includes non-AKS resources whose resource ID matches."""
        rows = [
            {
                "ResourceId": "/providers/Microsoft.Compute/virtualMachines/cluster-a-pool-vm",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Virtual Machines",
                "meterName": "D2s v3",
                "costInBillingCurrency": "50.00",
            },
        ]
        raw_csv = _build_csv(rows)

        costs_no_filter = _fallback_infra_costs(raw_csv)
        assert len(costs_no_filter) == 0

        costs_with_filter = _fallback_infra_costs(raw_csv, cluster_filter="cluster-a")
        assert "cluster-a-pool-vm" in costs_with_filter

    def test_empty_csv(self):
        raw_csv = "ResourceId,consumedService,meterCategory,meterName,costInBillingCurrency\n"
        costs = _fallback_infra_costs(raw_csv)
        assert len(costs) == 0

    def test_case_insensitive_columns(self):
        fieldnames = ["resourceid", "consumedservice", "metercategory", "metername", "costinbillingcurrency"]
        rows = [
            {
                "resourceid": "/providers/Microsoft.ContainerService/managedClusters/test-cluster",
                "consumedservice": "Microsoft.ContainerService",
                "metercategory": "Azure Kubernetes Service",
                "metername": "Node",
                "costinbillingcurrency": "75.00",
            },
        ]
        raw_csv = _build_csv(rows, fieldnames=fieldnames)
        costs = _fallback_infra_costs(raw_csv)

        assert "test-cluster" in costs
        assert costs["test-cluster"]["Azure Kubernetes Service / Node"] == pytest.approx(75.0)

    def test_multiple_clusters_aggregated_separately(self):
        rows = [
            {
                "ResourceId": "/providers/Microsoft.ContainerService/managedClusters/cluster-a",
                "consumedService": "Microsoft.ContainerService",
                "meterCategory": "Azure Kubernetes Service",
                "meterName": "Node",
                "costInBillingCurrency": "100.00",
            },
            {
                "ResourceId": "/providers/Microsoft.ContainerService/managedClusters/cluster-b",
                "consumedService": "Microsoft.ContainerService",
                "meterCategory": "Azure Kubernetes Service",
                "meterName": "Node",
                "costInBillingCurrency": "200.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv)

        assert costs["cluster-a"]["Azure Kubernetes Service / Node"] == pytest.approx(100.0)
        assert costs["cluster-b"]["Azure Kubernetes Service / Node"] == pytest.approx(200.0)

    def test_node_rg_compute_attributed_to_cluster(self):
        """VMs/disks/LBs in the MC_* node RG are attributed to the owning cluster."""
        node_rg_map = {"mc_myrg_my-cluster_eastus": "my-cluster"}
        rows = [
            {
                "ResourceId": "/subscriptions/s/resourceGroups/MC_myrg_my-cluster_eastus"
                "/providers/Microsoft.Compute/virtualMachineScaleSets/aks-np1-vmss",
                "resourceGroupName": "MC_myrg_my-cluster_eastus",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Virtual Machines",
                "meterName": "D2s v5",
                "costInBillingCurrency": "777.00",
            },
            {
                "ResourceId": "/subscriptions/s/resourceGroups/MC_myrg_my-cluster_eastus"
                "/providers/Microsoft.Network/loadBalancers/kubernetes",
                "resourceGroupName": "MC_myrg_my-cluster_eastus",
                "consumedService": "Microsoft.Network",
                "meterCategory": "Load Balancer",
                "meterName": "Standard Rule",
                "costInBillingCurrency": "148.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv, node_rg_map=node_rg_map)

        assert costs["my-cluster"]["Virtual Machines / D2s v5"] == pytest.approx(777.0)
        assert costs["my-cluster"]["Load Balancer / Standard Rule"] == pytest.approx(148.0)

    def test_node_rg_case_insensitive_collapse(self):
        """MC_... and mc_... rows for the same cluster collapse into one entry."""
        node_rg_map = {"mc_myrg_my-cluster_eastus": "my-cluster"}
        rows = [
            {
                "ResourceId": "/x/MC_myrg_my-cluster_eastus/providers/Microsoft.Compute/disks/d1",
                "resourceGroupName": "MC_myrg_my-cluster_eastus",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Storage",
                "meterName": "P10 Disk",
                "costInBillingCurrency": "10.00",
            },
            {
                "ResourceId": "/x/mc_myrg_my-cluster_eastus/providers/microsoft.compute/disks/d2",
                "resourceGroupName": "mc_myrg_my-cluster_eastus",
                "consumedService": "microsoft.compute",
                "meterCategory": "Storage",
                "meterName": "P10 Disk",
                "costInBillingCurrency": "10.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv, node_rg_map=node_rg_map)

        assert list(costs.keys()) == ["my-cluster"]
        assert costs["my-cluster"]["Storage / P10 Disk"] == pytest.approx(20.0)

    def test_unknown_node_rg_uses_lowercased_rg_label(self):
        """Node RG without a map entry (e.g. deleted cluster) is still attributed."""
        rows = [
            {
                "ResourceId": "/x/MC_gone_ghost_eastus/providers/Microsoft.Compute/disks/d1",
                "resourceGroupName": "MC_gone_ghost_eastus",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Storage",
                "meterName": "P10 Disk",
                "costInBillingCurrency": "5.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv)

        assert costs["mc_gone_ghost_eastus"]["Storage / P10 Disk"] == pytest.approx(5.0)

    def test_non_node_rg_compute_not_attributed(self):
        """Compute outside any node RG is not an AKS cost and is excluded."""
        rows = [
            {
                "ResourceId": "/x/some-rg/providers/Microsoft.Compute/virtualMachines/web-vm",
                "resourceGroupName": "some-rg",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Virtual Machines",
                "meterName": "D2s v5",
                "costInBillingCurrency": "50.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv)

        assert len(costs) == 0

    def test_cluster_filter_applies_to_node_rg(self):
        node_rg_map = {
            "mc_rg_keep_eastus": "keep",
            "mc_rg_drop_eastus": "drop",
        }
        rows = [
            {
                "ResourceId": "/x/MC_rg_keep_eastus/providers/Microsoft.Compute/disks/d1",
                "resourceGroupName": "MC_rg_keep_eastus",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Storage",
                "meterName": "P10 Disk",
                "costInBillingCurrency": "10.00",
            },
            {
                "ResourceId": "/x/MC_rg_drop_eastus/providers/Microsoft.Compute/disks/d2",
                "resourceGroupName": "MC_rg_drop_eastus",
                "consumedService": "Microsoft.Compute",
                "meterCategory": "Storage",
                "meterName": "P10 Disk",
                "costInBillingCurrency": "20.00",
            },
        ]
        raw_csv = _build_csv(rows)
        costs = _fallback_infra_costs(raw_csv, cluster_filter="keep", node_rg_map=node_rg_map)

        assert list(costs.keys()) == ["keep"]


class TestBuildNodeRgMap:
    """Tests for _build_node_rg_map."""

    def test_maps_node_rg_lowercased_to_name(self):
        data = {
            "value": [
                {"name": "my-cluster", "properties": {"nodeResourceGroup": "MC_rg_my-cluster_eastus"}},
            ]
        }
        assert _build_node_rg_map(data) == {"mc_rg_my-cluster_eastus": "my-cluster"}

    def test_multiple_clusters(self):
        data = {
            "value": [
                {"name": "a", "properties": {"nodeResourceGroup": "MC_rg_a_eastus"}},
                {"name": "b", "properties": {"nodeResourceGroup": "MC_rg_b_westus"}},
            ]
        }
        assert _build_node_rg_map(data) == {
            "mc_rg_a_eastus": "a",
            "mc_rg_b_westus": "b",
        }

    @pytest.mark.parametrize(
        "data",
        [{}, {"value": []}, {"value": None}],
        ids=["empty", "empty-list", "none-list"],
    )
    def test_empty_inputs(self, data):
        assert _build_node_rg_map(data) == {}

    def test_skips_clusters_missing_node_rg(self):
        data = {
            "value": [
                {"name": "ok", "properties": {"nodeResourceGroup": "MC_rg_ok_eastus"}},
                {"name": "no-props", "properties": {}},
                {"name": "no-name", "properties": {"nodeResourceGroup": "MC_rg_x_eastus"}, "extra": 1},
            ]
        }
        result = _build_node_rg_map(data)
        assert result == {"mc_rg_ok_eastus": "ok", "mc_rg_x_eastus": "no-name"}


class TestResolveCluster:
    """Tests for _resolve_cluster."""

    def test_managed_cluster_marker_preserves_case(self):
        rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/My-Cluster"
        assert _resolve_cluster(rid, "rg", {}) == "My-Cluster"

    def test_node_rg_map_match(self):
        node_rg_map = {"mc_rg_my-cluster_eastus": "my-cluster"}
        rid = "/x/MC_rg_my-cluster_eastus/providers/Microsoft.Compute/disks/d1"
        assert _resolve_cluster(rid, "MC_rg_my-cluster_eastus", node_rg_map) == "my-cluster"

    def test_mc_prefix_fallback_lowercased(self):
        rid = "/x/MC_rg_ghost_eastus/providers/Microsoft.Compute/disks/d1"
        assert _resolve_cluster(rid, "MC_rg_ghost_eastus", {}) == "mc_rg_ghost_eastus"

    def test_non_aks_returns_none(self):
        rid = "/x/some-rg/providers/Microsoft.Storage/storageAccounts/sa"
        assert _resolve_cluster(rid, "some-rg", {}) is None

    def test_managed_cluster_marker_takes_precedence_over_rg(self):
        node_rg_map = {"mc_rg_other_eastus": "other"}
        rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ContainerService/managedClusters/real"
        assert _resolve_cluster(rid, "MC_rg_other_eastus", node_rg_map) == "real"


class TestBuildNamespaceQueryBody:
    """Tests for _build_namespace_query_body."""

    def test_top_level_provider_is_set(self):
        body = _build_namespace_query_body("2026-06-01", "2026-06-30")
        assert body["provider"] == KUBERNETES_PROVIDER

    def test_time_period_brackets_full_days(self):
        body = _build_namespace_query_body("2026-06-01", "2026-06-30")
        assert body["timePeriod"]["from"] == "2026-06-01T00:00:00Z"
        assert body["timePeriod"]["to"] == "2026-06-30T23:59:59Z"

    def test_groups_by_namespace_cluster_service_category(self):
        body = _build_namespace_query_body("2026-06-01", "2026-06-30")
        names = [g["name"] for g in body["dataSet"]["grouping"]]
        assert names == ["Namespace", "Cluster", "ServiceCategory"]

    def test_granularity_none_for_monthly_total(self):
        body = _build_namespace_query_body("2026-06-01", "2026-06-30")
        assert body["dataSet"]["granularity"] == "None"
        assert body["timeframe"] == "Custom"

    def test_actual_cost_type(self):
        body = _build_namespace_query_body("2026-06-01", "2026-06-30")
        assert body["type"] == "ActualCost"


class TestClusterShortName:
    """Tests for _cluster_short_name."""

    def test_extracts_name_from_arm_id(self):
        assert _cluster_short_name(_cluster_id("rbac-cluster")) == "rbac-cluster"

    def test_case_insensitive_marker_preserves_value_case(self):
        rid = "/subscriptions/s/providers/Microsoft.ContainerService/managedClusters/My-Cluster"
        assert _cluster_short_name(rid) == "My-Cluster"

    def test_trailing_segment_after_name_dropped(self):
        rid = _cluster_id("prod-aks") + "/agentpools/np1"
        assert _cluster_short_name(rid) == "prod-aks"

    def test_non_arm_value_returned_unchanged(self):
        assert _cluster_short_name("just-a-name") == "just-a-name"

    def test_empty_string_returns_empty(self):
        assert _cluster_short_name("") == ""


class TestParseNamespaceQuery:
    """Tests for _parse_namespace_query."""

    def test_basic_namespace_costs(self):
        rows = [
            [10.5, 10.5, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [2.25, 2.25, "argocd", _cluster_id("rbac-cluster"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs == {"rbac-cluster": {"kube-system": 10.5, "argocd": 2.25}}

    def test_sums_across_service_categories(self):
        rows = [
            [10.0, 10.0, "gitlab", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [4.0, 4.0, "gitlab", _cluster_id("rbac-cluster"), "Networking", "USD"],
            [1.0, 1.0, "gitlab", _cluster_id("rbac-cluster"), "Storage", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs["rbac-cluster"]["gitlab"] == pytest.approx(15.0)

    def test_column_order_independent(self):
        # Columns shuffled vs the default ordering; parser must use names.
        columns = ["ServiceCategory", "Cluster", "Namespace", "CostUSD", "Cost", "Currency"]
        rows = [["Compute", _cluster_id("rbac-cluster"), "default", 7.0, 9.0, "USD"]]
        costs = _parse_namespace_query(_build_query_response(rows, columns=columns))
        assert costs["rbac-cluster"]["default"] == pytest.approx(9.0)

    def test_preserves_special_buckets(self):
        rows = [
            [5.0, 5.0, "#idle charges#", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [3.0, 3.0, "#unallocated charges#", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [2.0, 2.0, "#service charges#", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [1.0, 1.0, "#system charges#", _cluster_id("rbac-cluster"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert set(costs["rbac-cluster"]) == {
            "#idle charges#",
            "#unallocated charges#",
            "#service charges#",
            "#system charges#",
        }

    def test_separates_multiple_clusters(self):
        rows = [
            [10.0, 10.0, "kube-system", _cluster_id("cluster-a"), "Compute", "USD"],
            [20.0, 20.0, "kube-system", _cluster_id("cluster-b"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs == {
            "cluster-a": {"kube-system": 10.0},
            "cluster-b": {"kube-system": 20.0},
        }

    def test_cluster_filter_contains_match(self):
        rows = [
            [10.0, 10.0, "kube-system", _cluster_id("prod-aks"), "Compute", "USD"],
            [20.0, 20.0, "kube-system", _cluster_id("dev-aks"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows), cluster_filter="prod")
        assert costs == {"prod-aks": {"kube-system": 10.0}}

    def test_cluster_filter_is_case_insensitive(self):
        rows = [[10.0, 10.0, "kube-system", _cluster_id("Prod-AKS"), "Compute", "USD"]]
        costs = _parse_namespace_query(_build_query_response(rows), cluster_filter="PROD")
        assert costs == {"Prod-AKS": {"kube-system": 10.0}}

    @pytest.mark.parametrize("bad_cost", [None, "", "n/a", "NaN-ish"])
    def test_malformed_cost_treated_as_zero(self, bad_cost):
        rows = [[bad_cost, bad_cost, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs["rbac-cluster"]["kube-system"] == 0.0

    @pytest.mark.parametrize("bad_cost", ["NaN", "inf", "-inf", "Infinity", float("nan"), float("inf")])
    def test_non_finite_cost_treated_as_zero(self, bad_cost):
        rows = [[bad_cost, bad_cost, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs["rbac-cluster"]["kube-system"] == 0.0

    def test_short_row_skipped_without_crash(self):
        rows = [
            [10.0, 10.0],  # too few cells to index Cluster/ServiceCategory
            [5.0, 5.0, "argocd", _cluster_id("rbac-cluster"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs == {"rbac-cluster": {"argocd": 5.0}}

    def test_non_list_row_skipped(self):
        rows = [None, "garbage", [5.0, 5.0, "argocd", _cluster_id("rbac-cluster"), "Compute", "USD"]]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs == {"rbac-cluster": {"argocd": 5.0}}

    def test_skips_rows_missing_namespace_or_cluster(self):
        rows = [
            [10.0, 10.0, "", _cluster_id("rbac-cluster"), "Compute", "USD"],
            [10.0, 10.0, "kube-system", "", "Compute", "USD"],
            [5.0, 5.0, "argocd", _cluster_id("rbac-cluster"), "Compute", "USD"],
        ]
        costs = _parse_namespace_query(_build_query_response(rows))
        assert costs == {"rbac-cluster": {"argocd": 5.0}}

    def test_empty_rows_returns_empty(self):
        assert _parse_namespace_query(_build_query_response([])) == {}

    def test_missing_columns_returns_empty(self):
        # No Namespace/Cluster columns => cannot attribute => {}.
        columns = ["Cost", "CostUSD", "Currency"]
        rows = [[10.0, 10.0, "USD"]]
        assert _parse_namespace_query(_build_query_response(rows, columns=columns)) == {}

    def test_empty_response_returns_empty(self):
        assert _parse_namespace_query({}) == {}


class TestFetchNamespaceCosts:
    """Tests for fetch_namespace_costs (orchestration + pagination)."""

    def test_single_page(self, monkeypatch):
        rows = [[10.0, 10.0, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]]
        monkeypatch.setattr(
            "aks_namespace_costs.arm_json",
            lambda method, url, token, body=None: _build_query_response(rows),
        )
        costs = fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")
        assert costs == {"rbac-cluster": {"kube-system": 10.0}}

    def test_follows_next_link_and_merges_pages(self, monkeypatch):
        page1 = _build_query_response(
            [[10.0, 10.0, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]],
            next_link="https://management.azure.com/next-page",
        )
        page2 = _build_query_response(
            [[5.0, 5.0, "kube-system", _cluster_id("rbac-cluster"), "Networking", "USD"]],
        )
        responses = iter([page1, page2])
        calls: list[str] = []

        def fake_arm_json(method, url, token, body=None):
            calls.append(url)
            return next(responses)

        monkeypatch.setattr("aks_namespace_costs.arm_json", fake_arm_json)
        costs = fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")
        assert costs["rbac-cluster"]["kube-system"] == pytest.approx(15.0)
        assert len(calls) == 2
        assert calls[1] == "https://management.azure.com/next-page"

    def test_uses_preview_api_version_in_url(self, monkeypatch):
        seen: list[str] = []

        def fake_arm_json(method, url, token, body=None):
            seen.append(url)
            return _build_query_response([])

        monkeypatch.setattr("aks_namespace_costs.arm_json", fake_arm_json)
        fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")
        assert f"api-version={COST_QUERY_API_VERSION}" in seen[0]

    def test_passes_token_and_body(self, monkeypatch):
        captured: dict = {}

        def fake_arm_json(method, url, token, body=None):
            captured["method"] = method
            captured["token"] = token
            captured["body"] = body
            return _build_query_response([])

        monkeypatch.setattr("aks_namespace_costs.arm_json", fake_arm_json)
        fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "secret-token")  # noqa: S106
        assert captured["method"] == "post"
        assert captured["token"] == "secret-token"  # noqa: S105
        assert captured["body"]["provider"] == KUBERNETES_PROVIDER

    def test_propagates_cost_report_error(self, monkeypatch):
        def boom(method, url, token, body=None):
            raise CostReportError("ARM call failed")

        monkeypatch.setattr("aks_namespace_costs.arm_json", boom)
        with pytest.raises(CostReportError):
            fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")

    def test_empty_result_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(
            "aks_namespace_costs.arm_json",
            lambda method, url, token, body=None: _build_query_response([]),
        )
        assert fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok") == {}

    def test_repeated_next_link_raises(self, monkeypatch):
        # Server returns a nextLink pointing back to itself => would loop forever.
        loop = _build_query_response(
            [[1.0, 1.0, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]],
            next_link="https://management.azure.com/loop",
        )
        monkeypatch.setattr("aks_namespace_costs.arm_json", lambda method, url, token, body=None: loop)
        with pytest.raises(CostReportError, match="repeated nextLink"):
            fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")

    def test_exceeding_max_pages_raises(self, monkeypatch):
        # Each page has a unique nextLink so the loop only stops at the page cap.
        counter = {"n": 0}

        def fake_arm_json(method, url, token, body=None):
            counter["n"] += 1
            return _build_query_response(
                [[1.0, 1.0, "kube-system", _cluster_id("rbac-cluster"), "Compute", "USD"]],
                next_link=f"https://management.azure.com/page-{counter['n']}",
            )

        monkeypatch.setattr("aks_namespace_costs.arm_json", fake_arm_json)
        with pytest.raises(CostReportError, match="exceeded"):
            fetch_namespace_costs("sub-id", "2026-06-01", "2026-06-30", "tok")

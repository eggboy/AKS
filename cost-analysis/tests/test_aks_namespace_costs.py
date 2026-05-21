"""Tests for AKS namespace cost analysis."""

import csv
import io

import pytest

from aks_namespace_costs import (
    CostReportError,
    _extract_blob_url,
    _extract_polling_url,
    _fallback_infra_costs,
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


class TestExtractPollingUrl:
    """Tests for _extract_polling_url."""

    def test_location_header_single_quotes(self):
        stderr = "  'Location': 'https://management.azure.com/sub/costDetailsOperationResults/123'\n"
        result = _extract_polling_url(stderr)
        assert result == "https://management.azure.com/sub/costDetailsOperationResults/123"

    def test_location_header_lowercase(self):
        stderr = "  'location': 'https://management.azure.com/sub/costDetailsOperationResults/456'\n"
        result = _extract_polling_url(stderr)
        assert result == "https://management.azure.com/sub/costDetailsOperationResults/456"

    def test_regex_fallback(self):
        stderr = (
            "some debug output "
            "https://management.azure.com/sub/providers/costDetailsOperationResults/789 "
            "more text\n"
        )
        result = _extract_polling_url(stderr)
        assert result == "https://management.azure.com/sub/providers/costDetailsOperationResults/789"

    def test_no_matching_url(self):
        stderr = "some debug output without relevant URLs\n"
        assert _extract_polling_url(stderr) is None

    def test_empty_stderr(self):
        assert _extract_polling_url("") is None

    def test_location_header_preferred_over_regex(self):
        stderr = (
            "  'Location': 'https://management.azure.com/sub/costDetailsOperationResults/header'\n"
            "body url https://management.azure.com/sub/costDetailsOperationResults/body\n"
        )
        result = _extract_polling_url(stderr)
        assert result == "https://management.azure.com/sub/costDetailsOperationResults/header"


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

# AKS Namespace Cost Analysis

Get **invoice-accurate, per-namespace AKS cost** for any date range — ideal for
monthly chargeback/showback reports.

The tool calls the same Azure Cost Management **query** API that powers the Azure
portal's *Cost analysis → Kubernetes namespaces* view. It makes **pure REST calls**
(Python standard library only — **no Azure CLI or SDK dependency at runtime**),
authenticating with an Azure Resource Manager (ARM) bearer token you supply. Costs
are reconciled against your actual invoice (not in-cluster OpenCost list pricing),
and include the portal's special buckets: `#idle charges#`, `#system charges#`,
`#service charges#`, `#unallocated charges#`.

---

## Prerequisites

1. **[uv](https://docs.astral.sh/uv/)** (recommended) or Python 3.12+.
2. An **ARM bearer token** with the audience `https://management.azure.com` and read
   access (e.g. *Cost Management Reader* / *Reader*) on the subscription. See
   [Getting a token](#getting-a-token) below.
3. The **AKS cost analysis add-on** enabled on the cluster(s):
   ```bash
   az aks update -g <rg> -n <cluster> --enable-cost-analysis
   ```
4. An **Enterprise Agreement (EA) or Microsoft Customer Agreement (MCA)**
   subscription. This is required for the namespace view; pay-as-you-go
   subscriptions return no namespace data (the tool falls back automatically —
   see *Modes* below).

> The script is a [PEP 723](https://peps.python.org/pep-0723/) single-file
> script with **no third-party dependencies** (standard library only), so `uv`
> needs nothing to install.

---

## Getting a token

The script reads the token from the `AZURE_ACCESS_TOKEN` environment variable (or
the `--token` flag). The token's **audience/resource must be the ARM endpoint**
`https://management.azure.com`. Pick whichever source matches where you run it.

### 1. Azure CLI (local / interactive — easiest)

```bash
export AZURE_ACCESS_TOKEN=$(az account get-access-token \
  --resource https://management.azure.com \
  --query accessToken -o tsv)
```

> The CLI is used **only to mint the token** — the script itself never shells out
> to `az`. Tokens are short-lived (~60–90 min); regenerate when one expires.

### 2. Service principal (CI / automation)

Use OAuth2 client credentials against your tenant's token endpoint:

```bash
export AZURE_ACCESS_TOKEN=$(curl -s \
  -X POST "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" \
  -d "grant_type=client_credentials" \
  -d "client_id=<app-id>" \
  -d "client_secret=<app-secret>" \
  -d "scope=https://management.azure.com/.default" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

The service principal needs *Cost Management Reader* (or *Reader*) on the
subscription.

### 3. Managed identity via IMDS (in-cluster pod / Azure VM)

When running on an AKS pod or Azure VM with a managed identity assigned, fetch the
token from the Instance Metadata Service — no secret required:

```bash
export AZURE_ACCESS_TOKEN=$(curl -s -H "Metadata: true" \
  "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

This makes the script a good fit for a daily **CronJob** that writes per-namespace
cost to a database. Grant the pod's workload/managed identity *Cost Management
Reader* on the subscription.

---

## Quick start

```bash
cd cost-analysis

export SUBSCRIPTION_ID=<your-subscription-id>
export AZURE_ACCESS_TOKEN=$(az account get-access-token \
  --resource https://management.azure.com --query accessToken -o tsv)

# Namespace cost for a specific month:
uv run aks_namespace_costs.py --start 2026-06-01 --end 2026-06-30
```

Because it is a PEP 723 script, it also runs from any directory:

```bash
uv run --no-project /path/to/aks_namespace_costs.py --start 2026-06-01 --end 2026-06-30
```

Or with plain Python (no uv):

```bash
python3 aks_namespace_costs.py --start 2026-06-01 --end 2026-06-30
```

### Sample output

```
==========================================================================
Cluster/Resource               Namespace                              Cost
==========================================================================
my-cluster                     #service charges#              $    252.87
my-cluster                     kube-system                    $    219.21
my-cluster                     #idle charges#                 $    192.34
my-cluster                     app-routing-system             $    102.31
...
                               ── cluster total ──            $  1,211.28

==========================================================================
                               GRAND TOTAL                    $  1,211.28
```

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--start` | 30 days ago | Start date, `YYYY-MM-DD` (inclusive). |
| `--end` | today | End date, `YYYY-MM-DD` (inclusive). |
| `--cluster` | *(all)* | Filter to clusters whose name **contains** this string (case-insensitive). |
| `--subscription` | `$SUBSCRIPTION_ID` | Azure subscription ID. |
| `--token` | `$AZURE_ACCESS_TOKEN` | ARM bearer token (audience `https://management.azure.com`). See [Getting a token](#getting-a-token). |
| `--mode` | `namespace` | `namespace` or `infra` (see below). |

> **Cluster names are not hardcoded.** They are discovered from the API response
> (the `Cluster` dimension). `--cluster` is an optional *filter*, not a required
> input — omit it to see every cluster with cost in the subscription.

### Examples

```bash
# Default range (last 30 days), all clusters:
uv run aks_namespace_costs.py

# Filter to one cluster (contains-match):
uv run aks_namespace_costs.py --cluster prod --start 2026-06-01 --end 2026-06-30

# Pass the subscription explicitly instead of via env var:
uv run aks_namespace_costs.py --subscription <your-subscription-id> \
  --start 2026-06-01 --end 2026-06-30
```

---

## Modes

### `namespace` (default)

Per-namespace cost via the Cost Management query API
(`provider=Microsoft.ContainerService`). This is the primary, recommended path.

If it returns **no data** (add-on not enabled, non-EA/MCA subscription, or data
not collected yet) **or errors**, the tool prints the reason and automatically
falls back to `infra` mode so you still get a result.

### `infra`

Cluster-level attribution via the Generate Cost Details Report API. This API has
**no namespace columns**, so it cannot break cost down per namespace. Instead it
attributes all of a cluster's node-resource-group cost (compute, networking,
storage) to the owning cluster, mapped via the node resource group. Use this for
a cluster-level total when namespace data isn't available.

```bash
uv run aks_namespace_costs.py --mode infra --start 2026-06-01 --end 2026-06-30
```

---

## How it works

```
ARM bearer token ──► POST .../Microsoft.CostManagement/query?api-version=2023-04-01-preview
  (urllib, no az CLI)  body: { provider: "Microsoft.ContainerService",
                              grouping: [Namespace, Cluster, ServiceCategory], ... }
                              │
                              ▼
            rows grouped by namespace × cluster × service category
                              │  (summed across service category)
                              ▼
                  cluster ──► namespace ──► cost   ──►  printed report
```

- All HTTP is **pure `urllib`** with an `Authorization: Bearer <token>` header — no
  Azure CLI or SDK at runtime.
- Responses are parsed **by column name**, so Azure column-ordering changes don't
  break it.
- `nextLink` pagination is followed (with loop/limit guards) for large
  subscriptions.
- Transient `429 Too many requests` from Cost Management triggers the automatic
  fallback to `infra` mode; retry after ~60 s for namespace data.

### Caveat: preview API

`api-version=2023-04-01-preview` is the version the portal uses for the namespace
view. It is a **preview** version — undocumented and subject to change. The stable
Cost Management Query API, Cost Details API, and FOCUS exports do **not** expose a
namespace dimension, so this preview endpoint is currently the only public way to
get invoice-reconciled per-namespace cost.

---

## Development

```bash
# Install dev tooling (pytest, ruff):
uv venv --python 3.12
uv pip install -e ".[dev]"

# Lint + format:
uv run ruff check --fix . && uv run ruff format .

# Run the test suite (76 tests, no Azure calls — all mocked):
uv run pytest -q
```

The test suite mocks the ARM REST calls, so it runs offline and does not incur cost
or require a token.

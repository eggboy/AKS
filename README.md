# AKS

Centralised collection of Azure Kubernetes Service (AKS) guides, sample
applications, and supporting tooling that I have used or written while working
with AKS in production and lab environments.

## Repository layout

### Identity and access

- **[`PodIdentity/`](PodIdentity/)** — Walkthrough for the (now-deprecated)
  AAD Pod Identity add-on, including enabling pod identity on an existing
  cluster, binding a Managed Identity to a pod, and serving TLS via the
  Azure Key Vault Provider for Secrets Store CSI Driver. Ships two Spring
  Boot samples:
  - `boot-kv/` — Spring Boot reading secrets from Azure Key Vault.
  - `storage-test/` — Spring Boot accessing an Azure Storage Account, with
    docs comparing ROPC and Managed Identity credential flows.
- **[`WorkloadIdentity/`](WorkloadIdentity/)** — Azure AD Workload Identity
  sample. `storage-test/` is a Spring Boot app that authenticates to a
  Storage Account through a federated workload identity (no client secret).
- **[`GithubAction/`](GithubAction/)** — Notes for accessing an AAD-enabled
  AKS cluster from GitHub Actions in non-interactive mode using
  `kubelogin` + workload-identity federation. The matching workflow files
  live in [`.github/workflows/kubelogin.yml`](.github/workflows/kubelogin.yml)
  and [`.github/workflows/githubaction.yml`](.github/workflows/githubaction.yml).
- **[`kubeconfig-generator/`](kubeconfig-generator/)** — PEP 723 single-file
  `uv` script that mints a bearer-token kubeconfig for any Kubernetes
  ServiceAccount. Also contains `argocd/` RBAC manifests (standard wildcard
  and a hardened explicit-verb variant) for registering a target cluster
  with ArgoCD via the `argocd-manager` ServiceAccount.

### Networking and ingress

- **[`Ingress/AppGW_ManagedNginx/`](Ingress/AppGW_ManagedNginx/)** — Guide
  for fronting the AKS managed NGINX ingress controller (Application
  Routing add-on) with Azure Application Gateway, including TLS
  termination via Azure Key Vault, subdomain and path-based routing, and
  the App Gateway components (listener, backend pool/settings, custom
  health probe, rules) needed to make it work.
- **[`Ingress/sample-app/`](Ingress/sample-app/)** — Spring Boot echo app
  with reference Kubernetes manifests for several ingress flavours used in
  the guides above: internal/external NGINX, AGIC (Application Gateway
  Ingress Controller), and ALB (Application Gateway for Containers), plus
  subdomain and path-based routing examples.

### Compute and nodes

- **[`FlexNode/`](FlexNode/)** — End-to-end recipes for joining a
  non-Azure VM to an existing AKS cluster as a worker node with the
  [Azure AKS Flex Node](https://github.com/Azure/AKSFlexNode) agent in
  bootstrap-token mode (no Arc, no Service Principal):
  - `README.md` — Joining an AWS EC2 (Ubuntu 24.04) instance.
  - `AKSFlexNode-on-Azure-VM-guide.md` — Joining an Azure VM in a
    different region/VNet from the cluster.
- **[`GPU/`](GPU/)** — Creating a GPU node pool on AKS and installing the
  NVIDIA GPU Operator on top of node-feature-discovery, including a
  worked example of GPU time-slicing on `Standard_NC24ads_A100_v4`.
  `prometheus_dcgm.json` is a Grafana dashboard for DCGM GPU metrics.

### Infrastructure as code

- **[`terraform/`](terraform/)** — Terraform module that provisions an AKS
  cluster against pre-existing VNet/subnets, with system + user node
  pools, AAD-integrated managed Kubernetes RBAC (Azure RBAC for
  Kubernetes disabled), Azure CNI with Cilium network policy + data
  plane, OIDC issuer, KEDA, the Key Vault Secrets Provider
  add-on, OMS agent, Microsoft Defender, and an `AcrPull` role assignment
  for the kubelet identity against an existing Azure Container Registry.

### Operations and tooling

- **[`DockerRegistryMirror/`](DockerRegistryMirror/)** — Running an
  in-cluster Docker registry mirror (`docker.io/registry`) and wiring it
  into containerd on every node via a DaemonSet that installs the
  required `hosts.toml`. Requires containerd ≥ 1.5.
- **[`cost-analysis/`](cost-analysis/)** —
  `aks_namespace_costs.py` produces namespace-level cost breakdowns for
  AKS clusters using the Microsoft.CostManagement Generate Cost Details
  Report API. Requires the AKS cost analysis add-on. Packaged with
  `pyproject.toml` (Python ≥ 3.12, pytest + ruff dev deps).

## Per-directory documentation

Each subdirectory has its own `README.md` (or guide-suffixed `.md`) with the
detailed step-by-step instructions, manifests, and CLI snippets. This
top-level README is just an index.

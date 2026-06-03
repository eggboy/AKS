# ArgoCD external-cluster RBAC

Kubernetes manifests that provision the `argocd-manager` ServiceAccount and
RBAC that ArgoCD (running in a separate cluster) uses to manage **this** target
cluster.

Two flavours are provided:

| File | Permission model | When to use |
|---|---|---|
| `argocd/rbac.yaml` | Wildcard verbs on wildcard resources — matches upstream `argocd cluster add`. Functionally equivalent to `cluster-admin`. | Default. Required if ArgoCD must sync arbitrary CRDs installed later, RBAC objects that grant `escalate`/`bind`, or workloads that use uncommon verbs. |
| `argocd/rbac-hardened.yaml` | Explicit CRUD verbs (`get,list,watch,create,update,patch,delete,deletecollection`) on every resource. Read-only on discovery endpoints. | When you want to strip privilege-escalation primitives ArgoCD does not normally need. |

> **About "least privilege".** Kubernetes RBAC is allow-only — you cannot deny
> a specific resource inside a wildcard. When the management scope is "the
> whole cluster, any resource kind", the *minimum* role is still very broad.
> `argocd/rbac.yaml` is the least restrictive needed for the chosen scope, not
> least privilege in the narrow security sense.

## Quick start

```bash
# 1. Apply the RBAC to the TARGET cluster (the one ArgoCD will manage).
kubectl apply -f argocd/rbac.yaml          # or: argocd/rbac-hardened.yaml

# 2. Wait until the TokenController populates the Secret.
kubectl -n argocd wait --for=jsonpath='{.data.token}' \
  secret/argocd-manager-token --timeout=60s

# 3. Generate a kubeconfig with the project's generator.
./kubeconfig_generator.py -sa argocd-manager -n argocd

# 4. Register the cluster with ArgoCD (run against ArgoCD's host cluster).
argocd cluster add argocd-manager-context \
  --kubeconfig ./kubeconfig-argocd-manager \
  --name <friendly-cluster-name>
```

To extract the long-lived token manually instead of using the generator:

```bash
kubectl -n argocd get secret argocd-manager-token \
  -o jsonpath='{.data.token}' | base64 -d
```

## What each permission class enables

Use this table to decide whether `argocd/rbac.yaml`'s wildcard is acceptable,
or whether `argocd/rbac-hardened.yaml` (or an even tighter custom role) fits
better.

| Capability granted by `argocd/rbac.yaml` | ArgoCD normally needs it? | Risk if granted |
|---|---|---|
| CRUD on every resource (incl. `secrets`) | Yes | Secret exfiltration/overwrite, full workload control |
| `escalate` / `bind` on Roles/ClusterRoles | Only if Git manages RBAC that grants escalation | Bypasses the RBAC privilege-escalation check |
| `impersonate` on users/groups/SAs | No (rare) | Token holder can act as any identity in-cluster |
| `create` on `serviceaccounts/token` | No | Mint tokens for any other ServiceAccount |
| `approve` / `sign` on CSRs | No | Issue trusted client certificates |
| `pods/exec`, `attach`, `portforward` | Only via custom sync hooks | Interactive access to running workloads |
| `nodes/proxy`, `pods/proxy`, `services/proxy` | No | Bypass apiserver auth on backends |
| Wildcard non-resource URLs (writes) | No (reads only) | Hits internal apiserver endpoints |

`argocd/rbac-hardened.yaml` removes every row marked "No" by replacing the
verb wildcard with an explicit list and narrowing `nonResourceURLs` to
read-only discovery paths.

## Token model

The included `Secret` is of type `kubernetes.io/service-account-token` and is
populated by the in-cluster TokenController with a non-expiring bearer token.
This matches ArgoCD's behavior since v2.4 and avoids the silent breakage that
occurs when a TokenRequest-issued token (max ≈1 year on most clusters) expires
without anyone re-registering the cluster.

The ServiceAccount sets `automountServiceAccountToken: false` so that pods in
the target cluster never mount this credential by accident — only external
kubeconfig consumers (i.e. ArgoCD) ever see the token.

### Rotation

```bash
kubectl -n argocd delete secret argocd-manager-token
kubectl apply -f argocd/rbac.yaml
kubectl -n argocd wait --for=jsonpath='{.data.token}' \
  secret/argocd-manager-token --timeout=60s
# then re-run the generator and update the ArgoCD cluster Secret.
```

## Self-bricking warning

Do **not** place the manifests in `argocd/` under an ArgoCD Application that
manages the target cluster with `prune: true` and `selfHeal: true` unless you
fully understand the bootstrap order. ArgoCD can otherwise delete the very
ServiceAccount / Secret / ClusterRoleBinding it uses to authenticate and lose
access to the cluster.

If you want to manage these manifests via GitOps anyway, either:

* exclude them from pruning with the `Prune=false` sync option, or
* apply them out-of-band (e.g. via Terraform or a cluster-bootstrap job) and
  keep them outside any ArgoCD Application.

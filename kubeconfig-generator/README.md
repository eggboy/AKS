# kubeconfig-generator

Two things live in this directory:

1. **`kubeconfig_generator.py`** — a general-purpose CLI that issues a
   kubeconfig authenticating as a Kubernetes ServiceAccount. Useful any time
   you want a bearer-token kubeconfig for an application, CI job, or external
   tool instead of a user identity.
2. **`argocd/`** — opinionated RBAC manifests (`argocd-manager` SA + binding +
   long-lived token Secret) for using one such SA as ArgoCD's
   external-cluster manager. This is the canonical worked example.

---

## 1. Generate a kubeconfig for any ServiceAccount

```bash
./kubeconfig_generator.py -sa <sa-name> -n <sa-namespace>
# writes ./kubeconfig-<sa-name> (mode 0600)
```

The script is a [PEP 723 inline-metadata uv-run script](https://peps.python.org/pep-0723/) —
it self-installs its dependencies on first invocation (requires `uv`).

By default it uses the **long-lived token** from a `kubernetes.io/service-account-token`
Secret that the SA references (or that is annotated with
`kubernetes.io/service-account.name=<sa>`). If no such Secret exists, it falls
back to `kubectl create token` (TokenRequest, expires after `--token-expiry-hours`,
default 8760h ≈ 1y).

Pass `--no-prefer-long-lived-token` to invert that ordering — useful when you
*want* a short-lived rotating token.

Run `./kubeconfig_generator.py --help` for the full flag list (custom context
name, cluster name, output path, API server URL, source kubeconfig path, …).

---

## 2. ArgoCD external-cluster RBAC

Manifests that provision the `argocd-manager` ServiceAccount and RBAC that
ArgoCD (running in a separate cluster) uses to manage **this** target cluster.

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

### Quick start

```bash
# 1. Apply the RBAC to the TARGET cluster (the one ArgoCD will manage).
RBAC=argocd/rbac.yaml          # or: argocd/rbac-hardened.yaml
kubectl apply -f "$RBAC"

# 2. Wait until the TokenController populates the Secret.
kubectl -n argocd wait --for=jsonpath='{.data.token}' \
  secret/argocd-manager-token --timeout=60s

# 3. Generate a kubeconfig with the project's generator.
#    Default behaviour: reads the long-lived token from the Secret above —
#    this matches ArgoCD's external-cluster registration model and never expires.
./kubeconfig_generator.py -sa argocd-manager -n argocd

# 4. Register the cluster with ArgoCD (run against ArgoCD's host cluster).
#    The --service-account / --system-namespace flags tell `argocd cluster add`
#    to REUSE the SA we just created instead of creating a new (unbound) one
#    in kube-system, which is the CLI's default behaviour.
argocd cluster add argocd-manager-context \
  --kubeconfig ./kubeconfig-argocd-manager \
  --service-account argocd-manager \
  --system-namespace argocd \
  --name <friendly-cluster-name>
```

> ⚠️ If you omit `--service-account` and `--system-namespace`, the ArgoCD CLI
> will create a fresh `argocd-manager` SA in `kube-system` with no
> ClusterRoleBinding — every subsequent sync will then fail with `403 Forbidden`.

#### Alternative: register without the ArgoCD CLI

Construct the ArgoCD cluster Secret directly in ArgoCD's own namespace
(typically `argocd` on the ArgoCD host cluster):

```bash
TOKEN=$(kubectl -n argocd get secret argocd-manager-token \
  -o jsonpath='{.data.token}' | base64 -d)
CA=$(kubectl -n argocd get secret argocd-manager-token \
  -o jsonpath='{.data.ca\.crt}')
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

# Apply this to the ArgoCD HOST cluster, not the target:
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: <friendly-cluster-name>
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
stringData:
  name: <friendly-cluster-name>
  server: ${SERVER}
  config: |
    {"bearerToken": "${TOKEN}", "tlsClientConfig": {"caData": "${CA}"}}
EOF
```

### What each permission class enables

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

### Token model

The included `Secret` is of type `kubernetes.io/service-account-token` and is
populated by the in-cluster TokenController with a non-expiring bearer token.
This matches ArgoCD's behavior since v2.4 and avoids the silent breakage that
occurs when a TokenRequest-issued token (max ≈1 year on most clusters) expires
without anyone re-registering the cluster.

The ServiceAccount sets `automountServiceAccountToken: false` so that pods in
the target cluster never mount this credential by accident — only external
kubeconfig consumers (i.e. ArgoCD) ever see the token.

#### Rotation

```bash
RBAC=argocd/rbac.yaml          # or whichever flavour you applied
kubectl -n argocd delete secret argocd-manager-token
kubectl apply -f "$RBAC"
kubectl -n argocd wait --for=jsonpath='{.data.token}' \
  secret/argocd-manager-token --timeout=60s
# then re-run the generator and update the ArgoCD cluster Secret.
```

### Self-bricking warning

Do **not** place the manifests in `argocd/` under an ArgoCD Application that
manages the target cluster with `prune: true` and `selfHeal: true` unless you
fully understand the bootstrap order. ArgoCD can otherwise delete the very
ServiceAccount / Secret / ClusterRoleBinding it uses to authenticate and lose
access to the cluster.

If you want to manage these manifests via GitOps anyway, either:

* exclude them from pruning with the `Prune=false` sync option, or
* apply them out-of-band (e.g. via Terraform or a cluster-bootstrap job) and
  keep them outside any ArgoCD Application.

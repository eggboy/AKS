# Nginx to Application routing Gateway API (with Istio) Migration

A practical guide for moving AKS ingress from **NGINX** (the application-routing add-on's managed
NGINX, or self-hosted `ingress-nginx`) to the **application-routing add-on's Kubernetes Gateway API
implementation**, `approuting-istio`.

> **Scope:** L7 **HTTP/HTTPS** ingress. Every mapping and limitation below was verified on a running
> cluster; where a capability has **no equivalent**, that is called out explicitly so you can plan
> around it before cutover.

---

## 1. Know your target: `approuting-istio` is Gateway-API-only

The application-routing add-on's Gateway API implementation (`approuting-istio`) is **"Gateway API
based ingress via Istio, *without* service mesh functionality."** It runs an ingress-only `istiod`;
there are **no sidecars and no mesh**. The single most important consequence for a migration:

> **The Istio mesh CRDs — `DestinationRule`, `EnvoyFilter`, and the classic Istio `Gateway` — are
> not usable here.** Anything you would normally express through them (body-size limits, rate
> limiting, session affinity, backend HTTPS origination, client mTLS) has **no supported equivalent**
> on `approuting-istio`. See [§7](#7-capabilities-with-no-equivalent-plan-around-these).

This add-on is a different product from the **Istio service-mesh add-on**. If your NGINX setup
depends on the behaviors in §7, the service-mesh add-on (full Istio, mesh CRDs available) is the
appropriate target instead. Choose deliberately:

| | Application routing Gateway API (this guide) | Istio service-mesh add-on |
|---|---|---|
| Enable | `--enable-app-routing --enable-app-routing-istio --enable-gateway-api` | `az aks mesh enable` + `enable-ingress-gateway` + `--enable-gateway-api` |
| `GatewayClass` | `approuting-istio` | `istio` |
| Sidecars / mesh | No (ingress only) | Yes |
| Istio mesh CRDs (`DestinationRule`, `EnvoyFilter`, classic `Gateway`) | **Not available** | Available |
| Managed DNS + Key Vault TLS | Yes (App Routing operator) | No |
| Access logs on by default | Yes | No |

---

## 2. Prerequisites

Enable the implementation. All three flags matter: `--enable-app-routing-istio` turns on the Gateway
API implementation, `--enable-gateway-api` installs the AKS-managed Gateway API CRDs, and
`--enable-app-routing` deploys the App Routing operator (the DNS/TLS integration). The
`--enable-oidc-issuer`, `--enable-workload-identity`, and `azure-keyvault-secrets-provider` pieces are
needed only for the managed DNS/TLS flow in [§6](#6-tls-and-dns).

**New cluster:**
```bash
az aks create \
  --resource-group <rg> --name <cluster> --location <region> \
  --enable-app-routing-istio \
  --enable-app-routing \
  --enable-gateway-api \
  --enable-oidc-issuer \
  --enable-workload-identity \
  --enable-addons azure-keyvault-secrets-provider
```

**Existing cluster:**
```bash
az aks update -g <rg> -n <cluster> \
  --enable-app-routing --enable-app-routing-istio --enable-gateway-api \
  --enable-oidc-issuer --enable-workload-identity
az aks enable-addons -g <rg> -n <cluster> --addons azure-keyvault-secrets-provider
az aks get-credentials -g <rg> -n <cluster>
```

Confirm the control plane and `GatewayClass`:
```bash
kubectl get pods -n aks-istio-system        # istiod (the ingress control plane)
kubectl get gatewayclass approuting-istio    # ACCEPTED=True
```
```
NAME               CONTROLLER                               ACCEPTED   AGE
approuting-istio   istio.aks.azure.com/gateway-controller   True       ...
```

> **Reference versions** this guide was verified against: Kubernetes 1.34, Istio 1.29 (ingress
> `istiod`/proxy), Gateway API CRDs **v1.3.0, `standard` channel**. The managed install ships the
> standard channel only (`gateways`, `httproutes`, `grpcroutes`, `referencegrants`, `gatewayclasses`);
> experimental-channel CRDs are not present and cannot be added (see [§7](#7-capabilities-with-no-equivalent-plan-around-these)).

---

## 3. How the model differs

| Concept | NGINX Ingress | Application routing Gateway API |
|---|---|---|
| Controller | `ingress-nginx` / App Routing NGINX | `approuting-istio` Envoy gateway, auto-provisioned per `Gateway` |
| "Which controller" selector | `ingressClassName` | `Gateway` → `gatewayClassName: approuting-istio` |
| Routing object | `Ingress` (host/path + annotations) | `Gateway` (listeners/TLS) **+** `HTTPRoute` (host/path/filters) |
| Behavior tuning | `nginx.ingress.kubernetes.io/*` annotations | **HTTPRoute filters only** |
| TLS cert source | Key Vault annotation → synced Secret | Listener `tls.options` (Key Vault) **or** a TLS Secret in the Gateway namespace |
| Advanced traffic policy / mTLS / affinity | annotations | **no equivalent** ([§7](#7-capabilities-with-no-equivalent-plan-around-these)) |

**Key shift:** one `Ingress` becomes **two** objects — a shared `Gateway` (listeners + TLS, usually
one per environment) and a per-app `HTTPRoute`. Everything NGINX expressed as annotations either maps
to a native **HTTPRoute filter** ([§5](#5-annotation-cookbook-native-httproute-filters)) or has no
equivalent ([§7](#7-capabilities-with-no-equivalent-plan-around-these)).

### The auto-provisioned data plane

Applying a `Gateway` makes AKS provision a managed `Deployment` / `Service` (LoadBalancer) / `HPA`
named **`<gateway-name>-approuting-istio`**, in the **Gateway's own namespace**:

```bash
kubectl get gateway shared -n ingress
# NAME     CLASS              ADDRESS         PROGRAMMED   AGE
# shared   approuting-istio   <external-ip>   True         ...
```

The HPA defaults to `minReplicas: 2` — keep that in mind for anything that depends on replica count.
The TLS Secret a listener references **must live in this same Gateway namespace**.

---

## 4. Core translation: `Ingress` → `Gateway` + `HTTPRoute`

**Before (NGINX):**
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app1
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  tls:
    - hosts: [app1.example.com]
      secretName: keyvault-app1
  rules:
    - host: app1.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: { service: { name: app1, port: { number: 80 } } }
```

**After — shared `Gateway` (once per environment):**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: shared
  namespace: ingress            # data plane deploys here as shared-approuting-istio
spec:
  gatewayClassName: approuting-istio
  listeners:
    - name: http
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces: { from: All }
    - name: https
      port: 443
      protocol: HTTPS
      hostname: "*.example.com"
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: gw-tls         # TLS Secret in THIS namespace (see §6)
      allowedRoutes:
        namespaces: { from: All }
```

**After — per-app `HTTPRoute` (in the app namespace):**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: app1
  namespace: app1-ns
spec:
  parentRefs:
    - name: shared
      namespace: ingress
  hostnames: ["app1.example.com"]
  rules:
    - matches:
        - path: { type: PathPrefix, value: / }
      filters:
        - type: URLRewrite
          urlRewrite: { path: { type: ReplacePrefixMatch, replacePrefixMatch: / } }
      backendRefs:
        - name: app1
          port: 80
```

Verify before any DNS change, using the gateway IP with a `Host` header (or `--resolve`):
```bash
GW=$(kubectl get gateway shared -n ingress -o jsonpath='{.status.addresses[0].value}')
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: app1.example.com' "http://$GW/"          # 200
curl -sk -o /dev/null -w '%{http_code}\n' --resolve app1.example.com:443:$GW "https://app1.example.com/"  # 200
```

---

## 5. Annotation cookbook (native HTTPRoute filters)

Everything in this section is **native Gateway API** — no extra CRDs, fully supported on
`approuting-istio`.

| NGINX annotation / behavior | Gateway API equivalent |
|---|---|
| `nginx.ingress.kubernetes.io/rewrite-target` (prefix strip / replace) | `URLRewrite` → `ReplacePrefixMatch` |
| header add/set/remove (snippets / `proxy-set-headers`) | `RequestHeaderModifier` |
| inject/scrub **response** headers | `ResponseHeaderModifier` |
| total request timeout | rule `timeouts.request` |
| `nginx.ingress.kubernetes.io/ssl-redirect: "false"` | omit any HTTPS redirect (default) |
| `nginx.ingress.kubernetes.io/ssl-redirect: "true"` (force HTTPS) | HTTP listener + `RequestRedirect` → `scheme: https` |
| canary / weighted rollout (`nginx.ingress.kubernetes.io/canary-weight`) | multiple `backendRefs` with `weight` |
| `use-forwarded-headers` (append `X-Forwarded-For`/`-Proto`) | appended by default |

**Path rewrite** — `/vin/123` reaches the backend as `/anything/123`:
```yaml
filters:
  - type: URLRewrite
    urlRewrite:
      path: { type: ReplacePrefixMatch, replacePrefixMatch: /anything }
```

**Request / response headers**:
```yaml
filters:
  - type: RequestHeaderModifier
    requestHeaderModifier:
      add: [{ name: X-Custom-Header, value: demo }]
  - type: ResponseHeaderModifier
    responseHeaderModifier:
      add: [{ name: X-Canary, value: "on" }]
```

**Total request timeout** — note this is a *total* request timeout, **not** an NGINX read-idle
timeout. There is no read-idle equivalent on this add-on
([§7](#7-capabilities-with-no-equivalent-plan-around-these)):
```yaml
rules:
  - timeouts: { request: 60s }
    backendRefs: [{ name: app1, port: 80 }]
```

**Force HTTPS** — a redirect route bound to the HTTP listener via `sectionName` (returns `301` with a
`https://` `Location`):
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: app1-ssl-redirect, namespace: app1-ns }
spec:
  parentRefs:
    - name: shared
      namespace: ingress
      sectionName: http          # attach to the :80 listener only
  hostnames: ["app1.example.com"]
  rules:
    - filters:
        - type: RequestRedirect
          requestRedirect: { scheme: https, statusCode: 301 }
```

**Weighted split (canary)** — 80/20 across two backends:
```yaml
rules:
  - backendRefs:
      - { name: app1,        port: 80, weight: 80 }
      - { name: app1-canary, port: 80, weight: 20 }
```

---

## 6. TLS and DNS

NGINX App Routing pulls the cert from Key Vault via
`kubernetes.azure.com/tls-cert-keyvault-uri` and auto-creates the Secret. On `approuting-istio` you
have two paths.

**Option A — managed Key Vault TLS via the App Routing operator (recommended).** Put the cert
reference directly on the **listener** as `tls.options`; the operator provisions a
`SecretProviderClass`, syncs the Key Vault certificate to a `kubernetes.io/tls` Secret, and patches
the listener's `certificateRefs`. (Requires OIDC issuer + workload identity + the Key Vault secrets
provider add-on from [§2](#2-prerequisites).)
```yaml
    - name: https
      port: 443
      protocol: HTTPS
      hostname: "app1.example.com"
      tls:
        mode: Terminate
        options:
          kubernetes.azure.com/tls-cert-keyvault-uri: https://<vault>.vault.azure.net/certificates/<cert>
          kubernetes.azure.com/tls-cert-service-account: <service-account-in-listener-namespace>
```

**Option B — reference an existing TLS Secret** in the **Gateway namespace** (the SDS for the gateway
only loads Secrets from its own namespace):
```yaml
      tls:
        mode: Terminate
        certificateRefs:
          - { kind: Secret, name: gw-tls }   # must be in the Gateway's namespace
```

**Managed DNS.** The App Routing operator can also create Azure DNS records automatically from your
route hostnames. Apply a `ClusterExternalDNS` (cluster-scoped) or `ExternalDNS` (namespace-scoped)
resource that points an `external-dns` instance at your Azure DNS zone and tells it to watch
`Gateway` / `HTTPRoute` / `GRPCRoute` resources; records then appear from the hostnames on your
routes. This removes the separate `external-dns` deployment NGINX users typically ran.

---

## 7. Capabilities with NO equivalent (plan around these)

On NGINX these are annotations; on the Istio **service-mesh** add-on they map to `DestinationRule` /
`EnvoyFilter` / classic `Gateway`. **On `approuting-istio` none of those CRDs are usable**, so the
behaviors below have **no supported path on the gateway itself**. This is a platform boundary
enforced two independent ways:

1. The ingress `istiod` is granted access **only** to the `gateway.networking.k8s.io` API group. It
   cannot read `DestinationRule`, `EnvoyFilter`, or classic Istio `Gateway` resources — even if you
   install those CRDs yourself, they are silently never applied.
2. A managed admission webhook (`managed-gateway-api-ccp-validating-webhook`) **rejects installing or
   modifying any Gateway API CRD**, so you also cannot add experimental-channel resources such as
   `BackendTLSPolicy` (which would otherwise provide native backend-TLS) or the experimental
   `sessionPersistence` HTTPRoute field (native affinity).

You can't re-create these on the gateway — but most have a practical alternative that moves the
capability **in front of**, **inside**, or **beside** the gateway. The last column summarizes;
[§7.1](#71-alternatives-in-detail) has detail.

| NGINX annotation / behavior | On the gateway | Practical alternative |
|---|---|---|
| `proxy-body-size` (incl. the **1 MB default**) | ❌ uncapped | App-level `Content-Length` check **·** front WAF max-body-size (App Gateway WAF / Front Door) **·** API Management `validate-content` |
| `limit-rps` / `limit-burst-multiplier` / `limit-connections` | ❌ none | Front WAF rate-limit rule per client IP (App Gateway WAF v2 / Front Door Std-Prem) **·** API Management `rate-limit-by-key` / `quota-by-key` **·** app-level limiter |
| `affinity: cookie` + `session-cookie-*` | ❌ none | Make the app **stateless + shared session store** (e.g. Redis) **·** service-mesh add-on (`DestinationRule consistentHash`). *A front-proxy cookie does not help — it pins to the gateway origin, not to pods.* |
| `backend-protocol: HTTPS` | ❌ none | Expose an **HTTP port** on the backend for in-cluster traffic (TLS-terminating sidecar or app config), secured with `NetworkPolicy` **·** service-mesh add-on (sidecar TLS origination) |
| `auth-tls-secret` / `auth-tls-verify-client` (client mTLS) | ❌ none | Terminate/validate client certs on a **front proxy** — App Gateway mTLS (strict or passthrough), Front Door (Std/Prem) mTLS, or API Management client-cert validation **·** service-mesh add-on (classic `Gateway` `MUTUAL`) |
| `proxy-read-timeout` (read-idle) | ⚠️ partial | Omit `timeouts.request` (or set **`0s`**, which Gateway API treats as *disabled*) so long-lived streams aren't cut by the total timeout — you trade away idle-cutoff protection |
| `proxy-connect-timeout` / upstream keepalive tuning | ❌ none | Tune the **client/app** retry+timeout behavior **·** service-mesh add-on (`DestinationRule connectionPool`) |

> ⚠️ **Highest-risk gotcha — the disappearing 1 MB body limit.** NGINX rejects request bodies over
> **1 MB** by default. The `approuting-istio` gateway streams request bodies with **no implicit cap
> and no way to re-impose one at the gateway**. Uploads that NGINX silently rejected will start
> **succeeding** after cutover. Re-impose the cap somewhere else — application `Content-Length`
> validation, a front WAF/Front Door, API Management, or the service-mesh add-on — before you move
> any host that depended on it.

### 7.1 Alternatives in detail

Pick by where it's cleanest to enforce the behavior.

**A. Put an L7 service in front of the gateway (best for body-size, rate limiting, client mTLS).**
Many AKS estates already front the cluster with **Azure Front Door** or **Application Gateway (WAF
v2)** for global routing/TLS/WAF; that layer is the natural home for the gateway-level controls
`approuting-istio` lacks, and it covers every host at once:
  - **Body size** — App Gateway WAF enforces a configurable max request body size; Front Door caps
    request size at the edge. API Management's `validate-content` policy also rejects oversized bodies.
  - **Rate limiting** — App Gateway WAF v2 and Front Door (Standard/Premium) both support **rate-limit
    custom rules keyed on client IP** (requests per time window, action block). API Management offers
    `rate-limit-by-key` / `quota-by-key` for per-key limits.
  - **Client mTLS** — App Gateway supports mutual TLS in **strict** mode (validate against your CA) or
    **passthrough** mode (forward the client cert to the backend); Front Door Standard/Premium and API
    Management can validate client certificates too. Terminate/validate mTLS there, then forward to the
    gateway over the internal listener.

  > When you add a front proxy, the gateway appends to `X-Forwarded-For` by default, so the app can
  > still read the real client IP from the forwarded chain.

**B. Handle it in the application or pod.**
  - **Body size** — validate `Content-Length` / stream length in the app and return `413`.
  - **Rate limiting** — an in-app limiter (token bucket / middleware); note a per-pod limiter isn't a
    global cap, so size it against replica count.
  - **Backend HTTPS** — the gateway sends plaintext, so give the backend an **HTTP** port for
    in-cluster traffic (a TLS-terminating sidecar like a small nginx/Envoy in the pod, or app config),
    and restrict it with a `NetworkPolicy`. Reserve real backend TLS for the service-mesh add-on.
  - **Session affinity** — the durable fix is to make the workload **stateless** with a shared session
    store (Redis/Cosmos DB), which removes the need for stickiness entirely.

**C. Tune the native Gateway API you do have.**
  - **Read-idle timeout** — Gateway API only has a *total* `timeouts.request`. For long-poll/streaming
    routes, **omit it or set `0s`** (Gateway API treats zero as "disable the timeout") so in-flight
    streams aren't severed; you lose the idle-cutoff safety net, so pair it with client/app keepalives.

**D. Split the ingress, or pick a different target, for the exception hosts.**
  - **Run side by side** — migrate the L7-only hosts to `approuting-istio` now and **keep just the
    exception hosts on NGINX** (the managed NGINX add-on is supported through **November 2026**) or on
    the **Istio service-mesh add-on**, which exposes the full Istio CRD surface (`DestinationRule`,
    `EnvoyFilter`, classic `Gateway`) and expresses every row above natively.
  - **Application Gateway for Containers** is another Gateway API target with its own feature set if a
    host needs capabilities neither add-on covers.

**Bottom line:** an L7 front proxy (A) is usually the cleanest catch-all for body-size, rate limiting,
and client mTLS; backend-HTTPS and affinity are best solved in the workload (B); read-idle is a
config tweak (C); and anything still unmet means that host belongs on the service-mesh add-on or
stays on NGINX for now (D).

---

## 8. Migration strategy (zero-downtime, host-by-host)

Run NGINX and the Gateway in **parallel** and cut over one host at a time:

```
1. Stand up the shared Gateway (listeners + TLS) once per environment.   → verify: PROGRAMMED=True
2. Confirm each host's needs are covered by §5; if it needs anything      → if so: plan its §7.1
   from §7, plan an alternative (§7.1) or keep it off the gateway.           alternative or keep it.
3. For each migratable host: author an HTTPRoute against the SAME Service  → verify: curl via gateway
   while NGINX still serves prod.                                            IP with a Host header.
4. Smoke-test against the gateway IP with --resolve (no DNS change).       → verify: §9 checklist.
5. Cut over DNS / Front Door origin for that host to the gateway IP.       → verify: real 2xx, latency.
6. Soak; if healthy, remove the NGINX Ingress for that host.              → verify: NGINX log quiesces.
7. Repeat until all migratable hosts are done.
```

NGINX remains an **instant rollback** until DNS is switched and soaked.

---

## 9. Pre-cutover checklist (per host)

Run against the gateway IP with `--resolve`, before changing DNS:

```bash
GW=$(kubectl get gateway shared -n ingress -o jsonpath='{.status.addresses[0].value}')
HOST=app1.example.com

# 1. basic reachability + TLS termination
curl -sk -o /dev/null -w '%{http_code}\n' --resolve $HOST:443:$GW "https://$HOST/"           # expect 200

# 2. path rewrite (backend should see the rewritten path)
curl -s --resolve $HOST:443:$GW "https://$HOST/vin/123" -k                                    # backend path == rewrite

# 3. injected headers echoed by the backend
curl -s --resolve $HOST:443:$GW "https://$HOST/headers" -k                                    # X-Custom-Header present

# 4. total request timeout (if configured)
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --resolve $HOST:443:$GW "https://$HOST/<slow>" -k  # ~504 at the limit

# 5. force-HTTPS redirect (if configured)
curl -s -o /dev/null -w '%{http_code} %header{location}\n' -H "Host: $HOST" "http://$GW/"     # 301 https://...

# 6. access logs are on by default — confirm requests land
kubectl logs deploy/shared-approuting-istio -n ingress --tail=5                               # one JSON line per request
```

Each gateway proxy writes a **structured JSON access log line per request to stdout, on by default** —
no `Telemetry` resource or opt-in. Enable the Azure Monitor / Container Insights add-on to ship those
lines into Log Analytics (they appear in `ContainerLogV2`, filterable by the
`<gateway-name>-approuting-istio` pod name).

---

## 10. Decision summary

**Migrate to `approuting-istio`** when the host needs only L7 routing, TLS termination, path/header
rewrites, redirects, total request timeouts, and weighted splits — you also gain managed DNS + Key
Vault TLS and access logs by default.

**For hosts that need body-size limits, rate limiting, session affinity, backend HTTPS, client mTLS,
or read-idle timeouts**, the gateway has no native control — but you have alternatives ([§7.1](#71-alternatives-in-detail)):
push body-size / rate-limit / client-mTLS onto an **L7 front proxy** (Front Door, Application Gateway
WAF v2, or API Management); solve **backend HTTPS** and **affinity** in the workload (HTTP port +
`NetworkPolicy`, or stateless + shared session store); relax `timeouts.request` for **read-idle**
streaming; and for anything still unmet, **keep that host on NGINX** (supported through November 2026)
or move it to the **Istio service-mesh add-on**, which expresses all of these natively.

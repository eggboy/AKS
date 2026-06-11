# Migration Guide: Ingress NGINX → Istio Ingress Gateway (Gateway API) on AKS

A practical, **evidence-based** guide for moving AKS ingress from **NGINX** — either the **App Routing add-on (managed NGINX)** or self-hosted `ingress-nginx` — to the **AKS Istio add-on Ingress Gateway** driven by the **Kubernetes Gateway API**.

Every mapping in this guide was validated live on a real AKS cluster (Istio add-on, standard-channel Gateway API) across 15 ingress scenarios. The important distinction is **mechanism validated** versus **native 1:1 parity**: several NGINX behaviors work only with `DestinationRule` or `EnvoyFilter`, and a few are intentionally marked qualified rather than fully equivalent. Re-verify each capability in your own environment with the checklist in §9 before cutover.

> **Scope:** L7 **HTTP/HTTPS** ingress only.

---

## 1. How the two models differ

| Concept | NGINX Ingress | Istio Ingress Gateway (Gateway API) |
|---|---|---|
| Controller | `ingress-nginx` pod / App Routing add-on | Istio add-on (`asm-1-NN`) gateway pods in `aks-istio-ingress` |
| Selector of "which controller" | `ingressClassName` (e.g. `nginx-internal`) | `Gateway` → `gatewayClassName: istio` + listener |
| Routing object | `Ingress` (host/path rules + annotations) | `Gateway` (listeners/TLS) **+** `HTTPRoute` (host/path/filters) |
| Behavior tuning | `nginx.ingress.kubernetes.io/*` annotations | HTTPRoute **filters**, `DestinationRule`, `EnvoyFilter` |
| TLS cert source | Key Vault annotation → auto-synced secret | Secret in `aks-istio-ingress` (referenced by `credentialName`) |
| L7 features (rewrite, headers, redirect, basic request timeout) | annotations | **native Gateway API** |
| mTLS / session affinity | annotations | **classic Istio CRDs** (not standard-channel Gateway API) |

**Key shift:** one `Ingress` object becomes **two** objects — a shared `Gateway` (TLS + listeners, usually one per environment) and a per-app `HTTPRoute`. Behavior that NGINX expressed as annotations is split across HTTPRoute filters, DestinationRules, and (rarely) EnvoyFilters.

---

## 2. Prerequisites

1. **Istio add-on enabled** with an ingress gateway:
   ```bash
   az aks mesh enable --resource-group <rg> --name <cluster>
   az aks mesh enable-ingress-gateway --resource-group <rg> --name <cluster> --ingress-gateway-type external
   # internal gateway also available: --ingress-gateway-type internal
   ```
2. **Gateway API CRDs** present (the add-on installs the **standard** channel):
   ```bash
   kubectl get crd gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io
   kubectl get gatewayclass istio
   ```
3. Identify the gateway data plane (you reference its label in `Gateway.spec` / DestinationRules / EnvoyFilters):
   ```bash
   kubectl -n aks-istio-ingress get svc,pod -l istio=aks-istio-ingressgateway-external
   ```
4. **Namespaces are part of the mesh** (sidecar injection) so DestinationRules apply to your backends:
   ```bash
   kubectl label namespace <app-ns> istio.io/rev=asm-1-27 --overwrite
   ```

---

## 3. TLS: move certificates first

NGINX App Routing pulls the cert from Key Vault via
`kubernetes.azure.com/tls-cert-keyvault-uri: https://<vault>/certificates/<cert>` and auto-creates a secret named `keyvault-<ingress-name>`. **Istio has no equivalent annotation.** Two options:

**Option A — Secrets Store CSI Driver (recommended, keeps Key Vault as source of truth).**
Sync the Key Vault cert into a TLS secret **in `aks-istio-ingress`** with a `SecretProviderClass` (`objectType: secret`, `secretObjects` writing `tls.crt`/`tls.key`), mounted by a small pod so the secret is materialized. Then reference it from the `Gateway`.

**Option B — copy the existing secret** that App Routing already created:
```bash
kubectl get secret keyvault-<ingress-name> -n <app-ns> -o yaml \
  | sed 's/namespace: .*/namespace: aks-istio-ingress/' \
  | kubectl apply -f -
```

> **Critical:** gateway TLS/mTLS secrets **must live in `aks-istio-ingress`** — the shared external gateway's SDS only loads credentials from its own namespace.

---

## 4. Migration strategy (zero-downtime, host-by-host)

Run NGINX and Istio **in parallel** and cut over one host at a time:

```
1. Stand up the shared Istio Gateway (TLS) once per environment.      → verify: gateway PROGRAMMED=True
2. For each app: author HTTPRoute (+ DR/EnvoyFilter as needed),        → verify: curl via gateway IP with Host header
   pointing at the SAME Service, while NGINX still serves prod.
3. Smoke-test against the gateway IP using --resolve (no DNS change).  → verify: parity checklist (§7) passes
4. Cut over DNS / front-door origin for that host to the gateway IP.   → verify: real traffic 2xx, latency normal
5. Soak; if healthy, remove the NGINX Ingress object for that host.    → verify: NGINX access log quiesces
6. Repeat until all hosts migrated; then decommission NGINX.
```

This keeps NGINX as an **instant rollback** until DNS is switched and soaked.

---

## 5. Core translation: `Ingress` → `Gateway` + `HTTPRoute`

**Before (NGINX, their real pattern):**
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app1
  annotations:
    kubernetes.azure.com/tls-cert-keyvault-uri: https://kv.../certificates/app-cert
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx-internal
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

**After — shared `Gateway` (once per environment, in `aks-istio-ingress`):**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: shared-external
  namespace: aks-istio-ingress
spec:
  gatewayClassName: istio
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      hostname: "*.example.com"
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: keyvault-app1          # secret synced into aks-istio-ingress (§3)
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
    - name: shared-external
      namespace: aks-istio-ingress
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

Verify:
```bash
kubectl get gateway -n aks-istio-ingress shared-external          # PROGRAMMED=True, ADDRESS=<IP>
GW=<gateway-ip>
curl -sk https://app1.example.com/ --resolve app1.example.com:443:$GW -o /dev/null -w '%{http_code}\n'
```

---

## 6. Annotation cookbook (validated mappings)

Grouped by the Istio mechanism. Every mapping below was validated live; the **Status** column calls out where the result is mechanism-only or requires an additional caveat.

### 6.1 Native Gateway API — HTTPRoute filters (no extra CRDs)

| NGINX annotation | Istio (HTTPRoute) | Status |
|---|---|---|
| `rewrite-target: /$2` for prefix-strip patterns | `URLRewrite` → `ReplacePrefixMatch` | PASS for the tested prefix pattern (`/vin`, `/vin/`, `/vin/...`; non-matching `/vinyl` did not route) |
| `rewrite-target: /` | `URLRewrite` → `ReplacePrefixMatch: /` | PASS |
| `proxy-set-headers` / header-add snippets | `RequestHeaderModifier` (`add`/`set`/`remove`) | Mechanism PASS; validate the actual source header ConfigMap in the target cluster |
| basic total request timeout | rule `timeout.request` | PASS for total request timeout only; **not** NGINX read-idle parity |
| `ssl-redirect / force-ssl-redirect: false` | omit HTTPS redirect (default) | PASS |
| `ssl-redirect: true` (force HTTPS) | add an HTTP listener with a `RequestRedirect` filter → `scheme: https` | PASS |

```yaml
# headers + timeout in one rule
rules:
  - filters:
      - type: RequestHeaderModifier
        requestHeaderModifier:
          add:
            - { name: X-Custom-Header, value: demo }
    timeout: { request: 60s }     # total request timeout, not NGINX read-idle timeout
    backendRefs: [{ name: app1, port: 80 }]
```

### 6.2 Classic Istio `DestinationRule` (mesh-side traffic policy)

Create **one DestinationRule per backend Service**; combine multiple policies in a single `trafficPolicy`.

| NGINX annotation | DestinationRule field | Status |
|---|---|---|
| `affinity: cookie` + `session-cookie-name`/`-path` | `loadBalancer.consistentHash.httpCookie {name,path,ttl}` | PASS; path scoping and TTL expiry were validated |
| `backend-protocol: HTTPS` | `trafficPolicy.tls.mode: SIMPLE` (gateway originates TLS) | PASS only when backend cert validation is configured correctly; self-signed backends require `insecureSkipVerify: true` or a trusted CA/SAN setup |
| `proxy-connect-timeout` | `connectionPool.tcp.connectTimeout` | Qualified: timeout is per connect attempt; default gateway retries can make client-observed latency longer |
| `keepalive-timeout` / upstream keepalive | `connectionPool.http.idleTimeout` + `maxRequestsPerConnection` | Qualified: validates upstream pool idle/reuse, not full downstream/client keepalive parity |

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata: { name: app1, namespace: app1-ns }
spec:
  host: app1.app1-ns.svc.cluster.local
  trafficPolicy:
    loadBalancer:
      consistentHash:
        httpCookie: { name: SC_SESSION, path: /SC_WC, ttl: 3600s }   # ← affinity
    connectionPool:
      tcp:  { connectTimeout: 60s }                                  # ← proxy-connect-timeout
      http: { idleTimeout: 86400s, maxRequestsPerConnection: 0 }     # ← keepalive
    # tls:
    #   mode: SIMPLE
    #   # Use either a trusted backend certificate/CA configuration, or deliberately skip
    #   # verification for self-signed/internal certs after risk acceptance.
    #   # insecureSkipVerify: true
```

### 6.3 `EnvoyFilter` (only where no native knob exists)

EnvoyFilter **is accepted and enforced** on user-namespace Gateway API data planes in the AKS Istio add-on (validated — see gotcha §8.2).

| NGINX annotation | EnvoyFilter | Status |
|---|---|---|
| `proxy-body-size: 10m` / `20m` / `0` | HTTP `buffer` filter + route-level `BufferPerRoute` overrides | PASS; 10 MB, 20 MB, and disabled/unlimited behavior were validated |
| `proxy-read-timeout` read-idle semantics | route `idle_timeout` with total `timeout: 0s` | PASS via EnvoyFilter; HTTPRoute `timeout.request` is not equivalent |

```yaml
apiVersion: networking.istio.io/v1alpha3
kind: EnvoyFilter
metadata: { name: body-limit, namespace: aks-istio-ingress }
spec:
  workloadSelector: { labels: { istio: aks-istio-ingressgateway-external } }
  configPatches:
    - applyTo: HTTP_FILTER
      match:
        context: GATEWAY
        listener: { filterChain: { filter: { name: envoy.filters.network.http_connection_manager } } }
      patch:
        operation: INSERT_BEFORE
        value:
          name: envoy.filters.http.buffer
          typed_config:
            "@type": type.googleapis.com/envoy.extensions.filters.http.buffer.v3.Buffer
            max_request_bytes: 10485760
```

For multiple body-size values on one gateway, add route-level overrides:

```yaml
configPatches:
  - applyTo: HTTP_ROUTE
    match:
      context: GATEWAY
      routeConfiguration:
        vhost:
          name: app.example.com:80
          route:
            name: <generated-route-name>
    patch:
      operation: MERGE
      value:
        typed_per_filter_config:
          envoy.filters.http.buffer:
            "@type": type.googleapis.com/envoy.extensions.filters.http.buffer.v3.BufferPerRoute
            buffer:
              max_request_bytes: 20971520   # 20m; use disabled: true for proxy-body-size: "0"
```

For NGINX-style `proxy-read-timeout` read-idle behavior:

```yaml
configPatches:
  - applyTo: HTTP_ROUTE
    match:
      context: GATEWAY
      routeConfiguration:
        vhost:
          name: app.example.com:80
          route:
            name: <generated-route-name>
    patch:
      operation: MERGE
      value:
        route:
          timeout: 0s        # disable total request timeout for streaming routes
          idle_timeout: 60s  # NGINX-like read-idle timeout
```

### 6.4 Gateway-level / global

| NGINX setting | Istio equivalent |
|---|---|
| `use-forwarded-headers: true` | gateway forwards `X-Forwarded-For`/`-Proto` by default |
| mTLS (`auth-tls-secret` + `auth-tls-verify-client`) | **classic** `Gateway` `tls.mode: MUTUAL` + `credentialName` |

---

## 7. Capabilities that need classic Istio CRDs (no pure Gateway API parity)

The standard Gateway API channel does **not** cover these; use classic Istio CRDs:

| Capability | Why Gateway API can't (standard channel) | Use instead |
|---|---|---|
| **mTLS / client-cert auth** | `tls.frontendValidation` is rejected by the API server | classic `Gateway` `tls.mode: MUTUAL` + `credentialName` (secret with `tls.crt`/`tls.key`/`ca.crt` in `aks-istio-ingress`) |
| **Session affinity** | no `sessionPersistence` / `BackendLBPolicy` | `DestinationRule` `consistentHash.httpCookie` |

Both work — they just aren't Gateway-API-native (validated live).

```yaml
# mTLS — classic Gateway
apiVersion: networking.istio.io/v1
kind: Gateway
metadata: { name: mtls-gw, namespace: aks-istio-ingress }
spec:
  selector: { istio: aks-istio-ingressgateway-external }
  servers:
    - port: { number: 443, name: https, protocol: HTTPS }
      tls: { mode: MUTUAL, credentialName: mtls-credential }
      hosts: ["*"]
```

---

## 8. Critical gotchas (read before cutover)

### 8.1 The 1 MB body-size limit disappears silently
NGINX defaults `proxy-body-size` to **1 MB**. Envoy **streams request bodies with no implicit cap**, so after cutover large uploads that NGINX rejected will suddenly succeed. **Re-impose the limit explicitly** with the Buffer EnvoyFilter (§6.3). If different routes need `10m`, `20m`, and `0`, use route-level `BufferPerRoute`; a single gateway-wide cap is not enough.

### 8.2 EnvoyFilter is allowed on the add-on
Arbitrary `EnvoyFilter` (e.g. the buffer filter) **is accepted and programmed** on user-namespace Gateway API data planes in the AKS Istio add-on — there is no admission webhook blocking it. Body-size and request-header-buffer mappings rely on this (validated live).

### 8.3 Secret namespace matters
Gateway TLS/mTLS `credentialName`/`certificateRefs` secrets must be in **`aks-istio-ingress`**, not the app namespace. SDS won't load them otherwise (see §3).

### 8.4 DestinationRule needs the backend in the mesh
`DestinationRule` traffic policies (affinity, backend TLS, timeouts) require the **backend pods to have the Istio sidecar** (namespace labeled for injection). Without injection, the policy is silently ignored.

### 8.5 `backend-protocol: HTTPS` must account for certificate validation
`trafficPolicy.tls.mode: SIMPLE` originates TLS to the backend, but it does not automatically mean the upstream certificate is acceptable. In validation, a self-signed backend failed with `CERTIFICATE_VERIFY_FAILED` until `insecureSkipVerify: true` was added. Prefer a trusted CA/SAN configuration where possible; use `insecureSkipVerify` only when that matches the risk model.

### 8.6 `proxy-connect-timeout` is per-attempt, not always client-observed total
`connectionPool.tcp.connectTimeout` was programmed correctly, but the generated route also had a default retry policy (`num_retries: 2`). A blackhole backend with a 2s connect timeout therefore took roughly 6–8s from the client perspective. If exact latency matters, control retries as well as `connectTimeout`.

### 8.7 `proxy-read-timeout` is read-idle, not total request duration
Gateway API `timeout.request` is a total request timeout. It cut off a streaming response even though chunks arrived within the configured interval. Use Envoy route `idle_timeout` (§6.3) when you need NGINX-like read-idle behavior.

### 8.8 Two annotations have no exact equivalent (PARTIAL — low risk)
| NGINX annotation | Reality | Mitigation |
|---|---|---|
| `proxy-send-timeout` | No per-route upstream **send** timeout in Envoy | Covered transitively by `timeout.request` + `connectionPool`; usually no action needed |
| `proxy-buffer-size` / `proxy-buffers-number` | Envoy `max_request_headers_kb` / `per_connection_buffer_limit_bytes` tune **request** buffers; NGINX **response-header** buffering differs | Set only if you hit large-response-header limits; not a functional blocker |

These are buffering/timeout tunables — nothing breaks without them.

---

## 9. Validation checklist (per host, before DNS cutover)

Run each against the gateway IP with `--resolve` (no DNS change). These mirror the test recipes.

```bash
GW=<gateway-ip>; H=app1.example.com
R=(--resolve $H:443:$GW --resolve $H:80:$GW)

# 1. basic reachability + TLS
curl -sk "${R[@]}" https://$H/ -o /dev/null -w 'status=%{http_code}\n'

# 2. path rewrite (backend should see the rewritten path)
curl -sk "${R[@]}" https://$H/vin/api/v1/test

# 3. injected headers echoed
curl -sk "${R[@]}" https://$H/ | grep -i x-custom-header

# 4. read timeout
# For total request timeout: expect 504 around timeout.request.
# For NGINX read-idle parity: use a streaming endpoint; chunks inside idle_timeout should finish,
# but a gap longer than idle_timeout should fail.
curl -sk "${R[@]}" https://$H/delay/99 -o /dev/null -w 'status=%{http_code} t=%{time_total}\n'

# 5. body-size limit (expect 413 over the cap)
head -c 11534336 /dev/zero | curl -sk "${R[@]}" --data-binary @- https://$H/ -o /dev/null -w 'status=%{http_code}\n'

# 6. session affinity (all requests should pin to one pod)
C=$(curl -sk "${R[@]}" -D- https://$H/ | awk -F'; ' '/[Ss]et-[Cc]ookie/{print $1}' | cut -d' ' -f2)
for i in $(seq 10); do curl -sk "${R[@]}" -H "Cookie: $C" https://$H/ | grep -o 'pod=[^ ]*'; done | sort -u   # → 1 line

# 7. connect timeout: verify both cluster connect_timeout and route retry policy
curl -sk "${R[@]}" https://$H/connect-timeout -o /dev/null -w 'status=%{http_code} t=%{time_total}\n'

# 8. confirm what Envoy actually programmed
GW_POD=$(kubectl -n aks-istio-ingress get pod -l istio=aks-istio-ingressgateway-external -o jsonpath='{.items[0].metadata.name}')
kubectl -n aks-istio-ingress exec "$GW_POD" -c istio-proxy -- pilot-agent request GET config_dump | grep -E 'buffer|connect_timeout|idle_timeout|retry_policy'
```

Allow **~10–15 s** after applying config before asserting — programming is not instant.

---

## 10. Quick reference — which mechanism for which annotation

| Mechanism | Annotations it covers |
|---|---|
| **HTTPRoute filter** (native Gateway API) | rewrite-target, proxy-set-headers, total request timeout, ssl-redirect, header redirects |
| **DestinationRule** (classic) | affinity cookie, backend-protocol HTTPS, proxy-connect-timeout, upstream keepalive/idle |
| **EnvoyFilter** | proxy-body-size, route-specific body-size variants, NGINX-style read-idle timeout, request-header buffer tuning |
| **Classic Gateway** | mTLS (`MUTUAL`), force-HTTPS via dedicated listener |
| **No exact equivalent (PARTIAL)** | proxy-send-timeout, proxy-buffer-size/buffers-number |

All mappings above were validated live on an AKS cluster running the Istio add-on with the standard-channel Gateway API. Use the checklist in §9 to re-verify each capability in your own environment before cutover.

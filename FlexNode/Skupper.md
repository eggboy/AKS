# Skupper between AKS and an AKS Flex Node (over the public internet)

Set up a Skupper L7 service interconnect between AKS pods on regular AKS nodes and pods running on an AKS Flex Node, with **no VNet peering** and **no host-level access** to the Flex VM. Tested with Skupper 1.8.3 / skupper-router 2.7.3 against AKS 1.34.7.

The AKS side uses the standard `skupper init`. The Flex side runs **just the qpid-dispatch router as a DaemonSet** with a static ConfigMap — no Skupper service-controller, no `kubernetes.default` calls, no podman, no SSH.

---

## 1. Prereqs

- AKS cluster with at least one regular AKS node and one joined Flex node.
- `skupper` CLI (1.8.x) on your workstation.
- Cluster-admin kubeconfig.
- AKS egress can dial out to its own LoadBalancer IP (default).
- Flex node can dial the AKS LB public IP on TCP/45671.

```bash
export KUBECONFIG=...
export AKS_NS=skupper-aks
export FLEX_NS=skupper-flex
export FLEX_NODE=<your-flex-node-name>
```

---

## 2. Install Skupper on the AKS side

Pin the Skupper pods to a real AKS node (the default scheduler may place them on the Flex node, where they will not work).

```bash
kubectl create ns $AKS_NS
skupper init -n $AKS_NS --site-name aks-site \
  --enable-console=false --enable-flow-collector=false \
  --ingress loadbalancer --routers 1

# Replace (not merge) the nodeSelector — strategic-merge leaves the default keys behind.
for d in skupper-router skupper-service-controller; do
  kubectl -n $AKS_NS patch deploy $d --type=json --patch \
    '[{"op":"replace","path":"/spec/template/spec/nodeSelector",
       "value":{"kubernetes.azure.com/ebpf-dataplane":"cilium"}}]'
done

kubectl -n $AKS_NS rollout status deploy/skupper-router
kubectl -n $AKS_NS rollout status deploy/skupper-service-controller
```

Wait for the LoadBalancer to get an external IP:

```bash
kubectl -n $AKS_NS get svc skupper-router -w   # ctrl-C when EXTERNAL-IP appears
export AKS_LB=$(kubectl -n $AKS_NS get svc skupper-router \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "AKS Skupper LB: $AKS_LB"
```

---

## 3. Mint a connection token for the Flex side

```bash
skupper token create /tmp/skupper-token.yaml -n $AKS_NS --token-type=cert
# The file is just a Kubernetes Secret with ca.crt / tls.crt / tls.key.
```

Apply it into the Flex namespace and capture the resulting Secret name:

```bash
kubectl create ns $FLEX_NS
kubectl -n $FLEX_NS apply -f /tmp/skupper-token.yaml
export TOKEN_SECRET=$(kubectl -n $FLEX_NS get secret \
  -l skupper.io/type=connection-token -o jsonpath='{.items[0].metadata.name}')
echo "Token secret: $TOKEN_SECRET"
```

---

## 4. Static router config for the Flex side

Every service you want to bridge is a `tcpConnector` (outbound: AKS calls Flex) or `tcpListener` (inbound: Flex calls AKS) block in this ConfigMap.

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: flex-router-config
  namespace: $FLEX_NS
data:
  skrouterd.conf: |
    router {
      mode: edge
      id: flex-edge
    }

    sslProfile {
      name: link-aks
      caCertFile:     /etc/skupper-router-certs/link-aks/ca.crt
      certFile:       /etc/skupper-router-certs/link-aks/tls.crt
      privateKeyFile: /etc/skupper-router-certs/link-aks/tls.key
    }

    connector {
      name: aks
      host: $AKS_LB
      port: 45671
      role: edge
      sslProfile: link-aks
      verifyHostname: false
    }

    # Example: expose a Flex-node pod (nginx) to AKS as "flex-nginx:8080".
    tcpConnector {
      name: flex-nginx
      address: flex-nginx:8080
      host: 10.244.0.2          # pod IP on the Flex node — or a DNS name reachable locally
      port: 80
    }

    # Example: let Flex-node clients reach an AKS service "aks-nginx:8080".
    tcpListener {
      name: aks-nginx
      address: aks-nginx:8080
      host: 0.0.0.0
      port: 8080
    }
EOF
```

---

## 5. DaemonSet pinned to the Flex node

```bash
cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: skupper-flex-router
  namespace: $FLEX_NS
spec:
  selector:
    matchLabels: { app: skupper-flex-router }
  template:
    metadata:
      labels: { app: skupper-flex-router }
    spec:
      nodeSelector:
        kubernetes.azure.com/managed: "false"   # Flex nodes only
      tolerations:
        - operator: Exists
      containers:
        - name: router
          image: quay.io/skupper/skupper-router:2.7.3
          env:
            - name: QDROUTERD_CONF
              value: /etc/qpid-dispatch/skrouterd.conf
          ports:
            - { name: bridge, containerPort: 8080 }
          volumeMounts:
            - { name: config,   mountPath: /etc/qpid-dispatch }
            - { name: link-aks, mountPath: /etc/skupper-router-certs/link-aks }
      volumes:
        - name: config
          configMap: { name: flex-router-config }
        - name: link-aks
          secret: { secretName: $TOKEN_SECRET }
EOF

kubectl -n $FLEX_NS rollout status ds/skupper-flex-router
```

Verify the edge link came up:

```bash
kubectl -n $FLEX_NS exec ds/skupper-flex-router -- skstat -c | grep -E 'edge|aks'
# expect a line with mode=edge, dir=out, host=$AKS_LB, state=UP
```

---

## 6. Mirror each service on the AKS side

The Flex DaemonSet bridges traffic, but AKS pods still need a `Service` to call. For every `tcpConnector` you added on the Flex side, run:

```bash
skupper service create flex-nginx 8080 -n $AKS_NS
```

For every `tcpListener` you added (reverse direction), bind an AKS Deployment:

```bash
kubectl -n $AKS_NS create deployment aks-nginx --image=nginx
skupper service create aks-nginx 8080 -n $AKS_NS
skupper service bind   aks-nginx -n $AKS_NS deployment aks-nginx --target-port 80
```

---

## 7. Verify

```bash
# AKS pod (on an AKS node) → Flex pod
kubectl run probe --restart=Never --image=curlimages/curl \
  --overrides='{"spec":{"nodeName":"<aks-node-name>"}}' \
  --command -- sleep 60
kubectl wait --for=condition=Ready pod/probe --timeout=20s
kubectl exec probe -- curl -sf http://flex-nginx.$AKS_NS.svc.cluster.local:8080

# Flex pod → AKS pod
kubectl run probe-flex --restart=Never --image=curlimages/curl \
  --overrides="{\"spec\":{\"nodeName\":\"$FLEX_NODE\",\"tolerations\":[{\"operator\":\"Exists\"}]}}" \
  --command -- sleep 60
kubectl wait --for=condition=Ready pod/probe-flex --timeout=20s
# Pods on the Flex node have no kube-dns access, so call the DaemonSet directly:
kubectl exec probe-flex -- curl -sf http://skupper-flex-router.$FLEX_NS.svc.cluster.local:8080
# (or use the pod IP of the DaemonSet pod on this node)
```

Both should return the target's nginx response.

---

## 8. Adding more bridges later

1. Edit `flex-router-config` ConfigMap → add another `tcpConnector` or `tcpListener`.
2. `kubectl -n $FLEX_NS rollout restart ds/skupper-flex-router`.
3. On AKS, mirror with `skupper service create <name> <port> -n $AKS_NS` (and `skupper service bind` if the AKS side is the source).

---

## 9. Teardown

```bash
kubectl delete ns $FLEX_NS $AKS_NS
```

---

## Notes

- **Port ≥ 1024** for any service exposed via the DaemonSet — the router runs unprivileged.
- The Flex DaemonSet pod talks **only** to (a) other pods on the same Flex node and (b) the AKS LoadBalancer public IP. It never calls the in-cluster apiserver Service or kube-dns, so it doesn't need kube-proxy or Cilium on the Flex node.
- `nodeSelector: { kubernetes.azure.com/managed: "false" }` is the label aks-flex-node adds to joined hosts; adjust if your label differs.
- To bridge a Flex-side workload by Service name instead of pod IP, deploy a `kube-dns`-independent name (e.g. a hosts entry) or point the `tcpConnector.host` at the pod IP / a NodePort on the same Flex node.

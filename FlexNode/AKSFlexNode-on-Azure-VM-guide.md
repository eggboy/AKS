# Joining an Azure VM to an existing AKS cluster with AKS Flex Node

End-to-end recipe to turn any Ubuntu 24.04 Azure VM into a worker node of an existing AKS cluster using **bootstrap token** auth (no Arc, no Service Principal). Verified against `aks-flex-node v0.0.20` and AKS K8s `1.34.7` in May 2026.

> AKS Flex Node officially targets *non-Azure* VMs, but it works just as well on an Azure VM in a different region/VNet from the cluster. We joined a `westus3` VM to a `southeastasia` cluster.

---

## 0. Prereqs on your workstation

- `az` CLI logged in to the right tenant (`az login`)
- `kubectl` (any modern version)
- An existing AKS cluster you can fetch admin credentials for (i.e. `disableLocalAccounts: false`, or you're a member of an AAD admin group on the cluster)
- An SSH public key at `~/.ssh/id_rsa.pub`
- Variables — set these once in your shell:

```bash
# Cluster you want to join the VM to
export CLUSTER_RG="sandbox-rg"
export CLUSTER_NAME="rbac-cluster"

# Where you want the new VM
export VM_RG="aksflexnode-test-rg"
export VM_LOC="westus3"
export VM_NAME="flexnode-vm-01"
export VM_SIZE="Standard_D2s_v5"      # 2 vCPU / 8 GB / SSD

# Your public IP, so we can lock SSH down to just you
export MY_IP="$(curl -s https://ifconfig.me)"; echo "$MY_IP"
```

---

## 1. Get admin kubeconfig for the AKS cluster

```bash
export KUBECONFIG=/tmp/aksflex-kubeconfig
az aks get-credentials -g "$CLUSTER_RG" -n "$CLUSTER_NAME" \
  --admin --overwrite-existing --file "$KUBECONFIG"

kubectl get nodes        # sanity check
```

---

## 2. Create a Kubernetes bootstrap token + RBAC bindings

The flex node will use TLS bootstrapping: present a short-lived token, get a long-lived kubelet client certificate back.

```bash
TOKEN_ID=$(openssl rand -hex 3)
TOKEN_SECRET=$(openssl rand -hex 8)
export BOOTSTRAP_TOKEN="${TOKEN_ID}.${TOKEN_SECRET}"

# 24h TTL (macOS date syntax; on Linux: date -u -d "+24 hours" +"%Y-%m-%dT%H:%M:%SZ")
EXPIRATION=$(date -u -v+24H +"%Y-%m-%dT%H:%M:%SZ")
echo "$BOOTSTRAP_TOKEN  (expires $EXPIRATION)"

kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: bootstrap-token-${TOKEN_ID}
  namespace: kube-system
type: bootstrap.kubernetes.io/token
stringData:
  description: "AKS Flex Node bootstrap token"
  token-id: "${TOKEN_ID}"
  token-secret: "${TOKEN_SECRET}"
  expiration: "${EXPIRATION}"
  usage-bootstrap-authentication: "true"
  usage-bootstrap-signing: "true"
  auth-extra-groups: "system:bootstrappers:aks-flex-node"
EOF

kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: aks-flex-node-bootstrapper }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:node-bootstrapper
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: Group
  name: system:bootstrappers:aks-flex-node
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: aks-flex-node-auto-approve-csr }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:certificates.k8s.io:certificatesigningrequests:nodeclient
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: Group
  name: system:bootstrappers:aks-flex-node
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: aks-flex-node-role }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:node
subjects:
- apiGroup: rbac.authorization.k8s.io
  kind: Group
  name: system:bootstrappers:aks-flex-node
EOF
```

---

## 3. Provision the Azure VM

```bash
az group create -n "$VM_RG" -l "$VM_LOC" -o table

az vm create \
  --resource-group "$VM_RG" \
  --name "$VM_NAME" \
  --location "$VM_LOC" \
  --image Canonical:ubuntu-24_04-lts:server:latest \
  --size "$VM_SIZE" \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --public-ip-sku Standard \
  --os-disk-size-gb 50 \
  --nsg-rule SSH

VM_PUBLIC_IP=$(az vm show -g "$VM_RG" -n "$VM_NAME" -d --query publicIps -o tsv)
echo "VM_PUBLIC_IP=$VM_PUBLIC_IP"

# Lock SSH down to your IP only
NSG=$(az network nsg list -g "$VM_RG" --query "[0].name" -o tsv)
az network nsg rule update -g "$VM_RG" --nsg-name "$NSG" -n default-allow-ssh \
  --source-address-prefixes "$MY_IP" -o table

ssh -o StrictHostKeyChecking=accept-new azureuser@$VM_PUBLIC_IP 'uname -a; free -g; df -h /'
```

---

## 4. Build the agent config JSON locally

```bash
SUBSCRIPTION=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
AKS_RESOURCE_ID=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query id -o tsv)
LOCATION=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query location -o tsv)
K8S_VERSION=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query kubernetesVersion -o tsv)
SERVER_URL=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA_CERT_DATA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

cat > /tmp/aksflex-config.json <<EOF
{
  "azure": {
    "subscriptionId": "$SUBSCRIPTION",
    "tenantId": "$TENANT_ID",
    "cloud": "AzurePublicCloud",
    "bootstrapToken": { "token": "$BOOTSTRAP_TOKEN" },
    "arc": { "enabled": false },
    "targetCluster": {
      "resourceId": "$AKS_RESOURCE_ID",
      "location": "$LOCATION"
    }
  },
  "kubernetes": { "version": "$K8S_VERSION" },
  "node": {
    "kubelet": {
      "serverURL": "$SERVER_URL",
      "caCertData": "$CA_CERT_DATA"
    }
  },
  "agent": {
    "logLevel": "info",
    "logDir": "/var/log/aks-flex-node"
  }
}
EOF

scp /tmp/aksflex-config.json azureuser@$VM_PUBLIC_IP:/tmp/config.json
```

> ⚠️ The published docs sometimes show `"version": "1.30.0"` — **use your actual cluster version** (`kubectl version --short` or the `az aks show` query above), otherwise the agent will download a kubelet that mismatches the control plane.

---

## 5. Install the agent on the VM

```bash
ssh azureuser@$VM_PUBLIC_IP 'bash -s' <<'REMOTE'
set -e
# The installer prompts on /dev/tty for an az-login check that is only
# relevant for Arc mode. Pass --yes to skip it.
curl -fsSL https://raw.githubusercontent.com/Azure/AKSFlexNode/main/scripts/install.sh -o /tmp/install.sh
sudo bash /tmp/install.sh --yes

sudo aks-flex-node version
sudo mkdir -p /etc/aks-flex-node /var/log/aks-flex-node
sudo cp /tmp/config.json /etc/aks-flex-node/config.json
sudo chmod 600 /etc/aks-flex-node/config.json
REMOTE
```

---

## 6. Start the agent

> 💡 The published docs say `aks-flex-node bootstrap`, but the v0.0.20 release binary uses `agent` (the command was renamed in newer main-branch commits). The two are functionally equivalent for v0.0.20.

```bash
ssh azureuser@$VM_PUBLIC_IP 'bash -s' <<'REMOTE'
set -euo pipefail
sudo systemctl stop aks-flex-node-token 2>/dev/null || true
sudo systemctl reset-failed aks-flex-node-token 2>/dev/null || true

sudo systemd-run \
  --unit=aks-flex-node-token \
  --description="AKS Flex Node (token)" \
  --remain-after-exit \
  /usr/local/bin/aks-flex-node agent --config /etc/aks-flex-node/config.json

sleep 30
sudo systemctl status aks-flex-node-token --no-pager -l | head -15
sudo tail -n 30 /var/log/aks-flex-node/aks-flex-node.log
REMOTE
```

The agent runs ~13 bootstrap steps (install-arc → configure-os → download containerd/runc/CNI/kube binaries → configure-cni → configure-iptables → start-containerd → start-kubelet → start-npd) and typically finishes in **~30–60 seconds** depending on download speed.

---

## 7. Verify the node joined

```bash
kubectl get nodes -o wide   # flex-node should appear as Ready, version matches cluster

# Smoke test: schedule a tiny pod onto it
kubectl run flexnode-smoke \
  --image=registry.k8s.io/pause:3.10 \
  --restart=Never \
  --overrides='{"spec":{"nodeName":"'"$VM_NAME"'","tolerations":[{"operator":"Exists"}]}}'

kubectl get pod flexnode-smoke -o wide   # should be Running on flexnode-vm-01
```

---

## 8. Cross-cloud pod-to-pod with WireGuard (optional)

A joined Flex node can run pods, but **pod-to-pod traffic across the public internet is broken by default** — AKS pod CIDR (e.g. `10.244.0.0/16`) is not routable from the VM's VNet and vice versa. The fix is a WireGuard tunnel that carries both pod CIDRs **and** the node IPs.

### 8.1 Prereqs

```bash
# Variables (re-set if a new shell)
export AKS_POD_CIDR=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" \
  --query networkProfile.podCidr -o tsv)            # e.g. 10.244.0.0/16
export AKS_NODE_CIDR=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" \
  --query "agentPoolProfiles[0].vnetSubnetId" -o tsv \
  | xargs -I{} az network vnet subnet show --ids {} --query addressPrefix -o tsv)
                                                    # e.g. 10.10.0.0/16
export VM_VNET_CIDR="10.30.0.0/16"                  # whatever your VM subnet is
export WG_PORT=51820

# Generate two keypairs
mkdir -p /tmp/wg && cd /tmp/wg
wg genkey | tee aks_priv | wg pubkey > aks_pub
wg genkey | tee vm_priv  | wg pubkey > vm_pub
```

Open UDP `51820` from the AKS public egress IP to the VM:

```bash
NSG=$(az network nsg list -g "$VM_RG" --query "[0].name" -o tsv)
az network nsg rule create -g "$VM_RG" --nsg-name "$NSG" \
  -n allow-wg --priority 200 --access Allow --protocol Udp \
  --destination-port-ranges $WG_PORT --source-address-prefixes Internet
```

### 8.2 Install WireGuard on the VM

```bash
ssh azureuser@$VM_PUBLIC_IP 'bash -s' <<REMOTE
set -e
sudo apt-get update -qq && sudo apt-get install -y wireguard

sudo tee /etc/wireguard/wg0.conf >/dev/null <<EOF
[Interface]
PrivateKey = $(cat /tmp/wg/vm_priv)
Address    = 192.168.99.1/24
ListenPort = $WG_PORT
# Don't let wg-quick touch the host routing table — we'll add specific routes ourselves.
Table      = off

PostUp = ip route add ${AKS_POD_CIDR} dev wg0
PostUp = ip route add ${AKS_NODE_CIDR} dev wg0
PostDown = ip route del ${AKS_POD_CIDR} dev wg0 || true
PostDown = ip route del ${AKS_NODE_CIDR} dev wg0 || true

[Peer]
PublicKey  = $(cat /tmp/wg/aks_pub)
AllowedIPs = 192.168.99.2/32, ${AKS_POD_CIDR}, ${AKS_NODE_CIDR}
PersistentKeepalive = 25
EOF

sudo systemctl enable --now wg-quick@wg0
sudo wg show
REMOTE
```

### 8.3 Run a WireGuard gateway pod on an AKS node

The gateway runs as a `DaemonSet` pinned to one AKS node (use a label or `nodeName`). It needs `hostNetwork: true` and `NET_ADMIN`. **Do not use `linuxserver/wireguard`** — its PostUp scripts wipe the host's default route. Use a minimal image with plain `wireguard-tools`.

```bash
# Pick one AKS node to host the gateway
GW_NODE=$(kubectl get nodes -l '!kubernetes.azure.com/agentpool=flex' \
  -o jsonpath='{.items[0].metadata.name}')
kubectl label node "$GW_NODE" wg-gw=true --overwrite

VM_PUBLIC_IP=$(az vm show -g "$VM_RG" -n "$VM_NAME" -d --query publicIps -o tsv)

kubectl create ns wg-mesh --dry-run=client -o yaml | kubectl apply -f -

kubectl -n wg-mesh create secret generic wg-keys \
  --from-file=privkey=/tmp/wg/aks_priv \
  --from-literal=peer_pubkey="$(cat /tmp/wg/vm_pub)" \
  --from-literal=vm_endpoint="${VM_PUBLIC_IP}:${WG_PORT}" \
  --from-literal=vm_vnet_cidr="${VM_VNET_CIDR}"

kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: wg-gw
  namespace: wg-mesh
spec:
  selector: { matchLabels: { app: wg-gw } }
  template:
    metadata: { labels: { app: wg-gw } }
    spec:
      hostNetwork: true
      nodeSelector: { wg-gw: "true" }
      tolerations: [{ operator: Exists }]
      containers:
      - name: wg
        image: ghcr.io/jordanm/wireguard-tools:latest   # or any image with wg + iproute2
        securityContext:
          capabilities: { add: ["NET_ADMIN"] }
        command: ["/bin/sh","-c"]
        args:
        - |
          set -e
          PRIV=\$(cat /keys/privkey)
          PEER=\$(cat /keys/peer_pubkey)
          ENDPOINT=\$(cat /keys/vm_endpoint)
          VMNET=\$(cat /keys/vm_vnet_cidr)

          ip link add wg0 type wireguard || true
          ip addr add 192.168.99.2/24 dev wg0 || true
          echo "\$PRIV" | wg set wg0 private-key /dev/stdin
          wg set wg0 peer "\$PEER" endpoint "\$ENDPOINT" \
            allowed-ips 192.168.99.1/32,\${VMNET} persistent-keepalive 25
          ip link set wg0 up
          ip route replace \${VMNET} dev wg0
          sysctl -w net.ipv4.ip_forward=1
          iptables -t nat -A POSTROUTING -s \${VMNET} -o eth0 -j MASQUERADE || true

          sleep infinity
        volumeMounts:
        - { name: keys, mountPath: /keys, readOnly: true }
      volumes:
      - name: keys
        secret: { secretName: wg-keys }
EOF
```

### 8.4 Verify pod-to-pod

```bash
# Pod on AKS
kubectl run nginx-aks --image=nginx -l app=nginx-aks
AKS_POD_IP=$(kubectl get pod nginx-aks -o jsonpath='{.status.podIP}')

# Pod on Flex
kubectl run nginx-flex --image=nginx -l app=nginx-flex \
  --overrides='{"spec":{"nodeName":"'"$VM_NAME"'","tolerations":[{"operator":"Exists"}]}}'
FLEX_POD_IP=$(kubectl get pod nginx-flex -o jsonpath='{.status.podIP}')

# AKS → Flex
kubectl run c1 --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sf http://$FLEX_POD_IP

# Flex → AKS
kubectl run c2 --rm -it --image=curlimages/curl --restart=Never \
  --overrides='{"spec":{"nodeName":"'"$VM_NAME"'","tolerations":[{"operator":"Exists"}]}}' -- \
  curl -sf http://$AKS_POD_IP
```

Both should return the nginx welcome page.

---

## 9. Make `kubectl exec / logs / port-forward` work against Flex pods

Once the WireGuard tunnel from §8 is up **and the AKS node CIDR + Flex node CIDR are both in each peer's `AllowedIPs`**, `kubectl exec/logs/port-forward` into Flex pods works without any extra moving parts.

### Why the §8 config is exactly what you need

- `kubectl exec` is proxied by the apiserver through **konnectivity-agent**, which runs on AKS worker nodes with `hostNetwork: true`.
- That agent dials the Flex node's kubelet at `<flex-node-internal-ip>:10250` from the AKS node's host IP.
- So the tunnel must carry both the **AKS node IP → Flex node IP** path **and** the reply path.

Concretely, `AllowedIPs` on each side must include the **opposite side's node CIDR**:

| Side | AllowedIPs |
|---|---|
| AKS gateway pod (peer = VM) | `192.168.99.1/32, <VM_VNET_CIDR>` |
| VM (peer = AKS gateway) | `192.168.99.2/32, <AKS_NODE_CIDR>` |

The §8 `wg0.conf` and the `wg set ... allowed-ips` line already include these — nothing else to do.

### Verify

```bash
FLEX_POD=$(kubectl get pod -l app=nginx-flex -o jsonpath='{.items[0].metadata.name}')

kubectl exec $FLEX_POD -- hostname           # → nginx-flex
kubectl logs $FLEX_POD --tail=5              # → nginx access log
kubectl port-forward $FLEX_POD 18080:80 &    # → "Forwarding from 127.0.0.1:18080 -> 80"
curl -sf http://localhost:18080 >/dev/null && echo OK
```

### Gotchas

- **Never use the `linuxserver/wireguard` image for the AKS-side gateway.** Its PostUp/PreDown scripts manipulate the host routing table from inside `hostNetwork: true`, which can delete the AKS node's default route. When that happens, kubelet loses contact with the apiserver, the node goes `Ready=Unknown`, and pods get evicted. Use a plain `wireguard-tools` image and set `Table = off` on the VM side (already done in §8.2).
- **kube-proxy does not run on the Flex node**, so Service ClusterIPs (`kubernetes.default`, `kube-dns`) are not reachable from pods scheduled there. Use pod IPs for cross-cloud reachability tests.
- If `kubectl exec` returns `504 ... error dialing backend: ... i/o timeout` from konnectivity, check `wg show` on both sides for handshake activity, and confirm the Flex node's `InternalIP` in `kubectl get node <flex> -o wide` is inside the AKS-side peer's `AllowedIPs`.

---

## 10. Teardown

```bash
# Cluster-side cleanup
kubectl delete pod flexnode-smoke --ignore-not-found
kubectl delete node "$VM_NAME" --ignore-not-found
kubectl -n kube-system delete secret "bootstrap-token-${TOKEN_ID}" --ignore-not-found
kubectl delete clusterrolebinding \
  aks-flex-node-bootstrapper \
  aks-flex-node-auto-approve-csr \
  aks-flex-node-role --ignore-not-found

# Azure-side cleanup (nukes VM, disk, NIC, NSG, public IP)
az group delete -n "$VM_RG" --yes --no-wait

# WireGuard cleanup (if you ran §8/§9)
kubectl delete ns wg-mesh --ignore-not-found
kubectl label node "$GW_NODE" wg-gw- 2>/dev/null || true
```

---

## Gotchas / notes

| Issue | Fix |
|---|---|
| `install.sh` hangs on `Do you want to continue anyway?` | Pass `--yes` — the prompt only matters in Arc mode. |
| `unknown command "bootstrap"` | Release `v0.0.20` exposes it as `agent`, not `bootstrap`. Docs are ahead of the release. |
| Config says `kubernetes.version: 1.30.0` (from docs) | Use your actual cluster version — version skew between kubelet and apiserver will block the join. |
| Warning `Failed to ... AzureCLICredential: Please run 'az login'` in agent log | Benign for token mode. It's the periodic AKS spec collector trying to enrich status — node still works fine. |
| Node in different region/VNet than cluster | Totally supported — flex node only needs outbound HTTPS to the AKS API server + container registries. |
| AKS cluster has AAD + Azure RBAC enabled, no admin group | `--admin` get-credentials may fail. Either disable local accounts gating, or add yourself to an AAD admin group on the cluster first. |
| `disableLocalAccounts: true` | You can't use `--admin`; authenticate via AAD instead and ensure your principal has `Azure Kubernetes Service Cluster Admin Role`. |

## Reference

- GitHub: https://github.com/Azure/AKSFlexNode
- Usage docs: https://github.com/Azure/AKSFlexNode/blob/main/docs/usage.md
- E2E reference script (what this guide mirrors): https://github.com/Azure/AKSFlexNode/blob/main/hack/e2e/lib/node-join-token.sh
- Upstream K8s bootstrap tokens: https://kubernetes.io/docs/reference/access-authn-authz/bootstrap-tokens/

# Joining an AWS EC2 instance to an AKS cluster with AKS Flex Node

AKS Flex Node is an alpha agent from the Azure team that turns any Ubuntu 22.04 or 24.04 VM into an AKS worker node. The README lists AWS, GCP, OCI, Nebius, Tensorwave, and NVIDIA DGX Spark as supported targets. I ran the whole flow on a `t3.medium` in `ap-southeast-1` against an AKS cluster in `southeastasia` to confirm it works end to end.

This guide uses bootstrap-token mode, which is the simplest of the three auth modes the agent supports (Azure Arc and Service Principal are the other two). Verified with `aks-flex-node v0.0.20` and AKS Kubernetes `1.34.7`.

Project repo is at https://github.com/Azure/AKSFlexNode and the usage doc is at https://github.com/Azure/AKSFlexNode/blob/main/docs/usage.md.

## Prereqs on your workstation

You need four things on your laptop.

- `aws` CLI configured. For a personal sandbox account `AmazonEC2FullAccess` is enough. The minimum scoped policy is `ec2:Describe*`, `ec2:RunInstances`, `ec2:TerminateInstances`, `ec2:Create/DeleteSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`, `ec2:ImportKeyPair`, `ec2:CreateTags`. Don't use root access keys, create an IAM admin user instead.
- `az` CLI logged in to the tenant that owns the AKS cluster.
- `kubectl` on your `PATH`.
- An SSH public key at `~/.ssh/id_rsa.pub`.

Set the variables once so the rest of the snippets are copy-paste.

```bash
# AKS target
export CLUSTER_RG="sandbox-rg"
export CLUSTER_NAME="rbac-cluster"

# AWS target
export AWS_REGION="ap-southeast-1"        # pick the region closest to your AKS
export KEY_NAME="aksflexnode-key"
export SG_NAME="aksflexnode-sg"
export INSTANCE_TYPE="t3.medium"          # 2 vCPU, 4 GB, ~$0.05/hr on-demand
export MY_IP="$(curl -s https://ifconfig.me)/32"; echo "$MY_IP"
```

On instance size, the README says minimum 2 GB RAM. `t3.medium` gives you 4 GB for about a nickel per hour, which is the sweet spot. Free Tier `t3.micro` (1 GB) is not enough for this workload.

## Bootstrap token mode

Bootstrap tokens are a stock Kubernetes feature (https://kubernetes.io/docs/reference/access-authn-authz/bootstrap-tokens/) used for TLS bootstrapping. The node presents a short-lived shared token to the API server, gets a long-lived client certificate back via a signed CSR, and then forgets the token. No Azure Arc registration, no Service Principal, no managed identity on the EC2 side. The host cloud genuinely doesn't matter to the agent.

## Get admin kubeconfig for the AKS cluster

You need cluster-admin to create the bootstrap token secret and the RBAC bindings.

```bash
export KUBECONFIG=/tmp/aksflex-kubeconfig
az aks get-credentials -g "$CLUSTER_RG" -n "$CLUSTER_NAME" \
  --admin --overwrite-existing --file "$KUBECONFIG"

kubectl get nodes
```

If your cluster has `disableLocalAccounts: true`, drop the `--admin` flag and make sure your AAD principal has the `Azure Kubernetes Service Cluster Admin Role` on the cluster.

## Bootstrap token and RBAC bindings

Token format is strict. It has to be `6-char-id.16-char-secret`, the secret has to live in `kube-system` with type `bootstrap.kubernetes.io/token`, and the secret name has to be literally `bootstrap-token-<id>`. Anything else and the API server silently ignores it.

```bash
TOKEN_ID=$(openssl rand -hex 3)
TOKEN_SECRET=$(openssl rand -hex 8)
export BOOTSTRAP_TOKEN="${TOKEN_ID}.${TOKEN_SECRET}"

# 24h TTL. macOS shown below. Linux uses: date -u -d "+24 hours" +"%Y-%m-%dT%H:%M:%SZ"
EXPIRATION=$(date -u -v+24H +"%Y-%m-%dT%H:%M:%SZ")
echo "$BOOTSTRAP_TOKEN  expires $EXPIRATION"

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
```

Three ClusterRoleBindings. The first lets the bootstrapper group create CSRs, the second auto-approves the kubelet client CSRs, and the third grants `system:node` so the kubelet can post status once it has its certificate.

```bash
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

The same token and the same three bindings work for any number of nodes joining the same cluster.

## Provision the EC2 instance

Look up the latest Ubuntu 24.04 amd64 AMI from Canonical (owner ID `099720109477`). Hardcoding an AMI ID is a footgun because Canonical refreshes them roughly weekly.

```bash
AMI_ID=$(aws ec2 describe-images --region "$AWS_REGION" \
  --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
            "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
echo "AMI_ID=$AMI_ID"
```

Create a security group locked to your IP. EC2's default egress is wide open, which is what the agent needs for pulling Kubernetes binaries, container images, and reaching the AKS API server.

```bash
VPC_ID=$(aws ec2 describe-vpcs --region "$AWS_REGION" \
  --filters "Name=is-default,Values=true" --query 'Vpcs[0].VpcId' --output text)

SG_ID=$(aws ec2 create-security-group --region "$AWS_REGION" \
  --group-name "$SG_NAME" --description "AKSFlexNode test" \
  --vpc-id "$VPC_ID" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP"
aws ec2 create-tags --region "$AWS_REGION" --resources "$SG_ID" \
  --tags Key=Project,Value=AKSFlexNode

aws ec2 import-key-pair --region "$AWS_REGION" --key-name "$KEY_NAME" \
  --public-key-material fileb://$HOME/.ssh/id_rsa.pub
```

Launch with 50 GB of `gp3` because the default 8 GB fills up during the container image cache phase.

```bash
INSTANCE_ID=$(aws ec2 run-instances --region "$AWS_REGION" \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SG_ID" \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=50,VolumeType=gp3}' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=flexnode-ec2-01},{Key=Project,Value=AKSFlexNode}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
EC2_IP=$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "INSTANCE_ID=$INSTANCE_ID  EC2_IP=$EC2_IP"
```

Wait for SSH. `aws ec2 wait` only checks the EC2 control plane, not the OS, so I loop with a short backoff.

```bash
for i in {1..20}; do
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 ubuntu@$EC2_IP 'echo ready' 2>/dev/null && break
  sleep 6
done

ssh ubuntu@$EC2_IP 'uname -a; free -g; df -h /'
```

Expected output, with the AWS kernel suffix.

```
Linux ip-172-31-18-36 6.17.0-1015-aws #15~24.04.1-Ubuntu SMP Thu May  7 17:00:14 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
               total        used        free      shared  buff/cache   available
Mem:               3           0           3           0           0           3
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        48G  1.8G   46G   4% /
```

SSH user on the Canonical Ubuntu AMI is `ubuntu`.

## Build the agent config

The agent reads a single JSON config. The `azure.*` section still has to be present even though the host runs on AWS, because those fields identify the target AKS cluster (subscription, tenant, resource ID), not the host cloud. `node.kubelet.serverURL` and `caCertData` are how kubelet finds and trusts the API server without ever needing Azure metadata.

```bash
SUBSCRIPTION=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
AKS_RESOURCE_ID=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query id -o tsv)
LOCATION=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query location -o tsv)
K8S_VERSION=$(az aks show -g "$CLUSTER_RG" -n "$CLUSTER_NAME" --query kubernetesVersion -o tsv)
SERVER_URL=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA_CERT_DATA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

cat > /tmp/aksflex-ec2-config.json <<EOF
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

scp /tmp/aksflex-ec2-config.json ubuntu@$EC2_IP:/tmp/config.json
```

The upstream usage doc shows `"version": "1.30.0"` in the config example. Don't copy that literally. Use your actual cluster's Kubernetes version, otherwise the kubelet binary the agent downloads will be skewed too far from the apiserver and the join breaks.

## Install the agent

The installer script is `scripts/install.sh` in the repo. It downloads the binary from the latest GitHub release, drops it at `/usr/local/bin/aks-flex-node`, installs Azure CLI as a dependency, and creates `/etc/aks-flex-node/`. Pass `--yes` for non-interactive SSH, otherwise it reaches for `/dev/tty` for an Arc-related prompt and the install fails with `/dev/tty: No such device or address`.

```bash
ssh ubuntu@$EC2_IP 'bash -s' <<'REMOTE'
set -e
curl -fsSL https://raw.githubusercontent.com/Azure/AKSFlexNode/main/scripts/install.sh -o /tmp/install.sh
sudo bash /tmp/install.sh --yes
sudo aks-flex-node version

sudo mkdir -p /etc/aks-flex-node /var/log/aks-flex-node
sudo cp /tmp/config.json /etc/aks-flex-node/config.json
sudo chmod 600 /etc/aks-flex-node/config.json
REMOTE
```

Version output you should see.

```
AKS Flex Node Agent
Version: v0.0.20
Git Commit: 10872d9
Build Time: 2026-04-27T21:03:01Z
```

## Start the agent

The v0.0.20 release binary uses `aks-flex-node agent`, not `bootstrap` as the docs say. I start it as a transient systemd unit using `systemd-run`, same as the project's own e2e tests at https://github.com/Azure/AKSFlexNode/blob/main/hack/e2e/lib/node-join.sh.

```bash
ssh ubuntu@$EC2_IP 'bash -s' <<'REMOTE'
set -euo pipefail
sudo systemctl stop aks-flex-node-token 2>/dev/null || true
sudo systemctl reset-failed aks-flex-node-token 2>/dev/null || true

sudo systemd-run \
  --unit=aks-flex-node-token \
  --description="AKS Flex Node (token)" \
  --remain-after-exit \
  /usr/local/bin/aks-flex-node agent --config /etc/aks-flex-node/config.json

sleep 90
sudo systemctl status aks-flex-node-token --no-pager -l | head -15
sudo tail -n 30 /var/log/aks-flex-node/aks-flex-node.log
REMOTE
```

The agent runs 13 steps in order. `install-arc` (skipped because Arc is disabled), `configure-os`, `disable-docker`, `enrich-cluster-config`, `download-cni-binaries`, `download-cri-binaries` (containerd + runc), `download-kube-binaries` (kubelet, kubectl, kubeadm, kube-proxy from `dl.k8s.io`), `download-npd` (node problem detector), `configure-cni`, `start-containerd`, `configure-iptables`, `start-kubelet`, `start-npd`. On my `t3.medium` in `ap-southeast-1` the whole sequence took roughly 1 minute 46 seconds, with `download-kube-binaries` alone accounting for 95 seconds.

The lines you want to see at the tail of the log.

```
level=info msg="bootstrap step: start-kubelet completed successfully with duration 396.922651ms"
level=info msg="bootstrap step: start-npd completed successfully with duration 47.216835ms"
level=info msg="AKS node bootstrap completed successfully (duration: 1m45.632241208s, stepCount: 13)"
level=info msg="Bootstrap completed successfully, transitioning to daemon mode..."
```

There is also one benign warning you'll see right after.

```
level=warning msg="Failed to collect initial managed cluster spec: failed to get AKS managed cluster via SDK: AzureCLICredential: ERROR: Please run 'az login' to setup account."
```

That comes from the periodic spec collector trying to enrich status using Azure CLI auth. In token mode there is no `az login` on the EC2 instance, so the collector fails and gives up gracefully. The node still works, pods still schedule, kubelet still posts status. Ignore it.

## Verify the node joined

Run `kubectl get nodes -o wide` from your workstation.

```
$ kubectl get nodes -o wide
NAME                                STATUS   ROLES    AGE   VERSION   INTERNAL-IP    EXTERNAL-IP   OS-IMAGE             KERNEL-VERSION      CONTAINER-RUNTIME
aks-gitlabpv2-10971719-vmss000000   Ready    <none>   25m   v1.34.7   10.0.7.7       <none>        Ubuntu 24.04.4 LTS   6.8.0-1052-azure    containerd://2.1.6-2
aks-nodepool1-27425191-vmss000005   Ready    <none>   32m   v1.34.7   10.0.1.4       <none>        Ubuntu 22.04.5 LTS   5.15.0-1110-azure   containerd://1.7.31-1
aks-userpool1-42241789-vmss00000b   Ready    <none>   32m   v1.34.7   10.0.7.4       <none>        Ubuntu 24.04.4 LTS   6.8.0-1052-azure    containerd://2.1.6-2
aks-userpool1-42241789-vmss00000c   Ready    <none>   29m   v1.34.7   10.0.7.5       <none>        Ubuntu 24.04.4 LTS   6.8.0-1052-azure    containerd://2.1.6-2
aks-userpool1-42241789-vmss00000d   Ready    <none>   26m   v1.34.7   10.0.7.6       <none>        Ubuntu 24.04.4 LTS   6.8.0-1052-azure    containerd://2.1.6-2
flexnode-vm-01                      Ready    <none>   27m   v1.34.7   10.0.0.4       <none>        Ubuntu 24.04.4 LTS   6.17.0-1013-azure   containerd://2.0.4
ip-172-31-18-36                     Ready    <none>   38s   v1.34.7   172.31.18.36   <none>        Ubuntu 24.04.4 LTS   6.17.0-1015-aws     containerd://2.0.4
```

Last row is the EC2 instance. `ip-172-31-18-36` is the AWS-default hostname, `172.31.18.36` is the EC2 private IP inside the default VPC, and the `6.17.0-1015-aws` kernel string is proof that an AWS-hosted node is now serving an Azure-managed cluster. Status `Ready`, version matches the rest of the cluster at `v1.34.7`.

Smoke test with a pinned `pause` pod.

```bash
kubectl run flexnode-ec2-smoke \
  --image=registry.k8s.io/pause:3.10 --restart=Never \
  --overrides='{"spec":{"nodeName":"ip-172-31-18-36","tolerations":[{"operator":"Exists"}]}}'

sleep 25
kubectl get pod flexnode-ec2-smoke -o wide
```

```
NAME                 READY   STATUS    RESTARTS   AGE   IP           NODE              NOMINATED NODE   READINESS GATES
flexnode-ec2-smoke   1/1     Running   0          36s   10.244.0.3   ip-172-31-18-36   <none>           <none>
```

The pod IP `10.244.0.3` comes from the bridge CNI's local pod CIDR on that node, not from your cluster pod network. Pods on the EC2 node can't reach pods on the AKS-managed nodes without an overlay CNI (Calico VXLAN cluster-wide, for example) or a VPN between the AWS VPC and your Azure VNet. For a single-node demo or for workloads that only need to talk to the API server and external services it's fine.

Taint the flex node so AKS-managed DaemonSets (kube-proxy variants, CSI drivers, Konnectivity agent) don't try to schedule onto it.

```bash
kubectl taint node ip-172-31-18-36 dedicated=flex:NoSchedule --overwrite
```

## References

- https://github.com/Azure/AKSFlexNode
- https://github.com/Azure/AKSFlexNode/blob/main/docs/usage.md
- https://github.com/Azure/AKSFlexNode/blob/main/docs/design.md
- https://github.com/Azure/AKSFlexNode/blob/main/hack/e2e/lib/node-join-token.sh
- https://kubernetes.io/docs/reference/access-authn-authz/bootstrap-tokens/
- https://kubernetes.io/docs/reference/access-authn-authz/kubelet-tls-bootstrapping/

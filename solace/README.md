# Installing the Solace PubSub+ Event Broker on AKS

End-to-end install of a high-availability **Solace PubSub+ Event Broker** (Enterprise) on AKS using the Solace operator and Azure **Ultra SSD** for the message-spool disk. Verified with operator `1.4.0`, broker `10.25.15.4705`, and AKS Kubernetes `1.30+`.

The three manifests in this directory are deliberately tiny and meant to be applied in order:

| File | Purpose |
| --- | --- |
| `deploy.yaml` | Solace `pubsubplus-eventbroker-operator` v1.4.0 — namespace, CRD, RBAC, controller Deployment. Pulled straight from the upstream operator release. |
| `ultradisk.yml` | A `StorageClass` named `ultra-disk-sc` backed by `UltraSSD_LRS` via the Azure Disk CSI driver. |
| `broker.yml` | A `PubSubPlusEventBroker` CR (`ha-example`) — HA (3-node) redundancy group pinned to nodes labelled `workload=solace`, with PVCs carved out of `ultra-disk-sc`. |

> The broker image in `broker.yml` is `eggboy/solace-pubsub-enterprise:10.25.15.4705` — a private Docker Hub mirror. Swap in `solace/solace-pubsub-enterprise:10.25.15.4705` (public, but rate-limited) or your own registry as needed. The `regcred` pull secret reference in `deploy.yaml` and the broker spec is the join point.

## Prereqs

### 1. An AKS cluster with an Ultra-SSD-capable node pool dedicated to Solace

Ultra Disk only attaches to specific VM families (`Dsv3`, `Dsv4`, `Dsv5`, `Esv3`, `Esv4`, `Esv5`, etc.) **and** the node pool must be created with `--enable-ultra-ssd` in a zone that has Ultra Disk capacity. You cannot toggle Ultra SSD on after the pool is created.

```bash
export RG=aks-solace-rg
export CLUSTER=solace-aks
export LOCATION=southeastasia            # pick a region with Ultra Disk in at least one zone
export VMSIZE=Standard_D8s_v5            # 8 vCPU / 32 GiB — fits broker default request

az aks nodepool add \
  --resource-group "$RG" --cluster-name "$CLUSTER" \
  --name solacepool \
  --node-count 3 \
  --node-vm-size "$VMSIZE" \
  --zones 1 2 3 \
  --enable-ultra-ssd \
  --node-taints "workload=solace:NoSchedule" \
  --labels workload=solace
```

The HA broker is three pods (Primary, Backup, Monitor). With `--node-count 3` plus the taint/label, each broker pod lands on its own dedicated VM and nothing else gets co-scheduled there. The label/taint key `workload=solace` is the one `broker.yml` selects on — change both sides together if you rename it.

If you forget `--enable-ultra-ssd`, broker pods stay `Pending` with `failed to provision volume … UltraSSD_LRS is not supported on this node pool`.

### 2. `kubectl` pointing at the cluster

```bash
az aks get-credentials -g "$RG" -n "$CLUSTER" --overwrite-existing
kubectl get nodes -l workload=solace -o wide
```

You should see three `Ready` nodes carrying both the `workload=solace` label and the matching `NoSchedule` taint.

### 3. A Docker Hub pull secret named `regcred`

Both the operator Deployment in `deploy.yaml` (line ~2024) and the broker image in `broker.yml` reference `regcred`. The operator looks for it in its own namespace (`pubsubplus-operator-system`); the broker pods look for it in **whatever namespace you deploy the CR into** (`default` if you don't change it). You need it in both places.

```bash
export DH_USER=...           # your Docker Hub username
export DH_TOKEN=...          # PAT, not your password
export DH_EMAIL=...

# Created after Step 1 below so the operator namespace exists.
for NS in pubsubplus-operator-system default; do
  kubectl create secret docker-registry regcred \
    --namespace "$NS" \
    --docker-server=https://index.docker.io/v1/ \
    --docker-username="$DH_USER" \
    --docker-password="$DH_TOKEN" \
    --docker-email="$DH_EMAIL" \
    --dry-run=client -o yaml | kubectl apply -f -
done
```

Skip this if your broker image is fully public **and** you delete the `imagePullSecrets: [regcred]` block from `deploy.yaml` first. Most people will just create the secret.

## Step 1 — Install the operator

```bash
kubectl apply -f deploy.yaml
```

This applies, in one shot:

- `Namespace/pubsubplus-operator-system`
- `CustomResourceDefinition/pubsubpluseventbrokers.pubsubplus.solace.com` (v1beta1)
- `ServiceAccount`, `Role`, `ClusterRole`, `RoleBinding`, `ClusterRoleBinding` for the controller
- `Deployment/pubsubplus-eventbroker-operator` (image `docker.io/solace/pubsubplus-eventbroker-operator:1.4.0`, `replicas: 1`, leader-elect on)

Wait for the controller to come up.

```bash
kubectl -n pubsubplus-operator-system rollout status deploy/pubsubplus-eventbroker-operator --timeout=180s
kubectl -n pubsubplus-operator-system get pods
```

`WATCH_NAMESPACE` in the operator is set to `""`, so a single operator instance watches `PubSubPlusEventBroker` CRs across every namespace in the cluster. You only need one.

## Step 2 — Create the Ultra Disk StorageClass

```bash
kubectl apply -f ultradisk.yml
kubectl get sc ultra-disk-sc
```

Key fields and why they are what they are:

- `provisioner: disk.csi.azure.com` — the in-tree `kubernetes.io/azure-disk` provisioner is gone in modern AKS; CSI is the only path.
- `volumeBindingMode: WaitForFirstConsumer` — Ultra Disk is **zonal**. Binding has to wait until the broker pod is scheduled so the PV can be provisioned in the same zone as the node, otherwise the pod fails to mount.
- `skuname: UltraSSD_LRS` — the storage tier itself.
- `diskIopsReadWrite: "5000"` / `diskMbpsReadWrite: "200"` — sized to fit the broker's default PVC and a `D8s_v5` per-VM disk throughput cap (4 IOPS/GiB floor needs ≥17 GiB to hit 5000 IOPS, which is well under the default broker spool size). Bump these for larger workloads; just keep them under your chosen VM family's per-VM Ultra Disk cap, otherwise attach will succeed but throttling will hit.

## Step 3 — Deploy the broker

```bash
kubectl apply -f broker.yml
```

Watch the broker reconcile. The operator creates a StatefulSet, three pods, the internal services, and the PVCs.

```bash
kubectl get pubsubpluseventbroker ha-example -w
# in another shell:
kubectl get pods,svc,pvc -l app.kubernetes.io/instance=ha-example
```

Expected steady state once HA assembly completes (usually 3–6 minutes the first time, since the broker image is large and Ultra Disks have to be provisioned per zone):

```
NAME                          READY   STATUS    RESTARTS   AGE
ha-example-pubsubplus-0       1/1     Running   0          5m   # Primary
ha-example-pubsubplus-1       1/1     Running   0          5m   # Backup
ha-example-pubsubplus-2       1/1     Running   0          5m   # Monitor

NAME                                       TYPE           CLUSTER-IP     EXTERNAL-IP   PORT(S)
service/ha-example-pubsubplus              LoadBalancer   10.0.x.x       <pending|IP>  8080:..,55555:..,...
service/ha-example-pubsubplus-discovery    ClusterIP      None           <none>        8080/TCP,...

NAME                                          STATUS   VOLUME    CAPACITY   STORAGECLASS
persistentvolumeclaim/data-ha-example-...-0   Bound    pvc-...   30Gi       ultra-disk-sc
persistentvolumeclaim/data-ha-example-...-1   Bound    pvc-...   30Gi       ultra-disk-sc
persistentvolumeclaim/data-ha-example-...-2   Bound    pvc-...   10Gi       ultra-disk-sc   # Monitor (override in broker.yml)
```

The Monitor uses 10 GiB because `spec.storage.monitorNodeStorageSize: "10Gi"` overrides the default — the Monitor is voting-only, it doesn't hold message state, so 10 GiB is plenty.

## Step 4 — Get the admin password and connect

The operator generates the admin password into a Secret on first reconcile.

```bash
ADMIN_PW=$(kubectl get secret ha-example-pubsubplus-secrets \
  -o jsonpath='{.data.username_admin_password}' | base64 -d)
echo "admin password: $ADMIN_PW"
```

If your `LoadBalancer` service has an external IP, point a browser at `https://<EXTERNAL-IP>:8080` and log in as `admin`. Otherwise port-forward:

```bash
kubectl port-forward svc/ha-example-pubsubplus 8080:8080
# http://localhost:8080 — admin / $ADMIN_PW
```

## Verifying HA

```bash
kubectl exec ha-example-pubsubplus-0 -- /usr/sw/loads/currentload/bin/cli -A \
  -es "show redundancy"
```

You want to see `Config Status: Enabled`, `Redundancy Status: Up`, `ADB Link To Mate: Up`, `ADB Hello To Mate: Up`, and the Primary as `Active`/`Local Active`.

## Cleanup

```bash
kubectl delete -f broker.yml          # broker, PVCs (PVs follow Delete reclaim policy)
kubectl delete -f ultradisk.yml       # StorageClass
kubectl delete -f deploy.yaml         # operator + CRD + namespace

az aks nodepool delete -g "$RG" --cluster-name "$CLUSTER" --name solacepool
```

Delete in this order. Deleting the CRD first orphans the broker and leaves dangling PVCs.

## Troubleshooting

- **Broker pods `Pending` with `0/N nodes are available: had taint {workload: solace}`.** Your Solace node pool isn't tainted/labelled the way `broker.yml` expects, or you forgot to create it. Check `kubectl get nodes -l workload=solace`.
- **PVC stays `Pending` with `failed to provision … UltraSSD_LRS`.** The node pool was created without `--enable-ultra-ssd`, or the chosen zone has no Ultra Disk capacity. Rebuild the pool with the flag, or pick different zones (`--zones 1 2 3`).
- **`ImagePullBackOff` on broker pods.** Either the broker image is in a private registry and you haven't created `regcred` in the broker's namespace, or your Docker Hub PAT lacks `read` scope. The operator and broker pods need the secret in different namespaces — easy to do one and forget the other.
- **Operator pod `CrashLoopBackOff` with leader-election errors.** Usually a stale lease from a previous install. `kubectl -n pubsubplus-operator-system delete lease pubsubplus-eventbroker-operator-leader-election` and let the Deployment restart.

## References

- Solace PubSub+ Kubernetes Operator: <https://github.com/SolaceProducts/pubsubplus-kubernetes-quickstart>
- Operator CRD reference: <https://docs.solace.com/Software-Broker/Kubernetes-Operator-Reference.htm>
- Azure Ultra Disks on AKS: <https://learn.microsoft.com/azure/aks/use-ultra-disks>
- Azure Disk CSI StorageClass parameters: <https://github.com/kubernetes-sigs/azuredisk-csi-driver/blob/master/docs/driver-parameters.md>

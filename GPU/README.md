# Configuring GPU on AKS

Create AKS cluster with GPU node pool.

https://techcommunity.microsoft.com/t5/azure-high-performance-computing/running-gpu-accelerated-workloads-with-nvidia-gpu-operator-on/ba-p/4061318


```shell
$ az aks create \
    --name gpu-cluster \
    --resource-group sandbox-rg \
    --ssh-key-value ~/.ssh/id_rsa.pub \
    --enable-aad \
    --node-count 1 \
    --node-vm-size Standard_DS3_v2 \
    --network-plugin azure \
    --network-policy calico \
    --node-osdisk-type Ephemeral \
    --max-pods 110 \
    --service-cidr 10.10.0.0/24 \
    --dns-service-ip 10.10.0.10 \
    --load-balancer-backend-pool-type=nodeIP

$ az aks nodepool add --resource-group sandbox-rg --cluster-name gpu-cluster --name nc24ads --node-taints sku=gpu:NoSchedule --node-vm-size Standard_NC24ads_A100_v4 --node-count 1
```

## Install node-feature-discovery

Nvidia node-feature-discovery is used to discover GPU nodes. It is a requirement for GPU Operator.

```shell
$ helm install --wait --create-namespace -n gpu-operator node-feature-discovery node-feature-discovery --create-namespace --repo https://kubernetes-sigs.github.io/node-feature-discovery/charts --set-json master.config.extraLabelNs='["nvidia.com"]' --set-json worker.tolerations='[{ "effect": "NoSchedule", "key": "sku", "operator": "Equal", "value": "gpu"},{"effect": "NoSchedule", "key": "mig", "value":"notReady", "operator": "Equal"}]'
```

NodeFeatureRule is a custom resource definition (CRD) that is used to match the nodes based on the labels.

```shell
$ kubectl apply -f - <<EOF
apiVersion: nfd.k8s-sigs.io/v1alpha1
kind: NodeFeatureRule
metadata:
  name: nfd-gpu-rule
  namespace: gpu-operator
spec:
   rules:
   - name: "nfd-gpu-rule"
     labels:
        "feature.node.kubernetes.io/pci-10de.present": "true"
     matchFeatures:
        - feature: pci.device
          matchExpressions:
            vendor: {op: In, value: ["10de"]}
EOF
```

## Install GPU Operator

Since Node Feature Discovery and drivers are installed, we can skip the installation of them.

```shell
$ helm install --wait gpu-operator -n gpu-operator nvidia/gpu-operator --set-json daemonsets.tolerations='[{ "effect": "NoSchedule", "key": "sku", "operator": "Equal", "value": "gpu"}]' --set nfd.enabled=false --set driver.enabled=false --set operator.runtimeClass=nvidia-container-runtime
```

## Configuring Time Slicing

Nvidia 
```shell
$ az aks nodepool update --cluster-name rbac-cluster --resource-group sandbox-rg --nodepool-name nc24ads --labels "nvidia.com/device-plugin.config=nvidia-a100"

$ kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: time-slicing-config-all
  namespace: gpu-operator
data:
  any: |-
    version: v1
    flags:
      migStrategy: none
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
EOF

$ kubectl patch clusterpolicies.nvidia.com/cluster-policy -n gpu-operator --type merge -p '{"spec": {"devicePlugin": {"config": {"name": "time-slicing-config-all", "default": "any"}}}}'
```

Once cluster-policy is changed, Both gpu-feature-discovery and nvidia-device-plugin-daemonset pods will be restarted. If it doesnt' restart, restart the pods manually.

```shell
$ kubectl get events -n gpu-operator --sort-by='.lastTimestamp'
```

Validate the GPU configuration. We should see the GPU replicas set to 4.

```shell
$ kubectl describe node aks-nc24ads-14356471-vmss000000 | grep replica
      nvidia.com/gpu.replicas=4     
```

## Configuring MIG

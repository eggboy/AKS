# Pod Identity with AKS

AAD Pod Identity is supported as an add-on for AKS clusters. https://docs.microsoft.com/en-us/azure/aks/use-azure-ad-pod-identity

```shell
export RG=${RG}
export CLUSTER_NAME=aks119
export IDENTITY_NAME=kvidentity
```
## Enable Pod Identity on AKS Cluster

Pod Identity can be enabled on existing cluster using `--enable-pod-identity`

```shell
$ az aks update -g ${RG} -n rbac-cluster --enable-pod-identity
```

Create Managed Identity that will be used by Pod Identity. Then, add it to the cluster. 

```shell
$ az identity create -g ${RG} -n ${IDENTITY_NAME} -o json
{
"clientId": "...",
"clientSecretUrl": "...",
"id": "/subscriptions/...",
"location": "southeastasia",...

$ export AAD_IDENTITY_RESOURCE_ID="$(az identity show -g ${RG} -n ${IDENTITY_NAME} --query id -otsv)"
$ az aks pod-identity add --resource-group ${RG} --cluster-name ${CLUSTER_NAME} --namespace default --name ${IDENTITY_NAME} --identity-resource-id /subscriptions/.../resourcegroups/.../providers/Microsoft.ManagedIdentity/userAssignedIdentities/kvidentity

$ kubectl get azureidentity,azureidentitybinding
```

## Use Key Vault CSI Driver with Pod Identity

We will use nginx with Key Vault CSI Driver to store the certificates.

## Create Key Vault

Create Key Vault, and set policy to allow managed identity to get certificate from it.

```shell
$ az keyvault create -g ${RG} -n nginx-kv -l southeastasia --enabled-for-template-deployment true
$ az keyvault set-policy -n nginx-kv --certificate-permissions get --object-id $AAD_IDENTITY_PRINCIPALID
```

## Create AzureIdentity, AzureIdentity Binding 

```shell


az aks pod-identity add --resource-group ${RG} --cluster-name ${PREFIX}-aks --namespace default --name akv-identity --identity-resource-id ${AAD_IDENTITY_RESOURCE_ID}

# Take a look at AAD Resources
kubectl get azureidentity,azureidentitybinding -n default
```

## Spring Boot with Key Vault

```shell
$ az keyvault set-policy -n nginx-kv --secret-permissions get list--object-id $AAD_IDENTITY_PRINCIPALID
```

## Nginx with Key Vault CSI Driver

https://docs.microsoft.com/en-us/azure/key-vault/general/key-vault-integrate-kubernetes

```shell
$ helm repo add csi-secrets-store-provider-azure https://raw.githubusercontent.com/Azure/secrets-store-csi-driver-provider-azure/master/charts
$ helm install csi csi-secrets-store-provider-azure/csi-secrets-store-provider-azure
```
## SecretProviderClass

```yaml
apiVersion: secrets-store.csi.x-k8s.io/v1alpha1
kind: SecretProviderClass
metadata:
  name: azure-tls
spec:
  provider: azure
  secretObjects:
    - secretName: ingress-tls-csi # Kuberentes secret 'ingress-tls-csi' will be created with key and cert. 
      type: kubernetes.io/tls
      data:
        - objectName: tls-cert
          key: tls.key
        - objectName: tls-cert
          key: tls.crt
  parameters:
    usePodIdentity: "true"
    keyvaultName: "nginx-kv"
    objects: |
      array:
        - |
          objectName: tls-cert
          objectType: secret
    tenantId: "" # the tenant ID of the KeyVault
```

## Install Nginx using Helm 
```shell
$ helm install nginx-ingress ingress-nginx/ingress-nginx \
    --set controller.replicaCount=2 \
    --set controller.nodeSelector."beta\.kubernetes\.io/os"=linux \
    --set defaultBackend.nodeSelector."beta\.kubernetes\.io/os"=linux \
    --set controller.podLabels.aadpodidbinding=akv-identity \
    -f - <<EOF
controller:
  extraVolumes:
      - name: secrets-store-inline
        csi:
          driver: secrets-store.csi.k8s.io
          readOnly: true
          volumeAttributes:
            secretProviderClass: "azure-tls"
  extraVolumeMounts:
      - name: secrets-store-inline
        mountPath: "/mnt/secrets-store"
        readOnly: true
```

## Create Ingress
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tls-example-ingress
spec:
  tls:
    - hosts:
        - clientip.jaylee.io
      secretName: ingress-tls-csi
  rules:
    - host: clientip.jaylee.io
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: clientip
                port:
                  number: 8080
```
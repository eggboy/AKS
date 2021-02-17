# Pod Identity with AKS

AAD Pod Identity is supported as an add-on for AKS clusters. https://docs.microsoft.com/en-us/azure/aks/use-azure-ad-pod-identity

## Enable Pod Identity on existing AKS Cluster

```shell
$ az aks update -g sandbox-rg -n rbac-cluster --enable-pod-identity
```

Create Managed Identity that will be used by Pod Identity. Then, add it to the cluster. 

```shell
$ az identity create -g sandbox-rg -n aks120-kv -o json
{
"clientId": "...",
"clientSecretUrl": "...",
"id": "/subscriptions/...",
"location": "southeastasia",...

$ az aks pod-identity add --resource-group sandbox-rg --cluster-name aks120 --namespace default --name akv-identity --identity-resource-id /subscriptions/.../resourcegroups/.../providers/Microsoft.ManagedIdentity/userAssignedIdentities/aks120-kv

$ kubectl get azureidentity,azureidentitybinding
```

## Use Key Vault CSI Driver with Pod Identity

We will use nginx with Key Vault CSI Driver to store the certificates.

## Install Key Vault CSI Driver

https://docs.microsoft.com/en-us/azure/key-vault/general/key-vault-integrate-kubernetes

```shell
$ helm repo add csi-secrets-store-provider-azure https://raw.githubusercontent.com/Azure/secrets-store-csi-driver-provider-azure/master/charts
$ helm install csi csi-secrets-store-provider-azure/csi-secrets-store-provider-azure
```

## Create Key Vault

Create Key Vault, and set policy to allow managed identity to get certificate from it.

```shell
$ az keyvault create -g  sandbox-rg -n nginx-kv -l southeastasia --enabled-for-template-deployment true
$  az keyvault set-policy -n nginx-kv --certificate-permissions get --object-id []
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
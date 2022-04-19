# Pod Identity with AKS

Azure Active Directory (Azure AD) pod-managed identities use Kubernetes primitives to associate managed identities for Azure resources and identities in Azure AD with pods. AAD Pod Identity is supported as an add-on for AKS clusters. https://docs.microsoft.com/en-us/azure/aks/use-azure-ad-pod-identity

```shell
export RESOURCE_GROUP=sandbox-rg
export CLUSTER_NAME=aks119
export IDENTITY_NAME=kvidentity
```

## Enable Pod Identity on AKS Cluster

Pod Identity can be enabled on existing cluster using `--enable-pod-identity`

```shell
$ az aks update -g ${RESOURCE_GROUP} -n {CLUSTER_NAME} --enable-pod-identity
```

Create Managed Identity that will be used by Pod Identity. Then, add it to the cluster. You can add multiple Managed Identity to the cluster. 

```shell
$ az identity create -g ${RESOURCE_GROUP} -n ${IDENTITY_NAME} -o json
{
"clientId": "...",
"clientSecretUrl": "...",
"id": "/subscriptions/...",
"location": "southeastasia",...

$ export AAD_IDENTITY_RESOURCE_ID="$(az identity show -g ${RG} -n ${IDENTITY_NAME} --query id -otsv)"
$ az aks pod-identity add --resource-group ${RG} --cluster-name ${CLUSTER_NAME} --namespace default --name ${IDENTITY_NAME} --identity-resource-id ${AAD_IDENTITY_RESOURCE_ID}
```

Check if two CRDs, azureidentity,azureidentitybinding are created. 

```
$ kubectl get azureidentity,azureidentitybinding
```

# Azure Key Vault with Managed Identity on AKS

We have enabled pod identity and configured our cluster with managed identity. For app to make use of MI, you need to label it properly. This is example of yaml for sample app hosted at https://github.com/eggboy/keyvault-mi, aadpodidbinding in the label is to bound Managed Identity with the pod. 

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: keyvault-mi
  labels:
    app: keyvault-mi
spec:
  replicas: 1
  template:
    metadata:
      name: keyvault-mi
      labels:
        aadpodidbinding: jay-managedidentity
        app: keyvault-mi
    spec:
      containers:
        - name: keyvault-mi
          image: eggboy/keyvault-mi:0.0.3
          imagePullPolicy: Always
          env:
            - name: MANAGED_IDENTITY_CLIENT_ID
              value:
            - name: KEYVAULT_URI
              value:
      restartPolicy: Always
  selector:
    matchLabels:
      app: keyvault-mi
```

# Nginx with AKS Secret Store CSI Driver

The Azure Key Vault Provider for Secrets Store CSI Driver allows for the integration of an Azure key vault as a secrets store with an Azure Kubernetes Service (AKS) cluster via a CSI volume. https://docs.microsoft.com/en-us/azure/aks/csi-secrets-store-driver

```shell
$ az aks enable-addons --addons azure-keyvault-secrets-provider --name ${CLUSTER_NAME} --resource-group ${RESOURCE_GROUP}
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

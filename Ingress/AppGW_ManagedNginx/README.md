# Configuring Managed Nginx Ingress Controller with Azure Application Gateway

This document provides a guide on how to configure a Managed Nginx Ingress Controller with Azure Application Gateway in an Azure Kubernetes Service (AKS) cluster. The setup includes using Azure Key Vault for TLS certificates and configuring the ingress resources.
In the example, I assume that you have two subdomains, `external-subdomain1.jaylee.cloud` and `external-subdomain2.jaylee.cloud`, which will be used to route traffic to two different applications running in the AKS cluster. Certificates for these subdomains are stored in Azure Key Vault, and the ingress controller is configured to use these certificates for TLS termination.

## Configure Managed Nginx Ingress Controller

### Enable Application Routing Add-on on AKS

You can enable the Application Routing add-on when creating an AKS cluster or update an existing cluster.

[Enable on a new cluster or existing cluster](https://learn.microsoft.com/en-us/azure/aks/app-routing#enable-application-routing-using-azure-cli)

### Create Internal NGINX Ingress Controller

Create an internal NGINX Ingress Controller using the `NginxIngressController` custom resource definition (CRD). This controller will handle internal traffic routing within the AKS cluster.

```yaml
apiVersion: approuting.kubernetes.azure.com/v1alpha1
kind: NginxIngressController
metadata:
  name: nginx-internal
spec:
  ingressClassName: nginx-internal
  controllerNamePrefix: nginx-internal
  loadBalancerAnnotations:
    service.beta.kubernetes.io/azure-load-balancer-internal: "true"
```

## Ingress with subdomain and Path based routing

Be aware that `kubernetes.azure.com/tls-cert-keyvault-uri` annotation creates a secret in the cluster with the name `keyvault-<ingress-name>`.

This is an example of how to configure the ingress resources for two applications, `external-app1` and `external-app2`, with path-based routing and TLS termination using Azure Key Vault.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: external-app2-a
spec:
  selector:
    app: external-app2-a
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8080
  type: ClusterIP
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    kubernetes.azure.com/tls-cert-keyvault-uri: https://kv-jay-eastus.vault.azure.net/certificates/aks-ingress-jaylee-cloud-cert
    nginx.ingress.kubernetes.io/rewrite-target: /
  name: external-app2
spec:
  ingressClassName: nginx-internal
  rules:
    - host: external-subdomain2.jaylee.cloud
      http:
        paths:
          - backend:
              service:
                name: external-app2
                port:
                  number: 80
            path: /
            pathType: Prefix
          - path: /patha
            pathType: Prefix
            backend:
              service:
                name: external-app2-a
                port:
                  number: 80
  tls:
    - hosts:
        - external-subdomain2.jaylee.cloud
      secretName: keyvault-external-app2

```

## Application Gateway 

Have a look at the Application Gateway components in the diagram below. There are numerous components to be configured to make App Gateway work, and diagram will guide you through the process whenever you're lost. 

This will help you understand how the Application Gateway 

![Application Gateway Components](img/appgw_components.png)

### Listener

The listener is the entry point for the Application Gateway. It listens for incoming traffic on a specific port and forwards it to the appropriate backend pool based on the rules defined.

![Application Gateway Listener](img/listener.png)

### Backend Pool

Here, backend pool is set to the private IP of the NGINX Ingress Controller. 

![Application Gateway Backend Pool](img/backendpools.png)

### Backend Settings

![Application Gateway Backend Settings](img/backendsetting.png)

### Custom Health Probe

As we use wildcard host type in the Listener, we need to set a custom health probe to check the health of the NGINX Ingress Controller.
Ideally, you could setup a custom health probe for Nginx ingress controller itself, but in this example, I use one of the ingress of the application. 

![Application Gateway Health Probe](img/healthprobe.png)

### Rules

![Application Gateway Health Probe](img/rules.png)

### Test

Access the application using the subdomain `external-subdomain2.jaylee.cloud` and you should see the response from the application.

![Application Gateway Health Probe](img/test.png)
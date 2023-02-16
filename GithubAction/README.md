# Accessing AKS with kubelogin in Github Action

This is a sample project to show how to access AKS with kubelogin in Github Action. It's composed of multiple steps, 1. Use Federated Identity to az login 2. Use az aks get-credentials to create KUBECONFIG 3. Use kubelogin to login to AKS in non-interactive mode. 


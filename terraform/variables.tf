variable "resource_group" {
  default     = "sandbox-rg"
  description = "Resource group for all resources."
  type        = string
}

variable "cluster_name" {
  default     = "rbac-cluster"
  description = "Resource group for all resources."
  type        = string
}

variable "location" {
  default     = "southeastasia"
  description = "The Azure Region in which all resources will be provisioned in"
  type        = string
}

variable "kubernetes_version" {
  default     = "1.29.4"
  description = "The version of Kubernetes you want deployed to your cluster. Please reference the command: az aks get-versions --location eastus -o table"
  type        = string
}

variable "public_ssh_key_path" {
  default     = "~/.ssh/id_rsa.pub"
  description = "The Path at which your Public SSH Key is located. Defaults to ~/.ssh/id_rsa.pub"
  type        = string
}

variable "systempool_vm_sku" {
  default     = "Standard_D2plds_v5"
  description = "The Node type and size based on Azure VM SKUs Reference: az vm list-sizes --location eastus -o table"
  type        = string
}

variable "userpool_vm_sku" {
  default     = "Standard_D4ds_v4"
  description = "The Node type and size based on Azure VM SKUs Reference: az vm list-sizes --location eastus -o table"
  type        = string
}

variable "network_plugin" {
  default     = "azure"
  description = ""
  type        = string
}

variable "network_policy" {
  default     = "cilium"
  description = "Uses calico by default for network policy"
  type        = string
}

variable "network_data_plane" {
    default     = "cilium"
    description = ""
    type        = string
}

variable "service_cidr" {
  default     = "192.168.0.0/16"
  description = "The IP address CIDR block to be assigned to the service created inside the Kubernetes cluster. If connecting to another peer or to you On-Premises network this CIDR block MUST NOT overlap with existing BGP learned routes"
  type        = string
}

variable "dns_service_ip" {
  default     = "192.168.0.10"
  description = "The IP address that will be assigned to the CoreDNS or KubeDNS service inside of Kubernetes for Service Discovery. Must start at the .10 or higher of the svc-cidr range"
  type        = string
}
#
# variable "aks_subnet_prefix" {
#   default     = "aks"
#   description = "Resource group for all resources."
# }

# variable "ilb_subnet_prefix" {
#   default     = "ilb"
#   description = "Resource group for all resources."
# }
#
# variable "appgw_subnet_prefix" {
#   default     = "appgw"
#   description = "Resource group for all resources."
# }

variable "log_analytics_name" {
  default     = "la-eastus"
  description = "The IP address CIDR block to be assigned to the service created inside the Kubernetes cluster. If connecting to another peer or to you On-Premises network this CIDR block MUST NOT overlap with existing BGP learned routes"
  type        = string
}

variable "acr_name" {
  default     = "crjay"
  description = "The IP address CIDR block to be assigned to the service created inside the Kubernetes cluster. If connecting to another peer or to you On-Premises network this CIDR block MUST NOT overlap with existing BGP learned routes"
  type        = string
}


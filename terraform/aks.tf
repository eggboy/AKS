data "azurerm_log_analytics_workspace" "example" {
  name                = var.log_analytics_name
  resource_group_name = "sandbox-rg"
}

data "azurerm_subnet" "systempool" {
  name                 = "snet-nodepool-1"
  virtual_network_name = "vnet-aks-sg"
  resource_group_name  = var.resource_group
}

data "azurerm_subnet" "userpool" {
  name                 = "snet-nodepool-2"
  virtual_network_name = "vnet-aks-sg"
  resource_group_name  = var.resource_group
}

data "azurerm_subnet" "pods1" {
  name                 = "snet-pods-1"
  virtual_network_name = "vnet-aks-sg"
  resource_group_name  = var.resource_group
}

resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.cluster_name
  location            = var.location
  dns_prefix          = var.cluster_name
  resource_group_name = var.resource_group
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Standard"

  azure_active_directory_role_based_access_control {
    managed            = "true"
    azure_rbac_enabled = "false"
  }

  network_profile {
    network_plugin = var.network_plugin
    network_policy = var.network_policy
    service_cidr   = var.service_cidr
    dns_service_ip = var.dns_service_ip
    network_data_plane = var.network_data_plane
  }

  default_node_pool {
    name           = "systempool"
    node_count     = 2
    vm_size        = var.systempool_vm_sku
    max_pods       = 110
    only_critical_addons_enabled = true
    # vnet_subnet_id               = element(tolist(azurerm_virtual_network.default.subnet), 0).id
    vnet_subnet_id = data.azurerm_subnet.systempool.id
    pod_subnet_id  = data.azurerm_subnet.pods1.id
  }

  linux_profile {
    admin_username = "azureuser"

    ssh_key {
      key_data = file(var.public_ssh_key_path)
    }
  }

  identity {
    type = "SystemAssigned"
  }

  microsoft_defender {
    log_analytics_workspace_id = data.azurerm_log_analytics_workspace.example.id
  }

  azure_policy_enabled = true

  oms_agent {
    log_analytics_workspace_id = data.azurerm_log_analytics_workspace.example.id
  }

  oidc_issuer_enabled = true

  key_vault_secrets_provider {
    secret_rotation_enabled = true
  }

  #   web_app_routing {
  #     dns_zone_ids = []
  #   }

  workload_autoscaler_profile {
    keda_enabled = true
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "example" {
  name                  = "userpool"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = var.userpool_vm_sku
  node_count            = 3
  vnet_subnet_id        = data.azurerm_subnet.userpool.id
  pod_subnet_id         = data.azurerm_subnet.pods1.id
}

data "azurerm_container_registry" "acr" {
  name                = var.acr_name
  resource_group_name = var.resource_group
}

resource "azurerm_role_assignment" "acr" {
  role_definition_name             = "AcrPull"
  scope                            = data.azurerm_container_registry.acr.id
  principal_id                     = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

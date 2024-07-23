# resource "azurerm_virtual_network" "default" {
#   name                = "${var.cluster_name}-vnet"
#   resource_group_name = var.resource_group
#   location            = var.location
#   address_space       = ["10.0.0.0/8"]
# }
#
# resource "azurerm_subnet" "aks" {
#   name                 = "${var.aks_subnet_prefix}-subnet"
#   resource_group_name  = var.resource_group
#   virtual_network_name = azurerm_virtual_network.default.name
#   address_prefixes     = ["10.240.0.0/16"]
# }
#
# resource "azurerm_subnet" "ilb" {
#   name                 = "${var.ilb_subnet_prefix}-subnet"
#   resource_group_name  = var.resource_group
#   virtual_network_name = azurerm_virtual_network.default.name
#   address_prefixes     = ["10.242.0.0/16"]
# }
#
# resource "azurerm_subnet" "appgw" {
#   name                 = "${var.appgw_subnet_prefix}-subnet"
#   resource_group_name  = var.resource_group
#   virtual_network_name = azurerm_virtual_network.default.name
#   address_prefixes     = ["10.241.0.0/16"]
# }

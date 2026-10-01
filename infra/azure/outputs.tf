output "public_ip" {
  value = azurerm_public_ip.main.ip_address
}

output "ssh" {
  value = "ssh -i <private-key> azureuser@${azurerm_public_ip.main.ip_address}"
}

output "kube_tunnel" {
  description = "Run this, keep it open, then use a kubeconfig pointing at https://127.0.0.1:6443"
  value       = "ssh -i <private-key> -N -L 6443:127.0.0.1:6443 azureuser@${azurerm_public_ip.main.ip_address}"
}

variable "subscription_id" {
  description = "Azure subscription id (az account show --query id -o tsv)"
  type        = string
}

variable "allowed_ip" {
  description = "Your public IPv4. SSH is opened to this address only."
  type        = string
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key used to log in to the VM"
  type        = string
}

variable "location" {
  type    = string
  default = "swedencentral"
}

variable "vm_size" {
  description = "Standard_B2as_v2 by default; Standard_D2as_v5 is the fallback (both allowed in swedencentral)"
  type        = string
  default     = "Standard_B2as_v2"
}

variable "shutdown_time" {
  description = "Daily automatic shutdown, HHMM, Tunisia time"
  type        = string
  default     = "2300"
}

variable "k3s_version" {
  type    = string
  default = "v1.30.6+k3s1"
}

variable "prefix" {
  type    = string
  default = "incidentpilot"
}

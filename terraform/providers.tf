provider "proxmox" {
  pm_debug        = true
  pm_tls_insecure = true
}

terraform {
  required_version = ">= 0.14"
  required_providers {
    proxmox = {
      source  = "telmate/proxmox"
      version = "3.0.1-rc8"
    }
  }
}

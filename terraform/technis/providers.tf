provider "proxmox" {
  pm_api_token_id     = data.sops_file.sops-secret.data["pm_api_token_id"]
  pm_api_token_secret = data.sops_file.sops-secret.data["pm_api_token_secret"]
  pm_api_url          = data.sops_file.sops-secret.data["pm_api_url"]
  pm_debug            = true
  pm_tls_insecure     = true
}

provider "sops" {}

terraform {
  required_version = ">= 0.14"
  required_providers {
    sops = {
      source  = "carlpett/sops"
      version = "1.0.0"
    }
    proxmox = {
      source  = "telmate/proxmox"
      version = "3.0.1-rc8"
    }
  }
}

data "sops_file" "sops-secret" {
  source_file = "secret.sops.json"
}
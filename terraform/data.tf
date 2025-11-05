data "sops_file" "sops_secret" {
  source_file = "secret.sops.json"
}

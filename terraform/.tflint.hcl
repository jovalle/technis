# TFLint configuration for Terraform
# Documentation: https://github.com/terraform-linters/tflint

config {
  # Module inspection is enabled by default for terraform plugin
  call_module_type = "local"

  # Force the provider source to be set
  force = false

  # Disable recursive module inspection (can be slow)
  # disabled_by_default = false
}

# AWS plugin (if using AWS)
plugin "aws" {
  enabled = false  # Set to false to avoid credential issues when not using AWS
  version = "0.36.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"

  # Deep checking enabled (slower but more thorough)
  deep_check = true
}

# Terraform plugin (base rules)
plugin "terraform" {
  enabled = true
  version = "0.10.0"
  source  = "github.com/terraform-linters/tflint-ruleset-terraform"

  preset = "recommended"
}

# Proxmox plugin (if using Proxmox provider)
# plugin "proxmox" {
#   enabled = true
#   version = "0.1.0"
#   source  = "github.com/terraform-linters/tflint-ruleset-proxmox"
# }

# Rules configuration
rule "terraform_deprecated_interpolation" {
  enabled = true
}

rule "terraform_deprecated_index" {
  enabled = true
}

rule "terraform_unused_declarations" {
  enabled = true
}

rule "terraform_comment_syntax" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_typed_variables" {
  enabled = true
}

rule "terraform_module_pinned_source" {
  enabled = true
  style   = "semver" # or "flexible"
}

rule "terraform_naming_convention" {
  enabled = true

  # Naming convention formats
  variable {
    format = "snake_case"
  }

  locals {
    format = "snake_case"
  }

  output {
    format = "snake_case"
  }

  resource {
    format = "snake_case"
  }

  module {
    format = "snake_case"
  }

  data {
    format = "snake_case"
  }
}

rule "terraform_required_version" {
  enabled = true
}

rule "terraform_required_providers" {
  enabled = true
}

rule "terraform_standard_module_structure" {
  enabled = true
}

rule "terraform_workspace_remote" {
  enabled = true
}

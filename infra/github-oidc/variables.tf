variable "region" {
  description = "AWS region for the provider (IAM is global; region just configures the provider)."
  type        = string
  default     = "us-east-1"
}

variable "github_owner" {
  description = "GitHub org/user that owns the repo, e.g. jushy10."
  type        = string
}

variable "github_repo" {
  description = "Repository name, e.g. nama_backend."
  type        = string
}

variable "create_oidc_provider" {
  description = <<-EOT
    Create the GitHub OIDC provider. An AWS account can have only ONE provider
    for token.actions.githubusercontent.com — set this false if it already
    exists (it will be looked up instead).
  EOT
  type        = bool
  default     = true
}

variable "allowed_github_subjects" {
  description = <<-EOT
    Which GitHub workflow runs may assume the role, matched against the OIDC
    `sub` claim (supports `*`). Default restricts to pull_request runs of this
    repo — the only thing that needs to `terraform plan`. Examples:
      repo:OWNER/REPO:pull_request                 (PRs — default)
      repo:OWNER/REPO:ref:refs/heads/main          (pushes to main)
      repo:OWNER/REPO:environment:production       (a GitHub environment)
    Leave null to use the default built from owner/repo.
  EOT
  type        = list(string)
  default     = null
}

variable "role_name" {
  description = "Name of the IAM role GitHub Actions will assume."
  type        = string
  default     = "github-actions-terraform-plan"
}

# --- Optional: remote state backend permissions -----------------------------
# Plan must READ the state object and acquire/release the lock. Fill these in
# once you switch infra/ to the S3 backend (see infra/versions.tf). Leave blank
# while using a local backend.

variable "state_bucket" {
  description = "S3 bucket holding Terraform state (for backend read access). Blank to skip."
  type        = string
  default     = ""
}

variable "lock_table" {
  description = "DynamoDB state-lock table name. Blank to skip."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}

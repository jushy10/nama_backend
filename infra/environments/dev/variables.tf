variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (used in tags)."
  type        = string
  default     = "dev"
}

variable "domain_name" {
  description = "Public hostname for the app."
  type        = string
  default     = "api.namainsights.com"
}

variable "parent_domain" {
  description = "Registered domain / Route 53 hosted zone."
  type        = string
  default     = "namainsights.com"
}

variable "create_hosted_zone" {
  description = "Create the hosted zone. false = use existing (domain registered via Route 53)."
  type        = bool
  default     = false
}

variable "bastion_enabled" {
  description = "Keep the SSM bastion (the laptop->database tunnel host) provisioned. Parked (stopped) by default — see bastion_desired_state — so it costs only ~$0.64/mo (disk) until you start it on demand. Set false to remove it entirely. Not in the app's serving path — never affects the API."
  type        = bool
  default     = true
}

variable "bastion_desired_state" {
  description = "Power state Terraform holds the bastion in. Defaults to \"stopped\" so the box is parked (~$0.64/mo, disk only) unless you need it — start it on demand with infra/bastion.ps1 (a manual start persists until the next terraform apply reconciles it back). Set \"running\" to keep it up continuously across applies (~$7/mo). Only takes effect while bastion_enabled = true."
  type        = string
  default     = "stopped"

  validation {
    condition     = contains(["running", "stopped"], var.bastion_desired_state)
    error_message = "bastion_desired_state must be \"running\" or \"stopped\"."
  }
}

variable "bastion_auto_stop_idle_minutes" {
  description = "Auto-stop the bastion after this many minutes of near-idle CPU — a safety net for a manual `bastion.ps1 up` that's left running. Only armed while bastion_desired_state = \"stopped\" (in always-on mode the box is meant to stay up). Set 0 to disable."
  type        = number
  default     = 30
}

variable "frontend_domain_name" {
  description = "Apex hostname the frontend SPA is served at."
  type        = string
  default     = "namainsights.com"
}

variable "frontend_additional_domains" {
  description = "Extra hostnames that also serve the frontend (e.g. www)."
  type        = list(string)
  default     = ["www.namainsights.com"]
}

variable "frontend_canonical_domain" {
  description = "Canonical frontend hostname. Requests to any other served hostname (the apex) are 301-redirected here at the CloudFront edge — so namainsights.com sends visitors to www.namainsights.com. Must be one of frontend_domain_name / frontend_additional_domains. Null = serve every hostname directly with no redirect."
  type        = string
  default     = "www.namainsights.com"
}

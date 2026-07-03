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
  description = "Keep the SSM bastion (the laptop->database tunnel host) provisioned. It is parked STOPPED between sessions (~$0.64/mo, disk only) and started for a bounded window by the 'Bastion session' workflow. Set false to remove it entirely. Not in the app's serving path — never affects the API."
  type        = bool
  default     = true
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

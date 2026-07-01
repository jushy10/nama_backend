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

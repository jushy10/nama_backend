variable "parent_domain" {
  description = "The registered domain / hosted zone, e.g. namainsights.com."
  type        = string
}

variable "domain_name" {
  description = "The hostname to issue the certificate for, e.g. api.namainsights.com."
  type        = string
}

variable "create_zone" {
  description = "Create the Route 53 hosted zone. false = use an existing one (e.g. domain bought via Route 53)."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Extra tags (merged with the environment's default_tags)."
  type        = map(string)
  default     = {}
}

variable "parent_domain" {
  description = "The registered domain / hosted zone, e.g. namainsights.com."
  type        = string
}

variable "domain_name" {
  description = "The hostname to issue the certificate for, e.g. api.namainsights.com."
  type        = string
}

variable "subject_alternative_names" {
  description = "Extra hostnames to add to the same certificate, e.g. [\"www.namainsights.com\"]. The cert then covers domain_name + these."
  type        = list(string)
  default     = []
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

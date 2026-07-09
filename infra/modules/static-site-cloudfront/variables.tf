variable "name" {
  description = "Name prefix for the bucket and a label for the distribution."
  type        = string
}

variable "domain_name" {
  description = "Primary hostname the site is served at (e.g. namainsights.com)."
  type        = string
}

variable "additional_domain_names" {
  description = "Extra hostnames that also serve the site (e.g. [\"www.namainsights.com\"]). Must be covered by certificate_arn."
  type        = list(string)
  default     = []
}

variable "certificate_arn" {
  description = "ACM certificate ARN covering domain_name + additional_domain_names. MUST be issued in us-east-1 — CloudFront only reads certs from there."
  type        = string
}

variable "redirect_to_domain" {
  description = "Canonical host: if set, any request whose Host header isn't this hostname is 301-redirected to https://<this>/<same path+query> (e.g. apex namainsights.com -> www.namainsights.com). Must be one of domain_name / additional_domain_names, so the cert covers it and the distribution serves it. Null (default) = serve every alias directly, no redirect."
  type        = string
  default     = null
}

variable "route53_zone_id" {
  description = "Hosted zone ID for the alias records. Null = create no DNS records."
  type        = string
  default     = null
}

variable "default_root_object" {
  description = "Object returned for requests to the root path (the SPA entrypoint)."
  type        = string
  default     = "index.html"
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 (US/Canada/Europe edges) is the cheapest; _200 adds Asia, _All is global."
  type        = string
  default     = "PriceClass_100"
}

variable "tags" {
  description = "Extra tags (merged with the environment's default_tags)."
  type        = map(string)
  default     = {}
}

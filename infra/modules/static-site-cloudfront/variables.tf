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

variable "backend_origin_domain_name" {
  description = "Optional custom-origin hostname (e.g. the API's api.namainsights.com) that specific path patterns are routed to, so server-rendered/dynamic routes are served by the app behind the SAME distribution (and hostname) as the static SPA — this is what lets the SEO content pages inherit the site's authority instead of sitting on a separate host. Null (default) = static-only, no backend origin, behaves exactly as before."
  type        = string
  default     = null
}

variable "backend_path_patterns" {
  description = "Path patterns routed to backend_origin_domain_name (e.g. [\"/stock/*\", \"/sitemap.xml\", \"/robots.txt\", \"/llms.txt\"]). Each becomes an ordered cache behavior ahead of the SPA default. Ignored when backend_origin_domain_name is null."
  type        = list(string)
  default     = []
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

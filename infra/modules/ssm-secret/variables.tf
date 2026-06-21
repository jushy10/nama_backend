variable "name" {
  description = "Full SSM parameter name, e.g. /nama/dev/alpaca-api-key-id."
  type        = string
}

variable "description" {
  description = "Human-readable description of the parameter."
  type        = string
  default     = null
}

variable "placeholder" {
  description = "Initial value Terraform creates the parameter with. Overwrite with the real secret out of band; Terraform ignores value changes thereafter. Keep it ASCII — the value may be injected into an HTTP header before it's replaced, and non-ASCII bytes break latin-1 header encoding."
  type        = string
  default     = "REPLACE_ME_VIA_PUT_PARAMETER"
}

variable "tags" {
  description = "Extra tags, merged on top of the environment's default_tags."
  type        = map(string)
  default     = {}
}

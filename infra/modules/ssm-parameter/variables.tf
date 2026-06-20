variable "name" {
  description = "Full SSM parameter name, e.g. /nama/hello."
  type        = string
}

variable "value" {
  description = "Parameter value."
  type        = string
}

variable "description" {
  description = "Human-readable description of the parameter."
  type        = string
  default     = null
}

variable "type" {
  description = "Parameter type: String, StringList, or SecureString."
  type        = string
  default     = "String"
}

variable "tier" {
  description = "Standard (free) or Advanced."
  type        = string
  default     = "Standard"
}

variable "tags" {
  description = "Extra tags, merged on top of the environment's default_tags."
  type        = map(string)
  default     = {}
}

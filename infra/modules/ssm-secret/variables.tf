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
  description = "Initial value Terraform creates the parameter with. Overwrite with the real secret out of band; Terraform ignores value changes thereafter."
  type        = string
  default     = "REPLACE_ME — set via: aws ssm put-parameter --overwrite"
}

variable "tags" {
  description = "Extra tags, merged on top of the environment's default_tags."
  type        = map(string)
  default     = {}
}

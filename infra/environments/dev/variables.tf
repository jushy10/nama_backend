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

variable "greeting" {
  description = "Demo value stored in SSM Parameter Store."
  type        = string
  default     = "hello from terraform"
}

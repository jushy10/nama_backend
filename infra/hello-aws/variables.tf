variable "region" {
  description = "AWS region to connect to."
  type        = string
  default     = "us-east-1"
}

variable "greeting" {
  description = "Value stored in SSM Parameter Store — change it and re-apply to see Terraform UPDATE a resource."
  type        = string
  default     = "hello from terraform"
}

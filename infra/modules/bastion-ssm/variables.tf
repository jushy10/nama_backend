variable "name" {
  description = "Name prefix for the bastion and its IAM / security-group resources."
  type        = string
}

variable "vpc_id" {
  description = "VPC to launch the bastion in (the same VPC as the database)."
  type        = string
}

variable "subnet_id" {
  description = <<-EOT
    Subnet for the bastion. A public subnet is fine — the box opens no inbound
    ports — and a public IP lets the SSM agent reach the Systems Manager
    endpoints over the internet gateway, so no NAT gateway or VPC endpoints are
    needed.
  EOT
  type        = string
}

variable "extra_security_group_ids" {
  description = <<-EOT
    Extra security groups to attach to the instance. Pass the database's
    app_security_group_id here so the bastion is allowed to reach Postgres on
    5432.
  EOT
  type    = list(string)
  default = []
}

variable "instance_type" {
  description = "Instance size. t4g.nano (arm64) is the cheapest and ample for a jump host."
  type        = string
  default     = "t4g.nano"
}

variable "tags" {
  description = "Extra tags, merged on top of the environment's default_tags."
  type        = map(string)
  default     = {}
}

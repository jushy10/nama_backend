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

variable "auto_stop_idle_minutes" {
  description = <<-EOT
    Auto-stop the box after this many minutes of near-idle CPU — a safety net for
    a manual start that's forgotten, so a parked bastion can't quietly run up a
    bill. Implemented as a CloudWatch alarm with the built-in EC2 stop action (no
    Lambda, no agent). 0 disables it. A running box carrying real tunnel traffic
    keeps CPU above auto_stop_cpu_threshold_percent and won't trip; the tradeoff
    is that a tunnel left open but idle for the whole window is also stopped
    (reconnect is one command).
  EOT
  type        = number
  default     = 0
}

variable "auto_stop_cpu_threshold_percent" {
  description = "Average CPU% (over 5-min datapoints) at or below which the box counts as idle for auto_stop_idle_minutes. Only used when auto_stop_idle_minutes > 0."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Extra tags, merged on top of the environment's default_tags."
  type        = map(string)
  default     = {}
}

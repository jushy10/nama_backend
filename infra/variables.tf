variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix applied to created resources."
  type        = string
  default     = "nama"
}

variable "environment" {
  description = "Deployment environment (used in names/tags)."
  type        = string
  default     = "prod"
}

# --- Networking -------------------------------------------------------------
# RDS lives inside YOUR VPC. Point these at private subnets (no internet route)
# in at least two Availability Zones. The DB never gets a public IP.

variable "vpc_id" {
  description = "ID of the VPC the database will live in."
  type        = string
}

variable "db_subnet_ids" {
  description = "Private subnet IDs (>= 2, in different AZs) for the DB subnet group."
  type        = list(string)

  validation {
    condition     = length(var.db_subnet_ids) >= 2
    error_message = "Provide at least two subnets in different AZs."
  }
}

variable "allowed_app_security_group_ids" {
  description = <<-EOT
    Security Group IDs allowed to connect to the database on port 5432.
    Attach these SGs to your application (ECS task, EC2, Lambda, App Runner VPC
    connector). Connections are permitted SG-to-SG — no IP ranges, nothing from
    the public internet.
  EOT
  type        = list(string)
  default     = []
}

# --- Database ---------------------------------------------------------------

variable "db_name" {
  description = "Initial database name created inside the instance."
  type        = string
  default     = "nama"
}

variable "db_username" {
  description = "Master username. Its password is generated and stored in Secrets Manager by RDS."
  type        = string
  default     = "nama_app"
}

variable "engine_version" {
  description = "PostgreSQL major.minor version."
  type        = string
  default     = "16.4"
}

variable "parameter_group_family" {
  description = "Parameter group family, must match the engine major version (e.g. postgres16)."
  type        = string
  default     = "postgres16"
}

variable "instance_class" {
  description = "Instance size. db.t4g.micro is the cheapest Graviton option."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Storage in GiB (autoscales up to max_allocated_storage)."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Upper bound for storage autoscaling in GiB."
  type        = number
  default     = 100
}

variable "multi_az" {
  description = "Run a standby in a second AZ for failover. Doubles cost; leave off for dev."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "How many days of automated backups to keep."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Block accidental `terraform destroy` / console deletion of the DB."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Extra tags applied to all resources."
  type        = map(string)
  default     = {}
}

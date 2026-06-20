variable "name" {
  description = "Name prefix for the database and its resources."
  type        = string
}

variable "vpc_id" {
  description = "VPC the database lives in."
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the DB subnet group (need >= 2 in different AZs)."
  type        = list(string)
}

variable "db_name" {
  description = "Initial database name created inside the instance."
  type        = string
  default     = "nama"
}

variable "username" {
  description = "Master username. Its password is generated and stored in SSM."
  type        = string
  default     = "nama_app"
}

variable "engine_major" {
  description = "PostgreSQL major version; the latest matching minor is selected."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "Instance size. db.t4g.micro is free-tier eligible."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Storage in GiB."
  type        = number
  default     = 20
}

variable "backup_retention_days" {
  description = "Days of automated backups to keep."
  type        = number
  default     = 7
}

variable "multi_az" {
  description = "Run a standby in a second AZ (doubles cost). Off for dev."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Block accidental deletion. Off by default so dev can be destroyed."
  type        = bool
  default     = false
}

variable "database_url_ssm_name" {
  description = "SSM SecureString parameter name to store the SQLAlchemy URL in."
  type        = string
  default     = "/nama/database-url"
}

variable "tags" {
  description = "Extra tags (merged with the environment's default_tags)."
  type        = map(string)
  default     = {}
}

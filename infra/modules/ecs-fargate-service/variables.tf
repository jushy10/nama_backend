variable "name" {
  description = "Name for the cluster, service, ECR repo, etc."
  type        = string
}

variable "vpc_id" {
  description = "VPC to run in."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for the ALB and the Fargate tasks (>= 2 AZs)."
  type        = list(string)
}

variable "app_security_group_id" {
  description = "Security group that the database accepts — attached to tasks so they can reach the DB."
  type        = string
}

variable "database_url_ssm_arn" {
  description = "ARN of the SSM SecureString holding DATABASE_URL; injected into the container."
  type        = string
}

variable "container_port" {
  description = "Port the app listens on inside the container."
  type        = number
  default     = 8000
}

variable "desired_count" {
  description = "Number of task copies to run."
  type        = number
  default     = 1
}

variable "cpu" {
  description = "Task CPU units (256 = 0.25 vCPU)."
  type        = number
  default     = 256
}

variable "memory" {
  description = "Task memory in MiB."
  type        = number
  default     = 512
}

variable "health_check_path" {
  description = "HTTP path the ALB pings to check task health."
  type        = string
  default     = "/healthz"
}

variable "image_tag" {
  description = "ECR image tag the service runs."
  type        = string
  default     = "latest"
}

variable "tags" {
  description = "Extra tags (merged with the environment's default_tags)."
  type        = map(string)
  default     = {}
}

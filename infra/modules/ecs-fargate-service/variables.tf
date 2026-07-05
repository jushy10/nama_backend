variable "name" {
  description = "Name for the cluster, service, ECR repo, etc."
  type        = string
}

variable "vpc_id" {
  description = "VPC to run in."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for the Fargate tasks and the API Gateway VPC Link ENIs (>= 2 AZs)."
  type        = list(string)
}

variable "app_security_group_id" {
  description = "Security group that the database accepts — attached to tasks so they can reach the DB. Null for a service that needs no database (e.g. a static frontend)."
  type        = string
  default     = null
}

variable "database_url_ssm_arn" {
  description = "ARN of the SSM SecureString holding DATABASE_URL; injected into the container. Null = no DATABASE_URL secret (e.g. a static frontend)."
  type        = string
  default     = null
}

variable "extra_secrets" {
  description = "Additional secrets injected as container env vars: a map of ENV_VAR_NAME => SSM parameter ARN. The execution role is granted read on each, and they're injected alongside DATABASE_URL."
  type        = map(string)
  default     = {}
}

variable "extra_environment" {
  description = "Plain (non-secret) environment variables injected into the container: a map of ENV_VAR_NAME => value. Use extra_secrets for sensitive values."
  type        = map(string)
  default     = {}
}

variable "enable_bedrock_invoke" {
  description = "Grant the task role permission to invoke Anthropic Claude models on Amazon Bedrock (for the AI stock-analysis endpoint). Off by default so the module stays generic; the consuming service opts in."
  type        = bool
  default     = false
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

variable "sync_cpu" {
  description = "CPU units for the out-of-band sync task (run via `aws ecs run-task`, not the service). 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "sync_memory" {
  description = "Memory (MiB) for the out-of-band sync task. Larger than the API task on purpose — a heavy sweep (the ~2,800-row universe screen) needs headroom, but only for the minutes it runs, so it lives on a separate on-demand task instead of bloating the always-on service. Must form a valid Fargate CPU/memory pair with sync_cpu (256 CPU allows 512/1024/2048)."
  type        = number
  default     = 2048
}

variable "health_check_path" {
  description = "HTTP path the container health check pings; ECS replaces tasks that fail it."
  type        = string
  default     = "/healthz"
}

variable "image_tag" {
  description = "ECR image tag the service runs."
  type        = string
  default     = "latest"
}

variable "certificate_arn" {
  description = "ACM cert ARN for the API's custom domain (used when domain_name is set). Must be issued in this region."
  type        = string
  default     = null
}

variable "domain_name" {
  description = "Custom hostname for the API (e.g. api.namainsights.com), served HTTPS-only. Null = default execute-api endpoint only."
  type        = string
  default     = null
}

variable "route53_zone_id" {
  description = "Hosted zone ID for the domain_name record."
  type        = string
  default     = null
}

variable "tags" {
  description = "Extra tags (merged with the environment's default_tags)."
  type        = map(string)
  default     = {}
}

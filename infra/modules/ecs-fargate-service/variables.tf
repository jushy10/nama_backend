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
  description = "Number of task copies to run. When enable_autoscaling is true this is only the INITIAL count — autoscaling owns the running count thereafter (the service ignores later desired_count changes), so bounds are set by autoscaling_min/max_capacity."
  type        = number
  default     = 1
}

variable "enable_autoscaling" {
  description = "Attach ECS target-tracking autoscaling to the service (scales the task count on average CPU). Off by default so the module stays generic; the consuming service opts in. When on, the service's desired_count is managed by autoscaling between autoscaling_min_capacity and autoscaling_max_capacity."
  type        = bool
  default     = false
}

variable "autoscaling_min_capacity" {
  description = "Minimum task count when enable_autoscaling is true. Keep at 1 so idle cost is unchanged (still one always-on task)."
  type        = number
  default     = 1
}

variable "autoscaling_max_capacity" {
  description = "Maximum task count when enable_autoscaling is true. Cap sized against the DB connection budget: each task holds up to (DB_POOL_SIZE + DB_MAX_OVERFLOW) connections, so max_tasks * that + the sync task's pool must stay under RDS max_connections (~112 on db.t4g.micro)."
  type        = number
  default     = 3
}

variable "autoscaling_cpu_target" {
  description = "Target average CPU utilization (percent) for the target-tracking policy. The service scales out to hold CPU near this; 60 leaves headroom for a burst before a new task is warm."
  type        = number
  default     = 60
}

variable "apigw_throttle_rate_limit" {
  description = "API Gateway stage steady-state throttle (requests/second across all clients). A global cost + load ceiling under which the per-IP app limiter sits; raise alongside autoscaling_max_capacity."
  type        = number
  default     = 50
}

variable "apigw_throttle_burst_limit" {
  description = "API Gateway stage burst throttle (concurrent request bucket). Paired with apigw_throttle_rate_limit."
  type        = number
  default     = 100
}

variable "cpu_architecture" {
  description = "CPU architecture for BOTH Fargate task definitions (app + sync). ARM64 (Graviton) is ~20% cheaper per vCPU-hour than X86_64. Must match the architecture the pushed image was built for (see the build job in app-image.yml) — a mismatch fails at container start with 'exec format error'."
  type        = string
  default     = "ARM64"

  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "cpu_architecture must be ARM64 or X86_64."
  }
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
  description = "Memory (MiB) for the out-of-band sync task. Larger than the API task on purpose — a heavy sweep (the ~2,800-row universe screen + its per-ticker enrichment pass) needs headroom, but only for the minutes it runs, so it lives on a separate on-demand task instead of bloating the always-on service. Must form a valid Fargate CPU/memory pair with sync_cpu (256 CPU allows 512/1024/2048)."
  type        = number
  default     = 1024
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

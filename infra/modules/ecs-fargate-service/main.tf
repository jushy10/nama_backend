data "aws_region" "current" {}

locals {
  # Every SSM secret ARN the execution role must be able to read
  # (DATABASE_URL, if set, plus any extra_secrets).
  secret_arns = concat(
    var.database_url_ssm_arn == null ? [] : [var.database_url_ssm_arn],
    values(var.extra_secrets),
  )

  # Container "secrets" entries: env var name -> SSM ARN. Map iteration is
  # key-sorted, so the order is stable across plans (no perpetual diff).
  container_secrets = concat(
    var.database_url_ssm_arn == null ? [] : [
      { name = "DATABASE_URL", valueFrom = var.database_url_ssm_arn }
    ],
    [for env_name, arn in var.extra_secrets : { name = env_name, valueFrom = arn }],
  )

  # Container "environment" entries: plain (non-secret) env var name -> value.
  # Key-sorted iteration keeps the order stable across plans (no perpetual diff).
  container_environment = [
    for env_name, value in var.extra_environment : { name = env_name, value = value }
  ]
}

# ---------------------------------------------------------------------------
# Container image registry. CI builds the app image and pushes it here; the
# task definition below pulls "<repo>:<image_tag>".
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # let `terraform destroy` remove it even with images

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

# Housekeeping so old images don't pile up (ECR charges for storage):
#  1. Untagged images (orphaned layers) are removed after 7 days.
#  2. Keep only the 10 most recent images overall — this prunes old tagged
#     builds while always keeping the newest (which `latest` points at), so the
#     running image is never deleted. A blanket "expire everything after 7 days"
#     would delete the live image if you went a week without deploying.
resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the 10 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "this" {
  name_prefix       = "/ecs/${var.name}-"
  retention_in_days = 14
  tags              = var.tags
}

# ---------------------------------------------------------------------------
# IAM. Two roles:
#  - execution role: used by the ECS agent to pull the image, write logs, and
#    read the DATABASE_URL secret to inject it.
#  - task role: the app's own runtime identity (no AWS calls today, but ready).
# Role names start with "nama-" so the CI policy can stay scoped to nama-* roles.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to read the injected SecureString secrets (and
# decrypt them). Only created when at least one secret is configured — a static
# frontend has none.
data "aws_iam_policy_document" "execution_secrets" {
  count = length(local.secret_arns) == 0 ? 0 : 1

  statement {
    actions   = ["ssm:GetParameters"]
    resources = local.secret_arns
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"] # the default aws/ssm key
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = length(local.secret_arns) == 0 ? 0 : 1

  name_prefix = "secrets-"
  role        = aws_iam_role.execution.id
  policy      = data.aws_iam_policy_document.execution_secrets[0].json
}

# This policy gained a `count` (so it can be skipped for the DB-less frontend),
# which moves its address from [no index] to [0]. Tell Terraform it's the same
# resource so the existing backend policy migrates instead of being recreated.
moved {
  from = aws_iam_role_policy.execution_secrets
  to   = aws_iam_role_policy.execution_secrets[0]
}

resource "aws_iam_role" "task" {
  name_prefix        = "${var.name}-task-"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# Optional: let the app (its task role) invoke Anthropic Claude models on Amazon
# Bedrock for the AI stock-analysis endpoint. Bedrock authenticates as the task,
# so there is no API key to store — this policy is what grants access. Scoped to
# the Anthropic foundation models plus the cross-region inference profiles that
# front them (newer models are invoked through a profile, not the bare model id).
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "task_bedrock" {
  count = var.enable_bedrock_invoke ? 1 : 0

  statement {
    sid = "InvokeAnthropicModels"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*.anthropic.*",
    ]
  }
}

resource "aws_iam_role_policy" "task_bedrock" {
  count = var.enable_bedrock_invoke ? 1 : 0

  name_prefix = "bedrock-"
  role        = aws_iam_role.task.id
  policy      = data.aws_iam_policy_document.task_bedrock[0].json
}

# ---------------------------------------------------------------------------
# Networking & ingress: an API Gateway HTTP API reaches the tasks through a
# VPC Link (free for HTTP APIs), discovering live task IPs via Cloud Map.
# This replaced a public ALB: an ALB bills every hour plus two public IPv4
# addresses (~$24/mo to front a single task), while an HTTP API bills per
# request (~$1/M) — effectively $0 at dev traffic. Two trade-offs came with it:
#   - a HARD 30s integration timeout (the ALB idled at ~60s): long requests —
#     the Bedrock analysis endpoint, the /internal/*/sync cron batches — must
#     finish inside it or the caller gets a 504 while the app keeps working
#     (the sync workflows size their batches to fit);
#   - HTTPS only: there is no port-80 listener, so plain http://<domain> no
#     longer answers (the old ALB redirected it).
# Tasks also carry app_security_group_id so the database accepts them.
# ---------------------------------------------------------------------------

# The VPC Link's ENIs. No ingress rules needed — the link only originates
# connections to the tasks; nothing dials the link's ENIs directly.
resource "aws_security_group" "vpc_link" {
  name_prefix = "${var.name}-link-"
  description = "API Gateway VPC Link ENIs - egress to the app tasks"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "service" {
  name_prefix = "${var.name}-svc-"
  description = "App tasks: traffic from the API Gateway VPC Link only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "App port from the VPC Link"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.vpc_link.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# Cloud Map service discovery: ECS registers every running task's private IP
# and port here, and the API Gateway integration resolves live instances from
# it (via DiscoverInstances, not DNS). SRV records on purpose — ECS only
# registers the *port* attribute for SRV services, and API Gateway needs it
# to know where to connect (with A records it would assume port 80).
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name}.local"
  vpc  = var.vpc_id
  tags = var.tags
}

resource "aws_service_discovery_service" "this" {
  name = "app"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id

    dns_records {
      type = "SRV"
      ttl  = 10
    }

    routing_policy = "MULTIVALUE"
  }

  # "Custom" health = ECS reports task health into Cloud Map (driven by the
  # container health check in the task definition) instead of Route 53 probing.
  health_check_custom_config {
    failure_threshold = 1
  }

  tags = var.tags
}

# The (free) VPC Link: API Gateway's foothold inside the VPC — it places an
# ENI per subnet and forwards requests through them to the task IPs.
resource "aws_apigatewayv2_vpc_link" "this" {
  name               = var.name
  subnet_ids         = var.subnet_ids
  security_group_ids = [aws_security_group.vpc_link.id]
  tags               = var.tags
}

resource "aws_apigatewayv2_api" "this" {
  name          = var.name
  protocol_type = "HTTP"
  tags          = var.tags
}

resource "aws_apigatewayv2_integration" "this" {
  api_id             = aws_apigatewayv2_api.this.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = aws_service_discovery_service.this.arn
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.this.id

  # Private integrations only speak payload format 1.0. 30s is the HTTP API
  # ceiling, not a tunable — anything slower 504s at the gateway.
  payload_format_version = "1.0"
  timeout_milliseconds   = 30000
}

# One catch-all route: the app's FastAPI router does the real routing.
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.this.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  # API Gateway bills per request, so a runaway/hostile client is a bill as
  # well as load. Generous for one small task; raise alongside desired_count.
  default_route_settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }

  tags = var.tags
}

locals {
  # The custom domain needs both the hostname and an issued cert; the Route 53
  # alias additionally needs the zone.
  create_custom_domain = var.domain_name != null && var.certificate_arn != null
}

# Serve the API at https://domain_name. HTTPS only — API Gateway has no
# port-80 listener, so there's no HTTP->HTTPS redirect any more.
resource "aws_apigatewayv2_domain_name" "this" {
  count = local.create_custom_domain ? 1 : 0

  domain_name = var.domain_name

  domain_name_configuration {
    certificate_arn = var.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = var.tags
}

resource "aws_apigatewayv2_api_mapping" "this" {
  count = local.create_custom_domain ? 1 : 0

  api_id      = aws_apigatewayv2_api.this.id
  domain_name = aws_apigatewayv2_domain_name.this[0].id
  stage       = aws_apigatewayv2_stage.default.id
}

# DNS: point domain_name at the API's regional endpoint.
resource "aws_route53_record" "this" {
  count = local.create_custom_domain && var.route53_zone_id != null ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.this[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.this[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# ---------------------------------------------------------------------------
# The task definition + service.
# DATABASE_URL is injected from SSM via the container "secrets" block, so it
# never appears in the task definition in plaintext.
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = var.name
  tags = var.tags
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # The "secrets" block injects SSM SecureStrings (DATABASE_URL plus any
  # extra_secrets) as env vars. Omitted entirely when there are none (e.g. a
  # static frontend), so nothing secret appears in the task definition.
  container_definitions = jsonencode([
    merge(
      {
        name      = "app"
        image     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
        essential = true

        portMappings = [
          { containerPort = var.container_port, protocol = "tcp" }
        ]

        # The ALB's target-group check used to decide task health; with API
        # Gateway there is no LB, so the container checks itself and ECS
        # replaces failures. Python stdlib because the slim image has no curl.
        healthCheck = {
          command = [
            "CMD-SHELL",
            "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${var.container_port}${var.health_check_path}', timeout=4)\""
          ]
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = 30
        }

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.this.name
            "awslogs-region"        = data.aws_region.current.name
            "awslogs-stream-prefix" = "app"
          }
        }
      },
      length(local.container_environment) == 0 ? {} : {
        environment = local.container_environment
      },
      length(local.container_secrets) == 0 ? {} : {
        secrets = local.container_secrets
      }
    )
  ])

  tags = var.tags
}

# ---------------------------------------------------------------------------
# A SECOND task definition for out-of-band batch work — the data-sync sweeps.
# It is NOT a service: it's launched as one-off `aws ecs run-task` tasks (by the
# sync-* GitHub workflows, or by hand), runs `python -m app.sync <slice>` to
# completion, and exits — billed per-second, only while a sweep runs.
#
# Same image, roles, secrets and log group as the app; two deliberate differences:
#   - its OWN, larger memory (sync_memory, default 1 GB) so a heavy sweep (the
#     ~2,800-row universe screen + its per-ticker enrichment pass) has headroom
#     without bloating the small always-on API task — moving the sweeps here is
#     what keeps the service from OOM-ing on sync work;
#   - no portMappings and no healthCheck — it's a batch job, not a server.
# The `command` here is only a bare-run fallback; every invocation overrides it
# via `run-task --overrides` to pick the slice (e.g. app.sync universe).
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "sync" {
  family                   = "${var.name}-sync"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.sync_cpu
  memory                   = var.sync_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    merge(
      {
        name      = "app"
        image     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
        essential = true

        # Overridden per run (run-task --overrides) to select the slice; run bare,
        # the CLI prints usage and exits non-zero, which is a safe no-op.
        command = ["python", "-m", "app.sync"]

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.this.name
            "awslogs-region"        = data.aws_region.current.name
            "awslogs-stream-prefix" = "sync"
          }
        }
      },
      length(local.container_environment) == 0 ? {} : {
        environment = local.container_environment
      },
      length(local.container_secrets) == 0 ? {} : {
        secrets = local.container_secrets
      }
    )
  ])

  tags = var.tags
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets = var.subnet_ids
    # service SG (VPC Link -> task), plus the app SG (task -> database) when set.
    # A static frontend needs no database, so it carries only the service SG.
    security_groups = var.app_security_group_id == null ? [aws_security_group.service.id] : [
      aws_security_group.service.id, var.app_security_group_id
    ]
    assign_public_ip = true # needed in public subnets to pull the image
  }

  # Register each task's IP + port in Cloud Map (SRV needs container_name +
  # container_port) so the API Gateway integration can discover it. On a
  # deploy, stopping tasks are deregistered a beat before they die — a brief
  # window where the gateway can still hit the old IP (a stray 5xx during
  # rollouts; the ALB's connection draining was more graceful).
  service_registries {
    registry_arn   = aws_service_discovery_service.this.arn
    container_name = "app"
    container_port = var.container_port
  }

  tags = var.tags
}

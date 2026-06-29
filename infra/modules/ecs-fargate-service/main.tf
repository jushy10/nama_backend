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
# Networking: a public ALB, and a task SG that only accepts traffic from it.
# Tasks also carry app_security_group_id so the database accepts them.
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  description = "Public HTTP to the load balancer"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
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

resource "aws_security_group" "service" {
  name_prefix = "${var.name}-svc-"
  description = "App tasks: traffic from the ALB only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "App port from the ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
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

resource "aws_lb" "this" {
  name_prefix        = "nama-" # aws_lb name_prefix is capped at 6 chars
  load_balancer_type = "application"
  subnets            = var.subnet_ids
  security_groups    = [aws_security_group.alb.id]
  tags               = var.tags
}

resource "aws_lb_target_group" "this" {
  name_prefix = "nama-"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip" # Fargate tasks register by IP

  health_check {
    path                = var.health_check_path
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# Port 80: forward to the app when there's no cert, otherwise redirect to HTTPS.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = var.enable_https ? "redirect" : "forward"
    target_group_arn = var.enable_https ? null : aws_lb_target_group.this.arn

    dynamic "redirect" {
      for_each = var.enable_https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

# Port 443: only when HTTPS is enabled.
resource "aws_lb_listener" "https" {
  count = var.enable_https ? 1 : 0

  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

# DNS: point domain_name at the load balancer.
resource "aws_route53_record" "this" {
  count = var.domain_name == null || var.route53_zone_id == null ? 0 : 1

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

# Extra hostnames (e.g. www) that alias to the same load balancer.
resource "aws_route53_record" "additional" {
  for_each = var.route53_zone_id == null ? toset([]) : toset(var.additional_domain_names)

  zone_id = var.route53_zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
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

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets = var.subnet_ids
    # service SG (ALB -> task), plus the app SG (task -> database) when set.
    # A static frontend needs no database, so it carries only the service SG.
    security_groups = var.app_security_group_id == null ? [aws_security_group.service.id] : [
      aws_security_group.service.id, var.app_security_group_id
    ]
    assign_public_ip = true # needed in public subnets to pull the image
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http]

  tags = var.tags
}

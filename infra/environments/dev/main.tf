# Live config for the DEV environment. Composes reusable modules from
# ../../modules and supplies environment-specific values. To add infrastructure,
# call another module here (see ../../README.md → "How to add a new resource").

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Connectivity smoke test: a free SSM parameter, created via the shared module.
module "hello" {
  source = "../../modules/ssm-parameter"

  name        = "/nama/hello"
  value       = var.greeting
  description = "Created by Terraform as a connectivity smoke test."
}

# Default VPC + its subnets — so we don't have to build networking by hand.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Private PostgreSQL database (no public endpoint). The app reads its connection
# URL from the SSM parameter below; attach module.database.app_security_group_id
# to whatever compute needs to reach it.
module "database" {
  source = "../../modules/rds-postgres"

  name       = "nama-dev"
  vpc_id     = data.aws_vpc.default.id
  subnet_ids = data.aws_subnets.default.ids

  database_url_ssm_name = "/nama/dev/database-url"
}

# DNS + TLS certificate for the public hostname.
module "dns" {
  source = "../../modules/dns-cert"

  parent_domain = var.parent_domain
  domain_name   = var.domain_name
  create_zone   = var.create_hosted_zone
}

# The app on ECS Fargate, behind a public load balancer. It carries the
# database's app security group, reads DATABASE_URL from the SSM SecureString,
# and is served at domain_name over HTTPS.
module "app" {
  source = "../../modules/ecs-fargate-service"

  name                  = "nama-dev"
  vpc_id                = data.aws_vpc.default.id
  subnet_ids            = data.aws_subnets.default.ids
  app_security_group_id = module.database.app_security_group_id
  database_url_ssm_arn  = module.database.database_url_ssm_arn

  domain_name     = var.domain_name
  route53_zone_id = module.dns.zone_id
  certificate_arn = module.dns.certificate_arn
}

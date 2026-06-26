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

locals {
  # An internet-facing ALB runs one node — and bills one public IPv4 (~$3.60/mo)
  # — per subnet/AZ it spans. Spreading it across every default subnet (six AZs
  # in us-east-1) means paying for six IPs to front a single task. Two AZs is the
  # ALB minimum and plenty here, so pin the app to two subnets. The default VPC
  # has one subnet per AZ, so two distinct subnets are two AZs; sort() keeps the
  # selection stable across plans. The database keeps all subnets — it's private
  # (no public IP) and its subnet group just wants coverage across AZs.
  app_subnet_ids = slice(sort(data.aws_subnets.default.ids), 0, 2)
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

# Stock-data credentials for the stocks feature (GET /stocks/{symbol}). Created
# as SecureString placeholders; set the REAL values out of band so they never
# live in code or Terraform state:
#
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/alpaca-api-key-id     --value <YOUR_KEY_ID>
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/alpaca-api-secret-key --value <YOUR_SECRET>
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/finnhub-api-key       --value <YOUR_FINNHUB_KEY>
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/logodev-token         --value <YOUR_LOGODEV_PUBLISHABLE_KEY>
module "alpaca_api_key_id" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/alpaca-api-key-id"
  description = "Alpaca API key ID (stocks feature). Value set out of band."
}

module "alpaca_api_secret_key" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/alpaca-api-secret-key"
  description = "Alpaca API secret key (stocks feature). Value set out of band."
}

# Finnhub powers market cap + dividend enrichment. Optional: until the real key
# is set out of band the app simply returns those fields as null (best-effort),
# so the placeholder is harmless.
module "finnhub_api_key" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/finnhub-api-key"
  description = "Finnhub API key (stocks market cap + dividend). Value set out of band."
}

# Logo.dev serves company logos for GET /stocks/{symbol}/logo. Required: without
# it the logo endpoint returns 503 (the rest of the app is unaffected). This is
# the publishable key (pk_...) only — it rides in the image request URL. The
# separate secret key is NOT used here; don't store it unless we add brand search.
module "logodev_token" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/logodev-token"
  description = "Logo.dev publishable token (company logos). Value set out of band."
}

# FMP (Financial Modeling Prep) is used two ways, both off the same key:
#   1. OPS-TIME: scripts/sync_constituents.py loads the S&P 500 / Nasdaq-100
#      membership + sector into the index_constituents table (the app reads that
#      table; it never calls FMP for the screener while serving).
#   2. RUNTIME: GET /stocks/{symbol} fetches the company description from FMP's
#      profile endpoint (best-effort enrichment) — so the key is injected into
#      the ECS task via module.app.extra_secrets below. Without it the
#      description field is simply null, like a missing Finnhub key.
# The sync job reads it from SSM directly, alongside the database URL it writes:
#
#   export FMP_API_KEY=$(aws ssm get-parameter --name /nama/dev/fmp-api-key \
#     --with-decryption --query Parameter.Value --output text)
#   export DATABASE_URL=$(aws ssm get-parameter --name /nama/dev/database-url \
#     --with-decryption --query Parameter.Value --output text)
#   python scripts/sync_constituents.py
#
# Set the real value out of band, like the keys above:
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/fmp-api-key --value <YOUR_FMP_API_KEY>
module "fmp_api_key" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/fmp-api-key"
  description = "FMP API key (screener constituents sync + company description enrichment)."
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
  subnet_ids            = local.app_subnet_ids
  app_security_group_id = module.database.app_security_group_id
  database_url_ssm_arn  = module.database.database_url_ssm_arn

  # Injected as the env vars the app reads in app/stocks/router.py: the Alpaca
  # keys (required), the optional Finnhub key (market cap + dividend), the
  # Logo.dev token (required for the logo endpoint), and the FMP key (optional;
  # company description on GET /stocks/{symbol}).
  extra_secrets = {
    APCA_API_KEY_ID     = module.alpaca_api_key_id.arn
    APCA_API_SECRET_KEY = module.alpaca_api_secret_key.arn
    FINNHUB_API_KEY     = module.finnhub_api_key.arn
    LOGODEV_TOKEN       = module.logodev_token.arn
    FMP_API_KEY         = module.fmp_api_key.arn
  }

  enable_https    = true
  domain_name     = var.domain_name
  route53_zone_id = module.dns.zone_id
  certificate_arn = module.dns.certificate_arn
}

# DNS + TLS for the frontend (apex + www), in the same hosted zone. create_zone
# is false because module.dns / Route 53 already owns the zone; one cert covers
# both the apex and www via subject_alternative_names.
module "dns_frontend" {
  source = "../../modules/dns-cert"

  parent_domain             = var.parent_domain
  domain_name               = var.frontend_domain_name
  subject_alternative_names = var.frontend_additional_domains
  create_zone               = false
}

# The frontend SPA as static files in S3, served by CloudFront over HTTPS at the
# apex (and www). No Fargate task and no load balancer — far cheaper than running
# nginx on ECS for static assets. CI uploads the build to the bucket and
# invalidates the distribution (see the frontend_bucket_name /
# frontend_distribution_id outputs). The cert comes from module.dns_frontend,
# which issues it in us-east-1 — required, because CloudFront only reads certs
# from there (this stack already deploys to us-east-1).
module "frontend" {
  source = "../../modules/static-site-cloudfront"

  name                    = "nama-frontend-dev"
  domain_name             = var.frontend_domain_name
  additional_domain_names = var.frontend_additional_domains
  certificate_arn         = module.dns_frontend.certificate_arn
  route53_zone_id         = module.dns_frontend.zone_id
}

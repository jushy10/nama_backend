# Live config for the DEV environment. Composes reusable modules from
# ../../modules and supplies environment-specific values. To add infrastructure,
# call another module here (see ../../README.md → "How to add a new resource").

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

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

# Per-subnet detail, to learn each subnet's AZ *ID* for the filter below.
data "aws_subnet" "default" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  # API Gateway VPC Links aren't offered in every AZ — creating one in a
  # subnet in use1-az3 fails with "service is not available" (learned the
  # hard way; it took the API down mid-apply). Deny-list by AZ *ID*, not name:
  # names like us-east-1c map to different physical AZs per account, IDs are
  # stable.
  vpc_link_unsupported_az_ids = ["use1-az3"]

  app_candidate_subnet_ids = [
    for s in data.aws_subnet.default : s.id
    if !contains(local.vpc_link_unsupported_az_ids, s.availability_zone_id)
  ]

  # Two subnets/AZs are plenty for the app: the API Gateway VPC Link puts one
  # ENI in each (free — unlike the ALB this replaced, which billed a public
  # IPv4 per AZ), and the single task lands in one of them. The default VPC
  # has one subnet per AZ, so two distinct subnets are two AZs; sort() keeps
  # the selection stable across plans. The database keeps all subnets — it's
  # private (no public IP) and its subnet group just wants coverage across AZs.
  app_subnet_ids = slice(sort(local.app_candidate_subnet_ids), 0, 2)
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

# A small SSM-managed bastion for reaching the *private* database from a laptop.
# It opens no inbound ports and has no SSH key — you tunnel through it with
# Session Manager port forwarding (see infra/README.md → "Connecting the app").
# It carries the database's app SG, so it is allowed to reach Postgres on 5432.
#
# Parked (stopped) by default (see aws_ec2_instance_state below) so it costs only
# its ~$0.64/mo disk until you need it — start it on demand with infra/bastion.ps1
# (start/stop reuse the same instance + disk, so there's no recreate and no
# first-boot OOM). A t4g.nano + its public IPv4 + 8GB disk runs ~$7/mo *while
# running*; set bastion_desired_state = "running" to keep it up across applies, or
# bastion_enabled = false to remove it entirely (it's stateless — nothing is
# lost). It is NOT in the app's serving path, so none of this ever affects the
# API.
module "bastion" {
  count  = var.bastion_enabled ? 1 : 0
  source = "../../modules/bastion-ssm"

  name      = "nama-dev-bastion"
  vpc_id    = data.aws_vpc.default.id
  subnet_id = local.app_subnet_ids[0]

  extra_security_group_ids = [module.database.app_security_group_id]

  # Idle auto-stop safety net: if a manual `bastion.ps1 up` is forgotten, a
  # CloudWatch alarm stops the box after this many minutes of near-idle CPU. Only
  # armed while the bastion is parked-by-default (bastion_desired_state =
  # "stopped"); the "running" always-on mode wants it up, so it's disabled there.
  auto_stop_idle_minutes = var.bastion_desired_state == "stopped" ? var.bastion_auto_stop_idle_minutes : 0
}

# Hold the bastion's power state. Defaults to "stopped" (bastion_desired_state) so
# every apply parks it — the box exists but bills only its disk until you need it.
# Terraform's aws_instance never reconciles power state on its own, so this
# companion resource owns it: an on-demand `infra/bastion.ps1 up` starts the box
# and it stays up until the *next* apply reconciles it back to stopped (set
# bastion_desired_state = "running" to make it persist). It replaces the old
# always-on hold (and, before that, the "Bastion session" GitHub workflow).
resource "aws_ec2_instance_state" "bastion" {
  count       = var.bastion_enabled ? 1 : 0
  instance_id = module.bastion[0].instance_id
  state       = var.bastion_desired_state
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
#   aws ssm put-parameter --overwrite --type SecureString \
#     --name /nama/dev/cron-sync-token       --value <A_LONG_RANDOM_STRING>
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

# Shared bearer token guarding the /internal/*/sync cron endpoints (require_cron_token in
# app/stocks/endpoints/cron_auth.py). The guard is fail-closed, so until the real value is set
# out of band the module's placeholder is the accepted token — overwrite it with a long random
# string (the endpoints stay locked to whatever this holds). The sync workflows do NOT use these
# HTTP endpoints (they run one-off ECS tasks via `python -m app.sync`), so this only gates a
# manual/HTTP trigger.
module "cron_sync_token" {
  source      = "../../modules/ssm-secret"
  name        = "/nama/dev/cron-sync-token"
  description = "Shared bearer token for the /internal/*/sync cron endpoints. Value set out of band."
}

# DNS + TLS certificate for the public hostname.
module "dns" {
  source = "../../modules/dns-cert"

  parent_domain = var.parent_domain
  domain_name   = var.domain_name
  create_zone   = var.create_hosted_zone
}

# Google Search Console domain-property verification. A TXT record at the apex
# (namainsights.com) proves ownership of the domain and every subdomain, so the SEO
# sitemap can be submitted in Search Console. module.dns.zone_id is the namainsights.com
# hosted zone. If a later verification (Bing) or SPF needs the apex TXT too, add its value
# to this same `records` list — Route 53 keeps all apex TXT values in one record set, so a
# second record resource for the same name/type would collide.
resource "aws_route53_record" "google_site_verification" {
  zone_id = module.dns.zone_id
  name    = var.parent_domain
  type    = "TXT"
  ttl     = 300
  records = [
    "google-site-verification=hOAOsyYqg8DUzoo-mO_gTLL9DUmsoi9YOK82ouctwLI",
  ]
}

# The app on ECS Fargate, fronted by an API Gateway HTTP API (per-request
# billing — no always-on load balancer). It carries the database's app
# security group, reads DATABASE_URL from the SSM SecureString, and is served
# at domain_name over HTTPS (HTTPS only — API Gateway has no port 80).
module "app" {
  source = "../../modules/ecs-fargate-service"

  name                  = "nama-dev"
  vpc_id                = data.aws_vpc.default.id
  subnet_ids            = local.app_subnet_ids
  app_security_group_id = module.database.app_security_group_id
  database_url_ssm_arn  = module.database.database_url_ssm_arn

  # Injected as the env vars the app reads (app/stocks/router.py, plus the cron
  # guard in app/stocks/endpoints/cron_auth.py): the Alpaca keys (required), the
  # optional Finnhub key (market cap + dividend), the Logo.dev token (required for
  # the logo endpoint), and the cron sync token (guards the /internal/*/sync
  # endpoints). These ride onto BOTH task defs; the CLI sync task ignores the cron
  # token (it calls the runners directly, not over HTTP), which is harmless.
  extra_secrets = {
    APCA_API_KEY_ID     = module.alpaca_api_key_id.arn
    APCA_API_SECRET_KEY = module.alpaca_api_secret_key.arn
    FINNHUB_API_KEY     = module.finnhub_api_key.arn
    LOGODEV_TOKEN       = module.logodev_token.arn
    CRON_SYNC_TOKEN     = module.cron_sync_token.arn
  }

  # Plain (non-secret) config for the AI analysis endpoints, so the Bedrock model
  # and region are swappable without a code change. Using Claude Haiku 4.5: the
  # analysis output is short, plain-language summarization of already-computed
  # figures, so the fast/cheap tier is the right fit (and Opus 4.8 isn't entitled
  # to this account on Bedrock anyway). Drives BOTH the per-stock and ETF analysis
  # — they share this var; the sector, market, and earnings reads have their own
  # *_MODEL_ID overrides, each already defaulting to Haiku in code. The id is a
  # cross-region inference profile and must be access-enabled in this account;
  # Haiku 4.5 has no short alias on Bedrock, so it's the full versioned id (the
  # short us.anthropic.claude-haiku-4-5 400s with "invalid model identifier").
  extra_environment = {
    BEDROCK_REGION            = "us-east-1"
    BEDROCK_ANALYSIS_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # The public canonical origin the SEO content pages stamp into their canonical/OG
    # URLs and sitemap (app/stocks/endpoints/seo_endpoints.py). The *www* host, because
    # the frontend distribution 301-redirects the apex to www — a canonical/sitemap URL
    # on the apex would needlessly bounce. Harmless on the sync task def, which ignores it.
    PUBLIC_SITE_ORIGIN = "https://${var.frontend_canonical_domain}"
  }

  # Grant the task role bedrock:InvokeModel for the AI analysis endpoints
  # (per-stock /stocks/{symbol}/analysis, ETF, sector, market, earnings). Bedrock
  # authenticates as the task role, so there's no API key — but the model must be
  # access-enabled in this account / region, and BEDROCK_ANALYSIS_MODEL_ID names a
  # cross-region inference profile (defaults to us.anthropic.claude-haiku-4-5 in code).
  enable_bedrock_invoke = true

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
#
# redirect_to_domain makes www the canonical host: a CloudFront edge function
# 301-redirects the apex (namainsights.com) to www.namainsights.com, so the site
# has one canonical URL instead of serving identical content at both.
#
# SEO content pages: the same distribution also routes the server-rendered SEO paths to the
# APP origin (module.app at api.namainsights.com), so /stock/*, /sitemap.xml, /robots.txt and
# /llms.txt are served by FastAPI *under the main hostname* — inheriting the site's authority
# — while everything else stays the S3 SPA. The API Gateway's $default route passes these
# paths straight to the app. See app/stocks/seo/ and its README.
module "frontend" {
  source = "../../modules/static-site-cloudfront"

  name                    = "nama-frontend-dev"
  domain_name             = var.frontend_domain_name
  additional_domain_names = var.frontend_additional_domains
  redirect_to_domain      = var.frontend_canonical_domain
  certificate_arn         = module.dns_frontend.certificate_arn
  route53_zone_id         = module.dns_frontend.zone_id

  # Route the SEO surface to the app. var.domain_name is the API's custom domain
  # (api.namainsights.com); its cert covers that host, which the https-only origin needs.
  backend_origin_domain_name = var.domain_name
  backend_path_patterns = [
    "/stock/*",
    "/etf/*",
    "/sector/*",
    "/screen/*",
    "/sitemap.xml",
    "/robots.txt",
    "/llms.txt",
  ]
}

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

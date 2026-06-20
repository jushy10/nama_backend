# Minimal "is Terraform connected to AWS?" setup. No VPC, subnets, or other
# inputs required — apply it as soon as your credentials work.
#
#   - The two data sources confirm WHICH account/region your credentials
#     resolved to (read-only; they change nothing).
#   - The SSM parameter proves Terraform can actually create + update + destroy
#     resources in your account. SSM Parameter Store "Standard" tier is free, so
#     this costs nothing and is trivial to delete.

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

resource "aws_ssm_parameter" "hello" {
  name        = "/nama/hello"
  description = "Created by Terraform as a connectivity smoke test."
  type        = "String"
  tier        = "Standard" # free tier
  value       = var.greeting

  tags = {
    Project   = "nama"
    ManagedBy = "terraform"
  }
}

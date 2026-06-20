output "role_arn" {
  description = "Set this as the GitHub repo variable AWS_ROLE_ARN to enable the terraform plan job."
  value       = aws_iam_role.plan.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in this account."
  value       = local.oidc_provider_arn
}

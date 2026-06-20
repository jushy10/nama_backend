output "account_id" {
  description = "The AWS account your credentials resolved to."
  value       = data.aws_caller_identity.current.account_id
}

output "caller_arn" {
  description = "The identity (user/role) Terraform authenticated as."
  value       = data.aws_caller_identity.current.arn
}

output "region" {
  description = "The region the provider is using."
  value       = data.aws_region.current.name
}

output "parameter_name" {
  description = "The SSM parameter Terraform created."
  value       = aws_ssm_parameter.hello.name
}

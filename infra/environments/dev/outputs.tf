output "account_id" {
  description = "The AWS account this environment deploys to."
  value       = data.aws_caller_identity.current.account_id
}

output "region" {
  description = "The region this environment uses."
  value       = data.aws_region.current.name
}

output "hello_parameter_name" {
  description = "Name of the demo SSM parameter."
  value       = module.hello.name
}

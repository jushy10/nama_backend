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

output "database_endpoint" {
  description = "RDS endpoint (host:port). Private — reachable only inside the VPC."
  value       = module.database.endpoint
}

output "database_url_ssm_parameter" {
  description = "SSM SecureString name holding the SQLAlchemy DATABASE_URL."
  value       = module.database.database_url_ssm_name
}

output "app_security_group_id" {
  description = "Attach this SG to compute (ECS/EC2/Lambda) that needs the database."
  value       = module.database.app_security_group_id
}

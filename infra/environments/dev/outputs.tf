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

output "database_address" {
  description = "RDS hostname (no port) — the `host` for SSM port-forwarding through the bastion."
  value       = module.database.address
}

output "bastion_instance_id" {
  description = "SSM bastion instance id — the --target for `aws ssm start-session` to tunnel to the database."
  value       = module.bastion.instance_id
}

output "app_security_group_id" {
  description = "Attach this SG to compute (ECS/EC2/Lambda) that needs the database."
  value       = module.database.app_security_group_id
}

output "app_url" {
  description = "Public URL of the app (once an image is pushed and the service is healthy)."
  value       = module.app.url
}

output "ecr_repository_url" {
  description = "Push the app's Docker image here."
  value       = module.app.ecr_repository_url
}

output "frontend_url" {
  description = "Public URL of the frontend (live once the build is uploaded to the bucket)."
  value       = module.frontend.url
}

output "frontend_bucket_name" {
  description = "S3 bucket the frontend build is uploaded to (aws s3 sync target)."
  value       = module.frontend.bucket_name
}

output "frontend_distribution_id" {
  description = "CloudFront distribution id to invalidate after uploading a new build."
  value       = module.frontend.distribution_id
}

output "name_servers" {
  description = "Set these at your registrar — only populated if Terraform created the hosted zone."
  value       = module.dns.name_servers
}

output "endpoint" {
  description = "host:port of the database."
  value       = aws_db_instance.this.endpoint
}

output "address" {
  description = "Database hostname."
  value       = aws_db_instance.this.address
}

output "port" {
  description = "Database port."
  value       = aws_db_instance.this.port
}

output "db_name" {
  description = "Initial database name."
  value       = aws_db_instance.this.db_name
}

output "app_security_group_id" {
  description = "Attach to compute that needs to reach the database."
  value       = aws_security_group.app.id
}

output "database_url_ssm_name" {
  description = "SSM SecureString holding the SQLAlchemy connection URL."
  value       = aws_ssm_parameter.database_url.name
}

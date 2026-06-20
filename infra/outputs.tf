output "db_endpoint" {
  description = "Hostname:port of the database. Use the host in DATABASE_URL."
  value       = aws_db_instance.this.endpoint
}

output "db_address" {
  description = "Hostname of the database (no port)."
  value       = aws_db_instance.this.address
}

output "db_port" {
  description = "Database port."
  value       = aws_db_instance.this.port
}

output "db_name" {
  description = "Initial database name."
  value       = aws_db_instance.this.db_name
}

output "db_security_group_id" {
  description = "Attach inbound access by adding your app's SG to allowed_app_security_group_ids."
  value       = aws_security_group.db.id
}

output "master_user_secret_arn" {
  description = <<-EOT
    ARN of the Secrets Manager secret holding the master username/password.
    Grant your app's IAM role secretsmanager:GetSecretValue on this ARN and read
    it at startup to build DATABASE_URL. The secret is a JSON blob with
    {"username": ..., "password": ...}.
  EOT
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

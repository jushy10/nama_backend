output "name" {
  description = "The parameter name."
  value       = aws_ssm_parameter.this.name
}

output "arn" {
  description = "The parameter ARN."
  value       = aws_ssm_parameter.this.arn
}

output "version" {
  description = "The parameter version (bumps on each value change)."
  value       = aws_ssm_parameter.this.version
}

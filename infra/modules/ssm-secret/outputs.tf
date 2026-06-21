output "name" {
  description = "The parameter name."
  value       = aws_ssm_parameter.this.name
}

output "arn" {
  description = "The parameter ARN."
  value       = aws_ssm_parameter.this.arn
}

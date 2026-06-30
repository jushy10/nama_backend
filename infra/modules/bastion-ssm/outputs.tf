output "instance_id" {
  description = "EC2 instance id — the --target for `aws ssm start-session`."
  value       = aws_instance.this.id
}

output "security_group_id" {
  description = "The bastion's own security group id."
  value       = aws_security_group.this.id
}

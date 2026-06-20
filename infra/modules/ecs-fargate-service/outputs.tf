output "url" {
  description = "Public URL of the app."
  value       = "http://${aws_lb.this.dns_name}"
}

output "alb_dns_name" {
  description = "ALB DNS name."
  value       = aws_lb.this.dns_name
}

output "ecr_repository_url" {
  description = "Push the app image here."
  value       = aws_ecr_repository.this.repository_url
}

output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.this.name
}

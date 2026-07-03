output "url" {
  description = "Public URL of the app."
  value       = var.domain_name != null ? "https://${var.domain_name}" : aws_apigatewayv2_api.this.api_endpoint
}

output "api_endpoint" {
  description = "The API's default execute-api endpoint — works even before DNS points at it."
  value       = aws_apigatewayv2_api.this.api_endpoint
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

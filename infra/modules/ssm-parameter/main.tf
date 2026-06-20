resource "aws_ssm_parameter" "this" {
  name        = var.name
  description = var.description
  type        = var.type
  tier        = var.tier
  value       = var.value
  tags        = var.tags
}

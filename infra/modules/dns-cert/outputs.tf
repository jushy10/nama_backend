output "zone_id" {
  description = "Route 53 hosted zone ID."
  value       = local.zone_id
}

output "certificate_arn" {
  description = "ARN of the validated (issued) certificate."
  value       = aws_acm_certificate_validation.this.certificate_arn
}

output "name_servers" {
  description = "Nameservers to set at your registrar — only populated when this module created the zone."
  value       = local.name_servers
}

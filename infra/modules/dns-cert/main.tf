# Hosted zone — create it, or use the one that already exists (e.g. when the
# domain was registered through Route 53).
resource "aws_route53_zone" "this" {
  count = var.create_zone ? 1 : 0
  name  = var.parent_domain
  tags  = var.tags
}

data "aws_route53_zone" "this" {
  count        = var.create_zone ? 0 : 1
  name         = var.parent_domain
  private_zone = false
}

locals {
  zone_id      = var.create_zone ? aws_route53_zone.this[0].zone_id : data.aws_route53_zone.this[0].zone_id
  name_servers = var.create_zone ? aws_route53_zone.this[0].name_servers : []
}

# Free, auto-renewing TLS certificate, validated by DNS.
resource "aws_acm_certificate" "this" {
  domain_name       = var.domain_name
  validation_method = "DNS"
  tags              = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

# Write the validation record ACM asks for into the hosted zone.
resource "aws_route53_record" "validation" {
  for_each = {
    for dvo in aws_acm_certificate.this.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# Blocks until the certificate is issued (a minute or two once the zone is live).
resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

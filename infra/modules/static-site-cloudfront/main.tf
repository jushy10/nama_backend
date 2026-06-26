# A static single-page app served from a private S3 bucket via CloudFront over
# HTTPS. Replaces running nginx on Fargate behind an ALB for static files: no
# always-on task and no load balancer, so it costs cents at this scale.
#
# CI uploads the built assets to the bucket and invalidates the distribution
# (see outputs.tf for the bucket name + distribution id it needs).

locals {
  # apex + any extras (e.g. www) — the names the cert covers and CloudFront serves.
  all_domain_names = toset(concat([var.domain_name], var.additional_domain_names))
  origin_id        = "s3-${var.name}"
}

# Private bucket holding the build. Never public — CloudFront reads it through
# the Origin Access Control below; everything else is blocked. force_destroy so
# `terraform destroy` works even with objects present (dev convenience).
resource "aws_s3_bucket" "this" {
  bucket_prefix = "${var.name}-"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Origin Access Control — the modern (SigV4) replacement for Origin Access
# Identity. Lets this CloudFront distribution, and nothing else, read the bucket.
resource "aws_cloudfront_origin_access_control" "this" {
  name                              = "${var.name}-oac"
  description                       = "OAC for ${var.name}"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = var.name
  aliases             = local.all_domain_names
  default_root_object = var.default_root_object
  price_class         = var.price_class

  origin {
    domain_name              = aws_s3_bucket.this.bucket_regional_domain_name
    origin_id                = local.origin_id
    origin_access_control_id = aws_cloudfront_origin_access_control.this.id
  }

  default_cache_behavior {
    target_origin_id       = local.origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # AWS managed "CachingOptimized" policy (a fixed, well-known id). Using a
    # cache policy means we must NOT also set forwarded_values.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # SPA client-side routing: a deep link like /dashboard isn't a real object, so
  # S3 returns 403 (no ListBucket) or 404. Serve index.html with a 200 instead so
  # the app's router can take over.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/${var.default_root_object}"
    error_caching_min_ttl = 10
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/${var.default_root_object}"
    error_caching_min_ttl = 10
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # CloudFront only accepts ACM certs from us-east-1 (see variables.tf).
  viewer_certificate {
    acm_certificate_arn      = var.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = var.tags
}

# Bucket policy: allow only this distribution (matched by its ARN) to read objects.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json
}

# Point each hostname at the distribution (A for IPv4, AAAA for IPv6).
resource "aws_route53_record" "a" {
  for_each = var.route53_zone_id == null ? toset([]) : local.all_domain_names

  zone_id = var.route53_zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "aaaa" {
  for_each = var.route53_zone_id == null ? toset([]) : local.all_domain_names

  zone_id = var.route53_zone_id
  name    = each.value
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}

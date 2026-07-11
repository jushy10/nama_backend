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

  # When a backend origin is configured, a set of path patterns is routed to it (the
  # dynamic SEO content pages served by the app) while everything else stays on S3.
  backend_enabled   = var.backend_origin_domain_name != null
  backend_origin_id = "backend-${var.name}"

  # AWS managed policies (fixed, well-known ids):
  #  - CachingOptimized: cache key = path only (no query/headers/cookies), honors the
  #    origin's Cache-Control — so the app's per-route max-age drives edge caching.
  #  - AllViewerExceptHostHeader: forwards the viewer request to the origin but lets
  #    CloudFront set Host to the origin's own hostname, which an API Gateway custom
  #    domain requires to route (forwarding the viewer Host would 403 at the gateway).
  caching_optimized_policy_id            = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  all_viewer_except_host_request_policy  = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
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

# Edge function that 301-redirects any non-canonical hostname to var.redirect_to_domain
# (e.g. the apex -> www). Only created when a canonical host is set; a single
# distribution serves both names, so the redirect lives here rather than in a
# second distribution/bucket. CloudFront Functions run on the viewer-request path
# and are effectively free at this scale.
resource "aws_cloudfront_function" "canonical_redirect" {
  count   = var.redirect_to_domain == null ? 0 : 1
  name    = "${var.name}-canonical-redirect"
  runtime = "cloudfront-js-2.0"
  comment = "301 non-canonical hosts to ${var.redirect_to_domain}"
  publish = true
  code = templatefile("${path.module}/canonical-redirect.js.tftpl", {
    canonical_domain = var.redirect_to_domain
  })
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

  # The app origin (e.g. the API Gateway custom domain), added only when configured. A
  # custom (non-S3) origin reached over HTTPS; CloudFront verifies its cert against the
  # origin hostname, which is why we route to the API's own domain rather than the raw
  # execute-api endpoint.
  dynamic "origin" {
    for_each = local.backend_enabled ? [1] : []
    content {
      domain_name = var.backend_origin_domain_name
      origin_id   = local.backend_origin_id

      custom_origin_config {
        http_port              = 80
        https_port             = 443
        origin_protocol_policy = "https-only"
        origin_ssl_protocols   = ["TLSv1.2"]
      }
    }
  }

  # Route the configured path patterns to the app origin, ahead of the SPA default. These
  # carry the same canonical-host redirect as the default behavior so the apex still
  # 301s to www on a content URL. The patterns are non-overlapping, so their relative
  # order doesn't matter; each just needs to sit ahead of the (default) S3 behavior.
  dynamic "ordered_cache_behavior" {
    for_each = local.backend_enabled ? toset(var.backend_path_patterns) : toset([])
    content {
      path_pattern           = ordered_cache_behavior.value
      target_origin_id       = local.backend_origin_id
      viewer_protocol_policy = "redirect-to-https"
      allowed_methods        = ["GET", "HEAD", "OPTIONS"]
      cached_methods         = ["GET", "HEAD"]
      compress               = true

      cache_policy_id          = local.caching_optimized_policy_id
      origin_request_policy_id = local.all_viewer_except_host_request_policy

      dynamic "function_association" {
        for_each = var.redirect_to_domain == null ? [] : [1]
        content {
          event_type   = "viewer-request"
          function_arn = aws_cloudfront_function.canonical_redirect[0].arn
        }
      }
    }
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

    # Canonical-host redirect (apex -> www) at the edge, when configured. Runs
    # before the cache/origin, so a redirected request never touches S3.
    dynamic "function_association" {
      for_each = var.redirect_to_domain == null ? [] : [1]
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.canonical_redirect[0].arn
      }
    }
  }

  # SPA client-side routing: a deep link like /dashboard isn't a real object, so
  # S3 returns 403 (no ListBucket) or 404. Serve index.html with a 200 instead so
  # the app's router can take over.
  #
  # Caveat with a backend origin: these responses are DISTRIBUTION-wide (CloudFront has no
  # per-behavior error config), so a 404 from the app origin — e.g. /stock/{unknown-ticker}
  # — is also rewritten to index.html. That only affects *unadvertised* URLs (the sitemap
  # lists only real, screened tickers, which the app serves 200), so the indexed set is
  # unaffected; it just means a stray unknown /stock/* renders the SPA shell rather than a
  # hard 404. Non-404/403 statuses (400 on a malformed ticker, 5xx) pass through unchanged.
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

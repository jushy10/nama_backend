output "url" {
  description = "Public HTTPS URL of the site (the canonical host when a redirect is configured)."
  value       = "https://${coalesce(var.redirect_to_domain, var.domain_name)}"
}

output "bucket_name" {
  description = "S3 bucket holding the build. CI uploads the static assets here."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "ARN of the content bucket."
  value       = aws_s3_bucket.this.arn
}

output "distribution_id" {
  description = "CloudFront distribution id. CI invalidates this after an upload."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_domain_name" {
  description = "CloudFront domain (e.g. dxxxx.cloudfront.net) the hostnames alias to."
  value       = aws_cloudfront_distribution.this.domain_name
}

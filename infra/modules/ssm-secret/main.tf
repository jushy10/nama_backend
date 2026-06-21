# An SSM SecureString whose value is managed OUT OF BAND — set once via the
# console or `aws ssm put-parameter --overwrite`, never by Terraform. Terraform
# creates the parameter with a harmless placeholder and then ignores value
# changes, so the real secret never lives in code or Terraform state. Use this
# for externally-sourced secrets (third-party API keys, etc.). For values
# Terraform itself produces, use the ssm-parameter module instead.
resource "aws_ssm_parameter" "this" {
  name        = var.name
  description = var.description
  type        = "SecureString"
  value       = var.placeholder
  tags        = var.tags

  lifecycle {
    # The real value is set out of band; don't let Terraform clobber it.
    ignore_changes = [value]
  }
}

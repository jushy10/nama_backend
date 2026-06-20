data "aws_caller_identity" "current" {}

locals {
  subjects = coalesce(
    var.allowed_github_subjects,
    ["repo:${var.github_owner}/${var.github_repo}:pull_request"],
  )

  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.existing[0].arn

  tags = merge({ ManagedBy = "terraform" }, var.tags)
}

# ---------------------------------------------------------------------------
# GitHub OIDC provider. Lets GitHub Actions exchange a short-lived OIDC token
# for AWS credentials — no static access keys stored in GitHub.
# Only one of these may exist per account, hence the create/lookup toggle.
# ---------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  # AWS validates GitHub's token against a trusted CA now, but the field is
  # still required. These are GitHub's published thumbprints.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]

  tags = local.tags
}

data "aws_iam_openid_connect_provider" "existing" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

# ---------------------------------------------------------------------------
# The role GitHub Actions assumes. Trust policy pins:
#   - aud = sts.amazonaws.com (the audience GitHub requests)
#   - sub = the specific repo + event(s) in allowed_github_subjects
# so only the intended workflow runs of THIS repo can assume it.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.subjects
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = var.role_name
  description          = "Assumed by GitHub Actions to run terraform plan (read-only)"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
  tags                 = local.tags
}

# Plan only needs to READ AWS to refresh state. ReadOnlyAccess can't mutate
# anything, which is exactly what we want for a plan-only CI role.
resource "aws_iam_role_policy_attachment" "read_only" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# State-backend access (only when using the S3 remote backend). Read the state
# object and acquire/release the DynamoDB lock — the lock needs write actions
# that ReadOnlyAccess doesn't grant.
data "aws_iam_policy_document" "backend" {
  count = var.state_bucket != "" ? 1 : 0

  statement {
    sid       = "StateBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }

  statement {
    sid       = "StateObjectRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}/*"]
  }

  dynamic "statement" {
    for_each = var.lock_table != "" ? [1] : []
    content {
      sid       = "StateLock"
      effect    = "Allow"
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
      resources = ["arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.lock_table}"]
    }
  }
}

resource "aws_iam_role_policy" "backend" {
  count  = var.state_bucket != "" ? 1 : 0
  name   = "terraform-state-backend"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.backend[0].json
}

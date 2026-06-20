# infra/github-oidc/ — CI bootstrap (run once)

Creates the **GitHub OIDC provider** and an **IAM role** that the `terraform`
GitHub Actions workflow assumes to run `terraform plan`. OIDC means GitHub gets
**short-lived** AWS credentials per run — no static access keys stored in GitHub.

This is a **separate Terraform root** from `infra/` on purpose: it's applied once
with admin credentials, changes rarely, and has its own state. The app-infra
workflow (`working-directory: infra`) never touches it.

## What it creates

| Resource | Purpose |
| --- | --- |
| `aws_iam_openid_connect_provider` | Trusts GitHub's OIDC token issuer (one per account) |
| `aws_iam_role` | Assumed by GitHub Actions; trust scoped to **this repo's PR runs** |
| `ReadOnlyAccess` attachment | `plan` only needs to read AWS — it can't change anything |
| *(optional)* state-backend policy | Read state + lock, once you use the S3 backend |

## Apply (once)

```sh
cd infra/github-oidc
cp terraform.tfvars.example terraform.tfvars   # set github_owner / github_repo
terraform init
terraform apply
terraform output role_arn
```

## Wire it into GitHub

Repo → **Settings → Secrets and variables → Actions → Variables** → add:

| Variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | the `role_arn` output above (this switches the `plan` job on) |
| `AWS_REGION` | e.g. `us-east-1` (optional; defaults to `us-east-1`) |
| `TF_VAR_VPC_ID` | your VPC id |
| `TF_VAR_DB_SUBNET_IDS` | JSON list, e.g. `["subnet-aaa","subnet-bbb"]` |
| `TF_VAR_ALLOWED_APP_SECURITY_GROUP_IDS` | JSON list, e.g. `["sg-app"]` |

Open a PR that touches `infra/**` and the `plan` job will assume the role and
post its plan.

## Security notes

- The trust policy pins `aud = sts.amazonaws.com` **and** `sub = repo:OWNER/REPO:pull_request`,
  so only PR runs of *this* repo can assume the role. Tighten further with
  `allowed_github_subjects` (e.g. a specific GitHub environment) if you want.
- The role is **read-only**. A separate, more privileged role (gated behind a
  manual approval / GitHub environment) should be used if you later add a
  `terraform apply` job — never reuse this plan role for apply.

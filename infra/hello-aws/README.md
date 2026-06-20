# infra/hello-aws/ — minimal Terraform connected to AWS

The smallest possible Terraform that talks to your AWS account. It needs **no VPC,
subnets, or other setup** — apply it as soon as your credentials work, to confirm
the connection and learn the plan → apply → destroy loop.

## What it does

- **Reads** your account ID, identity, and region (data sources) — proves *who*
  Terraform is connected as. Changes nothing.
- **Creates** one free **SSM Parameter Store** entry (`/nama/hello`) — proves
  Terraform can create/update/destroy real resources. Costs nothing.

## Run it locally

First authenticate (one-time): install the tools and configure credentials.

```sh
# Windows: winget install Amazon.AWSCLI Hashicorp.Terraform
aws configure          # or: aws configure sso
aws sts get-caller-identity   # should print your account id
```

Then (after the one-time state-bucket setup under [Remote state](#remote-state-s3)):

```sh
cd infra/hello-aws
terraform init -backend-config="bucket=YOUR_STATE_BUCKET"   # provider + remote state
terraform plan      # preview — should say "Plan: 1 to add"
terraform apply     # type "yes"; creates the parameter
terraform output    # prints your account id, identity, region
```

Confirm it really landed in AWS, then clean up:

```sh
aws ssm get-parameter --name /nama/hello
terraform destroy   # removes the parameter; nothing lingers or bills
```

## Deploy it from GitHub Actions

The [`deploy-hello-aws`](../../.github/workflows/deploy-hello-aws.yml) workflow runs
Terraform in CI: **plan** on pull requests that touch `infra/hello-aws/**`, and
**apply** on pushes to `main`.

One-time setup — add these under **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `AWS_ACCESS_KEY_ID` | an IAM user's access key id |
| Secret | `AWS_SECRET_ACCESS_KEY` | that user's secret access key |
| Variable | `TF_STATE_BUCKET` | your Terraform state bucket (see [Remote state](#remote-state-s3)) |
| Variable | `AWS_REGION` | e.g. `us-east-1` (optional; defaults to `us-east-1`) |

The IAM user needs only a **least-privilege** policy — use
[`ci-iam-policy.json`](ci-iam-policy.json) in this folder. It allows
write/read/delete on SSM parameters under `/nama/*`, `sts:GetCallerIdentity`, and
the account-wide `ssm:DescribeParameters` (AWS requires `"*"` for that one — it
exposes parameter *metadata* only, never values). A leaked key still can't read
secret values or change anything outside `/nama/*`.

Attach it to the user with the CLI:

```sh
aws iam put-user-policy \
  --user-name nama-ci \
  --policy-name nama-hello-aws-deploy \
  --policy-document file://ci-iam-policy.json
```

…or in the console: **IAM → Users → (your user) → Add permissions → Create inline
policy → JSON**, then paste the file. **Replace `YOUR_STATE_BUCKET`** (in two
places) with your real bucket name from the next section first.

Then push to a branch and open a PR (the plan runs); merge to `main` and it applies.

## Remote state (S3)

State is stored in S3 (`backend "s3"` in `versions.tf`) so deploys are repeatable
and safe to run from CI and your laptop: versioned, encrypted, and locked against
concurrent runs via native S3 locking (`use_lockfile`, no DynamoDB table needed).

Create the bucket **once** (name must be globally unique). In the console:

1. **S3 → Create bucket** → a unique name, region **us-east-1**.
2. Leave **Block all public access** on, and default encryption on (both are the
   defaults). Create.
3. Open the bucket → **Properties → Bucket Versioning → Edit → Enable**.

Then wire the name in:

- GitHub: set the `TF_STATE_BUCKET` repository variable to the bucket name.
- IAM: in `ci-iam-policy.json`, replace `YOUR_STATE_BUCKET` (two places) before
  attaching the policy, so the CI user can read/write state and the lock file.
- Locally: `terraform init -backend-config="bucket=YOUR_STATE_BUCKET"`.

### Security note

**Static keys are the simple option, not the most secure one.** Long-lived AWS
keys live in GitHub here. The better upgrade is **GitHub OIDC** — swap the
`aws-access-key-id`/`aws-secret-access-key` inputs for a `role-to-assume` and drop
the secrets entirely. Do that once you're comfortable.

# infra/ — RDS PostgreSQL via Terraform

Provisions a secure PostgreSQL database on Amazon RDS for `nama_backend`.

## What it creates

| Resource | Purpose | Security property |
| --- | --- | --- |
| `aws_db_instance` | The PostgreSQL database | Encrypted at rest, **not** publicly accessible, IAM auth enabled |
| `aws_security_group` | The DB's firewall | Inbound 5432 **only** from your app's security group(s) |
| `aws_db_subnet_group` | Where RDS places the DB | Your **private** subnets (no internet route) |
| `aws_db_parameter_group` | Engine settings | `rds.force_ssl = 1` — rejects non-TLS connections |
| *(managed by RDS)* | Master password | Generated + stored + rotated in **Secrets Manager**; never in state |

## Prerequisites

- Terraform >= 1.5 and AWS credentials (`aws configure` / SSO / a role).
- An existing **VPC** with at least two **private subnets** in different AZs.
- (Eventually) the **security group** your application runs with.

## Usage

```sh
cd infra
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform plan      # review what will be created
terraform apply
```

Outputs you'll use:

```sh
terraform output db_address              # DB hostname
terraform output master_user_secret_arn  # Secrets Manager ARN with the password
```

## Connecting the app

The app reads a single env var, `DATABASE_URL` (see `app/db.py`). Two ways to supply it, least → most secure:

### Option A — password from Secrets Manager (recommended start)

RDS already stored the master credentials in Secrets Manager. Grant your app's
IAM role `secretsmanager:GetSecretValue` on the `master_user_secret_arn`, then
either:

- **ECS / App Runner:** map the secret straight into the container as an env var
  (the platform injects it — it never touches your image or repo), or
- **At startup:** fetch the secret, read `username`/`password` from its JSON, and
  assemble the URL:

  ```
  postgresql+psycopg://<username>:<password>@<db_address>:5432/nama?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-ca.pem
  ```

Download the RDS CA bundle (`global-bundle.pem`) and ship it with the app so
`sslmode=verify-full` can verify the server certificate.

### Option B — IAM database authentication (most secure, no password)

`iam_database_authentication_enabled = true` is already set. To use it:

1. In the database, create a role mapped to IAM:
   ```sql
   CREATE USER nama_iam;
   GRANT rds_iam TO nama_iam;
   -- grant nama_iam the privileges the app needs
   ```
2. Allow the app's IAM role to mint tokens:
   ```
   rds-db:connect  on  arn:aws:rds-db:<region>:<account>:dbuser:<db-resource-id>/nama_iam
   ```
3. At connect time, generate a 15-minute token (`aws rds generate-db-auth-token`
   / `boto3` `generate_db_auth_token`) and use it as the password with
   `sslmode=verify-full`. No long-lived secret exists to leak.

## CI access (GitHub OIDC)

The `terraform plan` CI job assumes an AWS role via GitHub OIDC (no static keys).
Create that role once with [`github-oidc/`](github-oidc/README.md), then set its
`role_arn` output as the `AWS_ROLE_ARN` repo variable.

## Notes

- `deletion_protection = true` by default — to tear down, set it `false`, apply, then destroy.
- `multi_az = false` keeps cost down for dev; turn it on for production failover.
- State can contain sensitive values — use the encrypted S3 backend stub in `versions.tf` for anything real.

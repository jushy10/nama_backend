# infra/hello-aws/ — start here

The smallest possible Terraform that talks to your AWS account. It needs **no VPC,
subnets, or other setup** — apply it the moment your credentials work, to confirm
the connection and learn the plan → apply → destroy loop. The real database
infrastructure lives one level up and can wait until you're ready.

## What it does

- **Reads** your account ID, identity, and region (data sources) — proves *who*
  Terraform is connected as. Changes nothing.
- **Creates** one free **SSM Parameter Store** entry (`/nama/hello`) — proves
  Terraform can create/update/destroy real resources. Costs nothing.

## Run

```sh
cd infra/hello-aws
terraform init      # downloads the AWS provider
terraform plan      # preview — should say "Plan: 1 to add"
terraform apply     # type "yes"; creates the parameter
terraform output    # prints your account id, identity, region
```

Confirm it really landed in AWS:

```sh
aws ssm get-parameter --name /nama/hello
```

Try an **update**: change `greeting` in `variables.tf` (or a `terraform.tfvars`),
re-run `terraform apply`, and watch Terraform modify the existing parameter in place.

## Clean up

```sh
terraform destroy   # removes the parameter; nothing lingers or bills
```

## Next steps (later)

When you're ready for the database and CI access:

- [`../`](../README.md) — secure RDS PostgreSQL (needs a VPC + private subnets).
- [`../github-oidc/`](../github-oidc/README.md) — IAM role so GitHub Actions can run `terraform plan`.

Each is a **separate root** with its own state — `cd` into it and run `terraform`
there. This `hello-aws` root stays independent, so you can keep it or destroy it
without affecting the others.

# infra/ — Terraform for the nama AWS account

Infrastructure as code, structured to grow. Reusable **modules** are composed by
per-**environment** root configs, with remote state in S3 and CI that plans on
pull requests and applies on merge.

## Layout

```
infra/
├── modules/                 # reusable building blocks (no provider/backend here)
│   └── ssm-parameter/       # wraps an aws_ssm_parameter
├── environments/            # one root config — and one state file — per environment
│   └── dev/                 # provider + S3 backend + default_tags; composes modules
├── ci-iam-policy.json       # least-privilege policy for the CI deploy user
└── README.md
```

- **Module** = a reusable component (a Lambda, a bucket, a parameter…). Written
  once, called many times. Declares resources only — never a provider or backend.
- **Environment** = a deployable root (`dev`, later `prod`). Owns the provider,
  region, backend/state, and tags, and wires modules together with real values.

## Conventions

- **Tagging is automatic.** Each environment sets `default_tags`
  (`Project`, `Environment`, `ManagedBy`), so every resource is tagged with no
  per-resource effort.
- **One state file per environment:**
  `s3://$TF_STATE_BUCKET/<env>/terraform.tfstate`, with native S3 locking
  (`use_lockfile`). The bucket is passed at init via `-backend-config` so it
  isn't committed.
- **Name with a project prefix** (`/nama/...`, `nama-...`) so the CI policy can
  stay scoped to just this project's resources.
- **Modules pin `required_providers`** but configure nothing — the environment does.

## How to add a new resource

1. **Reuse a module** if one fits — call it from `environments/dev/main.tf`:
   ```hcl
   module "my_thing" {
     source = "../../modules/ssm-parameter"
     name   = "/nama/my-thing"
     value  = "..."
   }
   ```
2. **Or write a new module** under `modules/<name>/` with `main.tf`,
   `variables.tf`, `outputs.tf`, and a `versions.tf` declaring `required_providers`.
   Then call it as above.
3. Open a PR → the `infra` workflow runs `terraform plan`. Merge → it applies.

## How to add a new environment (e.g. prod)

```sh
cp -r infra/environments/dev infra/environments/prod
```
Then in `prod/`: set the backend `key` to `prod/terraform.tfstate`, set
`environment = "prod"`, tweak any tfvars, and add `prod` to the `matrix` in
[`.github/workflows/infra.yml`](../.github/workflows/infra.yml). Each environment
has its own isolated state.

## Bootstrap (one-time per account)

1. **State bucket** — an S3 bucket, **versioned**, **encrypted**, **public access
   blocked**. Terraform can't create the bucket that holds its own state, so make
   this one by hand (console or CLI).
2. **CI user + policy** — attach [`ci-iam-policy.json`](ci-iam-policy.json) to the
   deploy IAM user, replacing `YOUR_STATE_BUCKET`. It grants SSM on `/nama/*`,
   read/write on the state bucket, and `sts:GetCallerIdentity` — nothing else.
3. **GitHub** — under **Settings → Secrets and variables → Actions**:
   - Secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
   - Variables: `TF_STATE_BUCKET` (required), `AWS_REGION` (optional)

## Run an environment locally

```sh
cd infra/environments/dev
terraform init -backend-config="bucket=YOUR_STATE_BUCKET"
terraform plan
terraform apply
```

## Next DevOps upgrades (when ready)

- **OIDC instead of static keys** — drop the access-key secrets; have CI assume a
  role. Same least-privilege policy, attached to the role.
- **Promotion flow** — apply `dev` automatically, gate `prod` behind a manual
  approval using a GitHub Environment.
- **Quality gates** — add `terraform fmt -check`, `validate`, and `tflint` steps
  to the `infra` workflow.

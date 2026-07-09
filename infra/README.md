# infra/ — Terraform for the nama AWS account

Infrastructure as code, structured to grow. Reusable **modules** are composed by
per-**environment** root configs, with remote state in S3 and CI that plans on
pull requests and applies on a merge to `main`.

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
3. Open a PR → the **Infrastructure** workflow runs `terraform plan` (preview).
   Merge to `main` → it applies.

## How to add a new environment (e.g. prod)

```sh
cp -r infra/environments/dev infra/environments/prod
```
Then in `prod/`: set the backend `key` to `prod/terraform.tfstate`, set
`environment = "prod"`, tweak any tfvars, and add `prod` to the `matrix` in
[`.github/workflows/infra.yml`](../.github/workflows/infra.yml). Each environment
has its own isolated state.

## Database (RDS PostgreSQL)

`environments/dev` provisions a **private** PostgreSQL instance via the
[`rds-postgres`](modules/rds-postgres) module:

- `db.t4g.micro`, encrypted, **not publicly accessible** (no internet path), in
  the account's default VPC. TLS is enforced (`rds.force_ssl`).
- The master password is **generated** and the full SQLAlchemy URL is stored as
  an **SSM SecureString** at `/nama/dev/database-url`. Nothing secret is in code.
- Two security groups: a `db` SG that only accepts Postgres from an `app` SG, and
  that empty `app` SG (output `app_security_group_id`) for you to attach to
  compute later.

### Connecting the app

The app reads `DATABASE_URL` (see [`app/db.py`](../app/db.py)). Because the DB is
**private, your laptop can't reach it directly.** Two ways to connect:

- **App runs in AWS (the intended path):** deploy the FastAPI app into the VPC
  (ECS/EC2/Lambda), attach `app_security_group_id`, and inject the
  `/nama/dev/database-url` SecureString as the `DATABASE_URL` env var.
> **Bootstrap note:** the bastion is the first real EC2 instance in this stack
> (everything else is serverless Fargate), so `nama-ci` needs EC2-instance and
> instance-profile permissions it didn't before — the `ManageBastionEc2` and
> `ManageNamaInstanceProfiles` statements in
> [`ci-iam-policy.json`](ci-iam-policy.json). **Re-paste the updated policy onto
> the `nama-ci` user before the apply runs**, or the apply fails with
> `AccessDenied` on `iam:CreateInstanceProfile`.

- **Local access (the secure path):** tunnel to the private DB through the
  `module "bastion"` SSM jump host. It opens **no inbound ports** and has **no SSH
  key** — AWS Systems Manager brokers the connection, so the database never
  becomes publicly reachable. One-time local setup: install the AWS CLI's
  [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).

  The bastion is **kept running continuously** so the tunnel is always ready —
  there's no session to start first. Terraform owns its power state (the
  `aws_ec2_instance_state.bastion` resource holds it `running`, starting it on
  apply if anything ever stopped it), so there's nothing to toggle. It's a
  `t4g.nano` (~$7/mo) and is not part of the app's serving path, so none of this
  affects the API. (`bastion_enabled = false` in `environments/dev/variables.tf`
  removes it entirely — it's stateless, nothing is lost.)

  ```sh
  # Look both up live (no local terraform needed). The bastion keeps its id
  # across stop/start, but a bastion_enabled off/on cycle mints a new one —
  # so always query by tag rather than hardcoding.
  BASTION=$(aws ec2 describe-instances \
    --filters Name=tag:Name,Values=nama-dev-bastion Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].InstanceId' --output text)
  DBHOST=$(aws rds describe-db-instances \
    --query "DBInstances[?starts_with(DBInstanceIdentifier,'nama-dev')].Endpoint.Address" --output text)

  # Forward localhost:5432 -> RDS:5432 through the bastion. Leave this running.
  aws ssm start-session --target "$BASTION" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"$DBHOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"5432\"]}"
  ```

  In another shell, point any client (`psql`, DBeaver, TablePlus, …) at
  `localhost:5432`. The username/password/db name are in the
  `/nama/dev/database-url` SecureString (`aws ssm get-parameter --name
  /nama/dev/database-url --with-decryption --query Parameter.Value --output
  text`); use `sslmode=require` and swap the host for `localhost`.

### Cost & teardown

`db.t4g.micro` is free-tier eligible for 12 months; otherwise ~$12–15/mo while
running. It bills whether or not you use it — `terraform destroy` (or remove the
`module "database"` block and apply) when you're done experimenting.
`deletion_protection` is off and `skip_final_snapshot` is on for easy teardown.

The `module "bastion"` jump host is a `t4g.nano` + its public IPv4 + 8 GB gp3 —
roughly **$7/mo**, and it is now **left running continuously** so the database
tunnel is always available (Terraform holds it in the `running` state). To stop
paying the running rate for a while, set `bastion_enabled = false` (removes it —
it's stateless) for a durable pause; a manual `stop-instances` works too but the
next `terraform apply` starts it back up:

```sh
aws ec2 stop-instances --instance-ids "$(aws ec2 describe-instances \
  --filters Name=tag:Name,Values=nama-dev-bastion Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
```

> Provisioning RDS takes several minutes, so the `apply` step runs longer than
> the SSM-only deploys did.

## App on ECS Fargate

`environments/dev` runs the FastAPI app as a container via the
[`ecs-fargate-service`](modules/ecs-fargate-service) module:

- An **ECR** repo (image registry), an **ECS cluster + service + task
  definition**, an **API Gateway HTTP API** in front (per-request billing; it
  reaches the tasks through a free **VPC Link**, discovering their IPs via
  **Cloud Map**), security groups, two IAM roles, and a CloudWatch log group.
  This replaced a public ALB (~$24/mo in hourly + public-IPv4 charges to front
  one task). Two consequences: requests have a **hard 30s timeout** at the
  gateway, and the API is **HTTPS-only** (no port-80 redirect).
- The task carries the database's `app` security group (so it can reach Postgres)
  and gets **`DATABASE_URL` injected from the SSM SecureString** — the secret
  never appears in the task definition.
- The app already auto-creates its tables on startup (`Base.metadata.create_all`),
  so no migration step is needed for this simple schema.

### Stocks API credentials (Alpaca + Finnhub + Logo.dev)

The stocks feature reads four keys at runtime: the **Alpaca** key/secret (price
snapshot + performance), an optional **Finnhub** key (market cap + dividend), and
the **Logo.dev** publishable token (company logos for `GET /stocks/{symbol}/logo`).
All are created as **SSM SecureString** placeholders by the
[`ssm-secret`](modules/ssm-secret) module and injected into the task as
`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` / `FINNHUB_API_KEY` / `LOGODEV_TOKEN`
(via the app module's `extra_secrets`) — the same mechanism as `DATABASE_URL`.

The Logo.dev value is the **publishable** key (`pk_...`) only; it rides in the
image request URL. The separate Logo.dev *secret* key is not used by the app —
don't store it here unless we add the brand-search adapter.

Terraform never holds the real values: it creates the parameters with a
placeholder and **ignores value changes**. Set the real keys **once**, out of
band, so nothing secret touches the repo, CI, or Terraform state:

```sh
aws ssm put-parameter --overwrite --type SecureString \
  --name /nama/dev/alpaca-api-key-id     --value <YOUR_KEY_ID>
aws ssm put-parameter --overwrite --type SecureString \
  --name /nama/dev/alpaca-api-secret-key --value <YOUR_SECRET>
aws ssm put-parameter --overwrite --type SecureString \
  --name /nama/dev/finnhub-api-key       --value <YOUR_FINNHUB_KEY>
aws ssm put-parameter --overwrite --type SecureString \
  --name /nama/dev/logodev-token         --value <YOUR_LOGODEV_PUBLISHABLE_KEY>
```

Rotate the same way (`put-parameter --overwrite`), then force a new deployment
to pick up the value. Without the **Alpaca** keys set, the price endpoints (e.g.
`/stocks/ticker/{ticker}`)
return `503`. The **Finnhub** key is optional: until it's set (the placeholder
isn't a real key), market cap and dividend come back `null` and the rest of the
response is unaffected. Without the **Logo.dev** token, `/stocks/{symbol}/logo`
returns `503`; the rest of the app is unaffected.

### Deploy order (important)

Terraform creates the registry and service, but the service has **no image to run
until CI pushes one**. So:

1. **Update `nama-ci`'s policy** (it now needs ECS/ECR/API Gateway/Cloud
   Map/logs + IAM for `nama-*` roles; `elasticloadbalancing:*` is only still
   listed so the apply that removed the old ALB could destroy it — safe to drop
   once that has run) — re-paste [`ci-iam-policy.json`](ci-iam-policy.json), or
   use the managed `PowerUserAccess` policy **plus** the `ManageNamaRoles`
   statement.
2. **Merge → the [Infrastructure](../.github/workflows/infra.yml) workflow applies.**
   The ECS service comes up but its tasks can't start yet (no image) — expected.
3. **The [Build & Deploy App](../.github/workflows/app-image.yml) workflow** builds
   the Docker image, pushes it to ECR, and rolls the service. It runs automatically
   on the same merge (it touches `app/**`/`Dockerfile`) and **waits for the ECR
   repo to exist**, so it no longer needs a manual re-run after the Infrastructure
   run. The ECR repo keeps only recent images (untagged expire after 7 days; last
   10 kept).
4. Once a task is healthy, hit `terraform output app_url` (the custom domain,
   or an `https://…execute-api…` address before DNS is set up). `GET /healthz`
   should return `{"status":"ok"}`.

### Custom domain + HTTPS

The [`dns-cert`](modules/dns-cert) module issues a free, auto-renewing ACM
certificate (DNS-validated) and the app module adds an API Gateway custom
domain + an `api.namainsights.com` record pointing at it. HTTPS only — API
Gateway has no port-80 listener, so plain `http://` doesn't answer (the old
ALB used to redirect it).

- The hosted zone for `namainsights.com` already exists (registered via Route 53),
  so `create_hosted_zone = false` and it all comes up in one apply.
- If you ever move to a registrar outside AWS, set `create_hosted_zone = true`;
  Terraform creates the zone and outputs `name_servers` to set at the registrar,
  then a second apply validates the cert once DNS is live.
- Change the hostname via the `domain_name` / `parent_domain` variables.

### Cost & teardown

This tier is cheap but **not** free: the Fargate task is ~$9/mo plus ~$3.65/mo
for its public IPv4, on top of RDS. The API Gateway HTTP API bills per request
(~$1/M — effectively $0 at dev traffic; the VPC Link is free), the ACM cert and
Route 53 queries are negligible (the hosted zone is ~$0.50/mo). `terraform
destroy` (or remove the `module "app"` block and apply) when you're done.

## Frontend on S3 + CloudFront

`environments/dev` serves the frontend (a static SPA built by Vite) from a
**private S3 bucket behind CloudFront** via the
[`static-site-cloudfront`](modules/static-site-cloudfront) module —
`module "frontend"`. Serving static files this way costs cents at this scale; it
replaced an earlier setup that ran nginx on a Fargate task behind its own ALB
(~$25/mo for an always-on task + load balancer).

- **Private bucket, CloudFront-only.** All public access is blocked; an Origin
  Access Control (OAC) lets only this distribution read the bucket.
- **Apex + www (www is canonical).** Served at `namainsights.com` and
  `www.namainsights.com`. A dedicated `module "dns_frontend"` issues one ACM cert
  covering both names, and the module adds A + AAAA alias records for each,
  pointing at the distribution. `redirect_to_domain` (set to
  `frontend_canonical_domain`, `www.namainsights.com`) attaches a CloudFront
  viewer-request function that **301-redirects the apex to www**, so the site has
  a single canonical URL. One distribution serves both — the redirect is an edge
  function, not a second bucket/distribution.
- **us-east-1 cert.** CloudFront only reads ACM certs from `us-east-1`. This
  stack already deploys there, so `module.dns_frontend`'s cert works as-is — but
  if you ever move the stack to another region, the cert must still be issued in
  `us-east-1`.
- **SPA routing.** 403/404 from S3 are rewritten to `index.html` (200) so
  client-side deep links resolve.

### Deploy order

Terraform creates the bucket + distribution (empty at first — CloudFront serves
403/`index.html` until a build is uploaded). Then the frontend repo's own GitHub
Action **uploads the build and invalidates the distribution** instead of building
a Docker image:

```sh
aws s3 sync ./dist "s3://$(terraform output -raw frontend_bucket_name)" --delete
aws cloudfront create-invalidation \
  --distribution-id "$(terraform output -raw frontend_distribution_id)" \
  --paths "/*"
```

That CI needs `s3:*` on `arn:aws:s3:::nama-frontend-*` (+ `/*`) and
`cloudfront:CreateInvalidation` — both already covered if it reuses the `nama-ci`
keys (see the policy change below). After the first upload,
`terraform output frontend_url` → `https://www.namainsights.com` (the canonical
host; the apex redirects to it).

> **Migrating from the old ECS frontend:** this is a cross-repo cutover. The
> frontend repo's deploy must switch from *docker build → push to
> `nama-frontend-dev` ECR → roll ECS* to the `s3 sync` + invalidation above, at
> the same time this applies. The old `nama-frontend-dev` ECR repo, ECS
> cluster/service, ALB, and IAM roles are destroyed by this change.

### IAM

`nama-ci` now also needs to manage the bucket and distribution. `ci-iam-policy.json`
adds `s3:*` scoped to `arn:aws:s3:::nama-frontend-*` and `cloudfront:*` — **re-apply
the updated policy to the `nama-ci` user** before the first apply (see Bootstrap).

### Cost & teardown

Effectively free at low traffic (CloudFront has a perpetual free tier; S3 storage
for a built SPA is pennies). `terraform destroy` (or remove `module "frontend"` +
`module "dns_frontend"` and apply) when you're done.

## Bootstrap (one-time per account)

1. **State bucket** — an S3 bucket, **versioned**, **encrypted**, **public access
   blocked**. Terraform can't create the bucket that holds its own state, so make
   this one by hand (console or CLI).
2. **CI user + policy** — attach [`ci-iam-policy.json`](ci-iam-policy.json) to the
   deploy IAM user, replacing `YOUR_STATE_BUCKET`. It grants SSM on `/nama/*`,
   the state bucket, `sts:GetCallerIdentity`, `rds:*` + EC2 SG/VPC actions (database),
   ECS/ECR/API Gateway/Cloud Map/logs + IAM scoped to `nama-*` roles (the app), and `s3:*` on
   `nama-frontend-*` + `cloudfront:*` (the static frontend). Broad on
   services, careful on IAM. Re-paste it whenever this file changes — or attach
   the managed `PowerUserAccess` policy plus the `ManageNamaRoles` statement.
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
  to the Infrastructure workflow.

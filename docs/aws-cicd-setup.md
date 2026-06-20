# AWS CI/CD setup guide

This is the one-time AWS setup that makes the two GitHub Actions pipelines work:

- `.github/workflows/deploy-backend.yml` — Go backend → **ECS (Fargate)**
- `.github/workflows/deploy-frontend.yml` — TS frontend → **S3 + CloudFront**

Work top-to-bottom. Each step names the AWS concept it teaches. You can do all
of this in the AWS Console (clicking) the first time — that's the best way to
learn what each piece is. Automate it later if you want.

> 💡 **Do step 0 first.** Set a billing alarm in **AWS Budgets** before anything
> else, and create a non-root **IAM user** for yourself instead of using the
> root account. This project fits comfortably in the free tier, but the alarm is
> your safety net.

---

## 1. ECR — a home for your Docker images

**Concept: container registry** (like a private Docker Hub).

Create one repository named **`nama-backend`**:

```bash
aws ecr create-repository --repository-name nama-backend --region us-east-1
```

The CI pipeline builds your image and `docker push`es it here.

---

## 2. CloudWatch Logs — where container logs go

**Concept: centralized logging.** ECS streams your Go app's stdout here.

```bash
aws logs create-log-group --log-group-name /ecs/nama-backend --region us-east-1
```

(This matches the `awslogs-group` in `ecs-task-definition.json`.)

---

## 3. ECS on Fargate — the thing that runs your container

**Concept: container orchestration.** ECS keeps your container running, restarts
it if it crashes, and can run multiple copies. **Fargate** means "I don't manage
any servers — AWS provides the compute." You'll create, in order:

1. **A cluster** (`nama-cluster`) — a logical group your service lives in.
2. **An Application Load Balancer (ALB)** — the public front door. It health-checks
   `/healthz` and only sends traffic to healthy containers. This is what gives the
   backend a stable public URL.
3. **A service** (`nama-backend-service`) — says "always keep N copies of this task
   running behind the load balancer." This is what the pipeline updates each deploy.

Easiest path the first time: AWS Console → ECS → **Create cluster** (Fargate) →
then **Create service**, attaching the load balancer and pointing the health check
at `/healthz` on port `8080`. The console wizard creates the security groups and
networking for you.

> The names `nama-cluster` / `nama-backend-service` / container `nama-backend`
> must match the `env:` block in `deploy-backend.yml` and the `family` /
> container name in `ecs-task-definition.json`. Change them in one place and
> they must change in all.

---

## 4. Two IAM roles for ECS

**Concept: IAM roles = identities for machines** (not people).

- **Execution role** (`nama-ecs-execution-role`): lets ECS pull the image from ECR
  and write logs to CloudWatch. Attach the AWS-managed policy
  `AmazonECSTaskExecutionRolePolicy`. Put its ARN in `ecs-task-definition.json`
  (replace `REPLACE_AWS_ACCOUNT_ID`).
- **Task role** (optional, add later): permissions your *app code* needs — e.g.
  reading from S3 or a database secret. Empty for now.

---

## 5. GitHub OIDC role — keyless deploys (the important security step)

**Concept: OIDC federation.** Instead of storing an AWS access key in GitHub,
GitHub proves its identity to AWS for the ~2 minutes a deploy runs, and AWS hands
back temporary credentials. Nothing long-lived can leak.

1. **Add GitHub as an identity provider** in IAM (one-time per AWS account):
   - Provider URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`
2. **Create a role** (e.g. `nama-github-deploy`) that trusts that provider, scoped
   to *your* repo so no other repo can assume it. Trust policy condition:

   ```json
   "StringLike": {
     "token.actions.githubusercontent.com:sub": "repo:jushy10/nama_backend:*"
   }
   ```
3. **Give the role permissions** to do the deploy: push to ECR, register/deploy ECS
   task definitions, `iam:PassRole` for the execution role, and (for the frontend)
   `s3:*` on your bucket + `cloudfront:CreateInvalidation`. Start broad while
   learning, then tighten.
4. Copy the role's ARN — it goes in the `AWS_ROLE_ARN` secret below.

---

## 6. Frontend: S3 + CloudFront (do when you add the frontend)

- **S3 bucket** — stores the built static files. Put its name in `S3_BUCKET` in
  `deploy-frontend.yml`.
- **CloudFront distribution** — CDN + HTTPS in front of the bucket. Copy its
  **Distribution ID** into the `CLOUDFRONT_DISTRIBUTION_ID` secret.
- Lock the bucket to "block all public access" and let CloudFront reach it via an
  **Origin Access Control** — users hit CloudFront, never S3 directly. (Concept:
  private origin behind a CDN.)

---

## 7. Tell GitHub the values

In your GitHub repo → **Settings → Secrets and variables → Actions → Secrets**,
add:

| Secret name                     | Value                                              | Used by   |
| ------------------------------- | -------------------------------------------------- | --------- |
| `AWS_ROLE_ARN`                  | ARN of the role from step 5                        | both      |
| `CLOUDFRONT_DISTRIBUTION_ID`    | Your CloudFront distribution ID                    | frontend  |

The bucket name, region, and ECS names live in the workflow `env:` blocks (they
aren't secret, so they're easier to read inline).

---

## 8. First deploy

```bash
# Replace REPLACE_AWS_ACCOUNT_ID in ecs-task-definition.json with your account ID,
# commit everything, then:
git push origin main
```

Watch it run under your repo's **Actions** tab. The first ECS rollout takes a few
minutes while it pulls the image and passes health checks. When the job goes green,
hit your load balancer's URL — you should see the JSON from `handleRoot`.

### Recommended order to avoid frustration

1. Steps 0–5, then push → get the **backend** deploying green first.
2. Add the `frontend/` app, do step 6, then push → frontend pipeline wakes up.

---

## Quick troubleshooting

| Symptom                                   | Usual cause                                                        |
| ----------------------------------------- | ------------------------------------------------------------------ |
| `Not authorized to perform sts:AssumeRole`| Trust policy `sub` doesn't match `repo:jushy10/nama_backend:*`      |
| ECR `push` denied                         | Role missing ECR permissions, or repo name mismatch                |
| ECS service never stabilizes              | Container failing health check — check CloudWatch `/ecs/nama-backend` logs |
| `PassRole` error on deploy                | Deploy role needs `iam:PassRole` for the execution role            |
| Frontend shows old version                | CloudFront cache — the invalidation step covers this               |

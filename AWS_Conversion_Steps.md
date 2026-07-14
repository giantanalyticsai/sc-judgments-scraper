# Converting the SC Judgments Scraper for AWS — Step-by-Step Plan

**Repo:** `giantanalyticsai/sc-judgments-scraper`
**Goal:** Run the scraper as an unattended **daily Fargate task**, triggered by **EventBridge Scheduler**, writing output to **S3**, observable via **CloudWatch**.
**Target infra repos:**
- App/environment stacks: `giantanalyticsai/tf-aws`
- Reusable modules: `giantanalyticsai/tf-modules-aws`

> This version is aligned to the **actual conventions** in `tf-aws` / `tf-modules-aws` after reviewing them. Key changes from the first draft are flagged inline as **[repo-aligned]**.

---

## What the review of the Terraform repos changed

| Discovery in `tf-aws` | Impact on this plan |
|---|---|
| **Output bucket already exists**: `indian-supreme-court-cases-and-judgement-dev`, managed in `giantanalyticsai/dev-121452789073/config/s3.tf` (`aws_s3_bucket.supreme_court_dev`). | **Step 6 (S3 bucket) is largely done for dev.** We reference the existing bucket; we do *not* create a new one. Only add versioning/lifecycle if wanted. |
| **Direct sibling precedent**: `bharatlaw-etl` — a legal-judgments scraper running as **AWS Batch on Fargate** in `dev/batch/`, image `121452789073.dkr.ecr.ap-south-1.amazonaws.com/bharatlaw-etl:latest`, writing to an S3 bucket. Also `district-court-judgments` bucket exists. | We follow the sibling's shape: **account-local ECR image + Fargate + S3**, dev account first. |
| **Canonical CronJob pattern** (`docs/20-ecs-v2-app-stack.md` §9.1): *"CronJob → EventBridge Scheduler → `RunTask` target."* | **Compute choice = EventBridge Scheduler → ECS Fargate `RunTask`** (see "Compute decision" below). |
| **Hub-and-spoke networking** (`docs/10-architecture.md`, ADR-0001): spoke VPCs (dev/prd) have **no IGW/NAT**; egress goes via the net account's Transit Gateway → centralized NAT. Spokes keep local VPC endpoints (ECR, S3, CloudWatch, SSM). | The task runs in **dev private subnets, `assignPublicIp=DISABLED`**; outbound to `scr.sci.gov.in` exits via TGW→NAT; S3 via the gateway endpoint. (The `bharatlaw-etl` batch stack took a Default-VPC shortcut — we use the proper spoke VPC.) |
| **Conventions**: region `ap-south-1`; dev account `121452789073` (profile `dev`), prd `680164043825` (profile `prd`); state bucket `giantanalyticsai-{env}-terraform-state`, key `{env}/{area}/{service}/terraform.tfstate`, `use_lockfile = true`; modules pinned `?ref=vX.Y.Z`; standard files `backend.tf`/`providers.tf`/`locals.tf`/`main.tf`/`outputs.tf`; `README.md` auto-generated; `make plan/apply DIR=… ENV=…`. | All new stacks follow these exactly. |

---

## Current status of the app repo (baseline)

| Area | State |
|---|---|
| `--daily` / `--offset` (`scrape.py`) | **Done** (uses `TZ` env for "today") |
| Captcha model baked into image (`Dockerfile` → `fetch_model.py`) | **Done** |
| Multi-stage Dockerfile + `.dockerignore`, non-root | **Done** |
| **S3 storage backend** (`src/storage.py`) | **Not done** — local filesystem only |
| **`boto3` dependency** | **Not done** — absent from `pyproject.toml` / `uv.lock` |
| AWS infra (ECR / IAM / task def / schedule / logs) | **Not done** |

**Blocker:** Fargate's local disk is ephemeral. Until output goes to S3, a cloud run scrapes correctly and then throws everything away on exit. **Steps 1–3 are the critical path.**

> The existing `docker-compose.yml` / `schedule.sh` / `docker-entrypoint.sh` are a **local-only** scheduler. On AWS they are replaced by EventBridge + ECS and become dev-only conveniences. Keep them; they don't ship in the cloud path.

---

## Compute decision: EventBridge Scheduler → ECS Fargate `RunTask`

The org has two batch patterns in use:
- **AWS Batch on Fargate** (`bharatlaw-etl`) — best for queued/fan-out/dependency workloads (its decade backfill submits chained jobs). Overkill for one small daily task.
- **EventBridge Scheduler → ECS `RunTask`** — documented as *the* CronJob equivalent.

**Recommendation:** EventBridge Scheduler → ECS `RunTask`. It is a genuine daily cron of a single short task, which is exactly what the documented pattern targets, and it keeps the footprint minimal (no compute environment / job queue to own).

**Alternative (if you prefer to mirror the sibling exactly):** EventBridge Scheduler → **AWS Batch `SubmitJob`**, reusing a Batch stack modeled on `dev/batch/`. Note where they diverge: Batch adds a compute environment + job queue; the sibling submits jobs via a `null_resource local-exec`, which is **not** a schedule — we'd still add EventBridge for the daily trigger. Decide this before Phase C.

---

## Phase A — Code changes (this repo)

### Step 1 — Add an S3 storage backend
- In `src/storage.py`, branch `Storage` on whether `output_dir` starts with `s3://`; keep the local-`Path` behaviour unchanged (dev + tests stay working).
- S3 branch (boto3):
  - `already_downloaded` → `head_object` (keeps daily runs idempotent / resumable).
  - `save` → `put_object` for both the PDF and the metadata JSON.
  - `failures.json` read/write against an S3 key, so `--retry-failures` survives across task runs.
- Parse `s3://bucket/prefix`; mirror the on-disk layout as key `<prefix>/<year>/<name>.pdf|.json`.
- Rely on the task role for credentials (no keys in code/env).

### Step 2 — Add the `boto3` dependency
- Add `boto3>=1.34` to `pyproject.toml` `dependencies`; re-run `uv lock` (reproducible Docker builds).

### Step 3 — Local test against the real dev bucket
- With dev SSO creds (`aws sso login --profile dev`):
  `python scrape.py --daily --output-dir s3://indian-supreme-court-cases-and-judgement-dev/sc`
- Confirm objects land in S3, and a second run skips already-present objects (`head_object` path).

---

## Phase B — Image

### Step 4 — Build amd64 and verify S3 write
- `docker build --platform linux/amd64 -t sc-judgments-scraper .` (Fargate is amd64).
- Confirm the captcha model is baked in (no GitHub access at runtime).
- Run the container with `--output-dir s3://…-dev/sc` + dev creds; confirm objects land in S3.

---

## Phase C — AWS infrastructure (Terraform in `tf-aws`)

Target **dev account `121452789073`** first, region `ap-south-1`. Prod (`680164043825`) is a later copy. Proposed stack location, following the `legal/` service-area convention (where the legal-data buckets and `legal/ecr` already live):

```
tf-aws/giantanalyticsai/dev-121452789073/legal/sc-scraper/
  backend.tf   providers.tf   locals.tf   main.tf   outputs.tf
```
Backend key: `dev/legal/sc-scraper/terraform.tfstate`, bucket `giantanalyticsai-dev-terraform-state`, `use_lockfile = true`.
(Alternatively mirror the top-level `batch/` placement if you go the Batch route.)

### Step 5 — ECR repository
- Add `sc-judgments-scraper` to an ECR stack using the pinned module, matching `dev/legal/ecr` (module `?ref=v5.9.0`, `MUTABLE` tags in dev, lifecycle policy on):
  ```hcl
  module "ecr" {
    source           = "git::ssh://git@github.com/giantanalyticsai/tf-modules-aws.git//modules/ecr?ref=v5.9.0"
    repository_names = ["sc-judgments-scraper"]
    image_tag_mutability = "MUTABLE"
    scan_on_push         = false
    encryption_type      = "AES256"
    enable_lifecycle_policy       = true
    untagged_image_retention_days = 7
    tagged_image_count            = 30
    tags = {}
  }
  ```
  Add the repo name to `dev/legal/ecr/locals.tf` (preferred — reuse the existing stack) rather than a new ECR stack.
- Build `--platform linux/amd64`, tag, `docker push` to `121452789073.dkr.ecr.ap-south-1.amazonaws.com/sc-judgments-scraper:<tag>`.

### Step 6 — S3 output bucket (mostly done)
- **Dev bucket already exists**: `indian-supreme-court-cases-and-judgement-dev` (managed in `dev/config/s3.tf`). No new bucket for dev — the task role just needs access to it.
- Optional hardening in `dev/config/s3.tf`: add versioning / a lifecycle rule per retention policy (other buckets there already do this).
- **For prod later**: add an `indian-supreme-court-cases-and-judgement` (or `-prd`) bucket in `prd/…/config/s3.tf` following the same encryption + public-access-block block.
- Key prefix: `<prefix>/<year>/*.pdf|.json`; `failures.json` at a fixed key.

### Step 7 — IAM roles (stack-owned, `ecs-tasks` trust)
Create the roles **in this stack** (do not reuse EKS IRSA — the `ecs-v2` isolation principle):
- **Task execution role** — `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch Logs).
- **Task role** — least privilege on the one bucket:
  `s3:PutObject`, `s3:GetObject` on `arn:aws:s3:::indian-supreme-court-cases-and-judgement-dev/*` and `s3:ListBucket` on the bucket ARN (Get/List needed for skip-existing + retry).

### Step 8 — ECS cluster + Fargate task definition
- Small ECS cluster for scheduled tasks (Container Insights on), or reuse an existing dev cluster if one is designated for jobs.
- Task definition:
  - Image: the ECR URI from Step 5.
  - Size: **0.25–0.5 vCPU / 1 GB** (I/O-bound).
  - `executionRoleArn` + `taskRoleArn` from Step 7.
  - `awslogs` driver → a `/ecs/sc-judgments-scraper` (or `/aws/…`) CloudWatch log group.
  - Env: `TZ=Asia/Kolkata`, `OUTPUT_DIR=s3://indian-supreme-court-cases-and-judgement-dev/sc` (passed as env or command arg).
  - Command `["--daily"]` (add `--offset 1` if the portal publishes late — config flip).
  - Default ephemeral storage (20 GB) is sufficient.

### Step 9 — Networking (spoke VPC, private subnets)
- Run in the **dev spoke VPC private subnets** via `terraform_remote_state` of `dev/vpc` (`private_subnet_ids`), `assignPublicIp=DISABLED`.
- Security group: **egress 443 only**, no inbound (it's a task, not a service). Outbound to `scr.sci.gov.in` exits via TGW → net-account NAT.
- S3 stays on the spoke's **gateway VPC endpoint** (already present per the architecture doc); ECR/CloudWatch use the local interface endpoints.

### Step 10 — Manual `RunTask` (verification gate)
- `aws ecs run-task --cluster … --task-definition … --launch-type FARGATE --network-configuration 'awsvpcConfiguration={subnets=[<dev-private-subnet>],securityGroups=[<task-sg>],assignPublicIp=DISABLED}' --region ap-south-1 --profile dev`
- Verify a full day lands in S3 and logs appear in CloudWatch **before** automating.

### Step 11 — EventBridge Scheduler (the trigger)
- Schedule expr `cron(30 23 * * ? *)`, timezone **`Asia/Kolkata`**.
- Target: **ECS `RunTask`** (native target — no Lambda). Include cluster, task def, `LaunchType=FARGATE`, and the Step 9 network config.
- Scheduler needs an IAM role allowing `ecs:RunTask` + `iam:PassRole` on the task/execution roles.

### Step 12 — Logging & monitoring
- Per-run logs in the CloudWatch log group from Step 8.
- CloudWatch alarm on task **non-zero exit** (scraper exits 1 when throttled) and/or a metric filter on the "aborted" log line, wired to the existing SNS/alerting used by `mgmt/cost-management` or equivalent.
- Optional: a second EventBridge schedule a few hours later running `--retry-failures`.

---

## Rollout order

1. **Steps 1–3** — S3 backend + `boto3` + local test against `…-dev` bucket. *(critical path)*
2. **Step 4** — amd64 image writes to S3.
3. **Decide** ECS `RunTask` vs Batch `SubmitJob` (Compute decision).
4. **Steps 5, 7–9** — ECR repo, IAM, cluster/task def, networking. *(Step 6 already done for dev.)*
5. **Step 10** — manual `run-task` proves end-to-end.
6. **Step 11** — EventBridge schedule; let it fire once on its own.
7. **Step 12** — alarm + optional retry schedule.
8. **Prod** — repeat Phase C in the `prd-680164043825` tree with a prod bucket.

Terraform ops per stack:
```bash
aws sso login --profile dev
make plan  DIR=giantanalyticsai/dev-121452789073/legal/sc-scraper ENV=dev
make apply DIR=giantanalyticsai/dev-121452789073/legal/sc-scraper ENV=dev APPROVE=true
```

---

## Deliverables

**Code (`sc-judgments-scraper`):**
- S3 output backend in `src/storage.py`
- `boto3` dependency + updated `uv.lock`
- (Dockerfile / `.dockerignore` / `--daily` already present)

**Infra (`tf-aws`, dev first then prd):**
- ECR repo `sc-judgments-scraper` (add to `legal/ecr`)
- New stack `legal/sc-scraper/`: IAM (exec + task roles), ECS cluster + Fargate task def, security group, CloudWatch log group, EventBridge schedule + scheduler role, alarm
- S3: reference existing `indian-supreme-court-cases-and-judgement-dev` (optional versioning/lifecycle); add prod bucket for prd

**Modules (`tf-modules-aws`):**
- Reuse `ecr`. No new module strictly required for a single scheduled task (define resources in the stack, as `batch/` does). If this pattern will recur, consider a small `scheduled-fargate-task` module later.
```

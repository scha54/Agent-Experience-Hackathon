# CraveQuest — Deployment & Developer Guide

CraveQuest makes a genuine Strands Decider 2B cooking-strategy decision
(`#tool-selection`) and then generates recipe content with Amazon Bedrock. This
guide covers everything a new contributor or operator needs to run CraveQuest
locally, deploy it to AWS, tear it down, and keep spend under control.

> **Cost notice up front:** deploying CraveQuest to AWS creates **paid**
> resources (Amazon Bedrock, a container service, Amazon API Gateway, AWS
> Amplify Hosting, and Amazon CloudWatch). This deployment is **not free of
> charge.** See [Paid-resource inventory](#paid-resource-inventory) and the
> [Budget-monitoring checklist](#budget-monitoring-checklist) before you deploy.

## Contents

1. [Repository layout](#repository-layout)
2. [Prerequisites](#prerequisites)
3. [Local development](#local-development)
   - [Backend](#backend-local)
   - [`DECIDER_MODE=mock` (Mock_Mode)](#decider_modemock-mock_mode)
   - [Frontend](#frontend-local)
   - [Running the benchmark](#running-the-benchmark)
4. [Environment variable reference](#environment-variable-reference)
5. [Credential setup](#credential-setup)
6. [Bedrock model-access instructions](#bedrock-model-access-instructions)
7. [Deploying to AWS](#deploying-to-aws)
8. [Teardown](#teardown)
9. [Lambda-vs-container rationale](#lambda-vs-container-rationale)
10. [Paid-resource inventory](#paid-resource-inventory)
11. [Budget-monitoring checklist](#budget-monitoring-checklist)
12. [Troubleshooting](#troubleshooting)

---

## Repository layout

```
backend/     Python/FastAPI service: decision module, strategy tools,
             Bedrock generator, dietary safety checker, metrics (see backend/README.md)
frontend/    React + TypeScript + Vite web client
infra/       AWS CDK (TypeScript) stack — the deployable topology (see infra/README.md)
scripts/     benchmark_decisions.py — reproducible Decider-vs-baseline measurement harness
docs/        This guide and the demo scenario
```

---

## Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11+ | Backend + benchmark script |
| Node.js | 18+ (20 LTS recommended) | Frontend build and the CDK infrastructure |
| AWS CLI | v2 | Credentials, Bedrock model access, deployment |
| AWS CDK CLI | v2 (`npm i -g aws-cdk` or `npx cdk`) | Synth/deploy/destroy of `infra/` |
| Docker | recent | Building the Backend_API + Decider_2B container image |

You also need an AWS account with permission to deploy the stack and to enable
Amazon Bedrock model access in your target region.

---

## Local development

### Backend (local)

From `backend/`:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements-dev.txt
cp .env.example .env          # then fill in the required values
```

Edit `.env` and set at least the three required, no-default variables
(`DECIDER_MODEL`, `BEDROCK_MODEL_ID`, `ALLOWED_ORIGINS`). The app validates
configuration at startup and fails fast with a message naming any missing or
out-of-range value, so a misconfigured `.env` is caught immediately rather than
at request time.

Run the API:

```bash
uvicorn app.main:app --reload --port 8000
```

Run the test suite (pytest + Hypothesis property tests):

```bash
pytest
```

### `DECIDER_MODE=mock` (Mock_Mode)

You do **not** need the real 2-billion-parameter Decider server running to
develop against CraveQuest. Set:

```bash
# in backend/.env
DECIDER_MODE=mock
```

In Mock_Mode the `MockDeciderAdapter` returns a deterministic, explicitly
**labeled** strategy with `is_mock=true` and **no genuine confidence** — so the
API omits `decision_confidence` and the UI never presents the result as a real
Decider_2B inference. This lets the full frontend-to-backend path be exercised
without the model. Mock output is only ever produced when `DECIDER_MODE=mock`
is set explicitly; the default is `real`.

For a fully real local decision path instead, run the Strands Decider server
(`strands-decider serve`) and point `STRANDS_DECIDER_API_BASE` at it (default
`http://127.0.0.1:8000`), keeping `DECIDER_MODE=real`.

### Frontend (local)

From `frontend/`:

```bash
npm install
cp .env.example .env          # set VITE_API_BASE_URL to your local backend
npm run dev                   # Vite dev server, default http://localhost:5173
```

The frontend `.env` holds **only** the public backend URL and a favorites cap.
It must never contain AWS credentials — the browser talks solely to the public
API URL and all AWS access happens server-side.

Make sure the backend's `ALLOWED_ORIGINS` includes the Vite origin
(`http://localhost:5173` by default) or browser requests will be blocked by
CORS.

Frontend tests:

```bash
npm run test        # vitest
```

### Running the benchmark

The reproducible benchmark compares Decider_2B strategy selection against the
named deterministic-fallback baseline using identical inputs, and reports sample
count, median, and p95 per measurement. It runs in Mock_Mode by default, so it
needs no live Decider server:

```bash
python scripts/benchmark_decisions.py --sample-size 100
python scripts/benchmark_decisions.py --sample-size 10 --seed 7 --json
python scripts/benchmark_decisions.py --decider real   # against a live Decider server
```

> **Honesty rule:** do not state any performance improvement that the benchmark
> does not actually measure. The benchmark is a measurement harness, not a
> guarantee.

---

## Environment variable reference

Every backend configuration value is read from the environment (optionally
seeded from `backend/.env`) and validated at startup. The three variables marked
**required** have no default and the app refuses to start without them. The
confidence threshold must fall within `[0.0, 1.0]` or startup fails with the
offending value reported.

### Backend configuration

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| Decider mode | `DECIDER_MODE` | `real` | `real` uses the live Decider_2B server; `mock` uses the labeled Mock_Mode adapter (no genuine inference). |
| Decider checkpoint | `DECIDER_MODEL` | **required** | Decider_2B checkpoint/model id passed to the provider. No default, never hardcoded. |
| Decider API base | `STRANDS_DECIDER_API_BASE` | `http://127.0.0.1:8000` | Base URL of the Decider_2B server. |
| Decider API key | `STRANDS_DECIDER_API_KEY` | _(unset)_ | Optional bearer token for the Decider server; leave blank if none is required. |
| Decision timeout | `DECISION_TIMEOUT_MS` | `5000` | Per-decision call timeout in ms. A timeout past this triggers the deterministic fallback. |
| Confidence threshold | `DECISION_CONFIDENCE_THRESHOLD` | `0.5` | Must be within `[0.0, 1.0]`. A genuine confidence below it is recorded (`below_threshold`) but does **not** by itself force a fallback. |
| Bedrock model id | `BEDROCK_MODEL_ID` | **required** | Amazon Bedrock model id used for generation (and for IAM scoping). No default. |
| Bedrock retries | `BEDROCK_MAX_RETRIES` | `3` | Single source of truth for Bedrock retry count (reconciles the resilience default of 3 and the cost-control intent). Lower it to tighten cost. |
| Bedrock backoff (initial) | `BEDROCK_BACKOFF_INITIAL_MS` | `500` | Initial exponential-backoff delay between Bedrock attempts, in ms. |
| Bedrock backoff (max) | `BEDROCK_BACKOFF_MAX_MS` | `8000` | Upper bound on the backoff delay, in ms. |
| Bedrock attempt timeout | `BEDROCK_ATTEMPT_TIMEOUT_MS` | `30000` | Per-attempt Bedrock call timeout, in ms. |
| Max available time | `MAX_AVAILABLE_TIME_MINUTES` | `1440` | Upper bound for the request `available_time_minutes` field. |
| Max servings | `MAX_SERVINGS` | `20` | Upper bound for the request `servings` field. |
| Max string length | `MAX_STRING_LEN` | `200` | Max length of any single input string. |
| Max list length | `MAX_LIST_LEN` | `50` | Max element count of any input list (e.g. ingredients, equipment). |
| Request body cap | `MAX_BODY_BYTES` | `65536` | Per-recipe-endpoint request body cap in bytes (64 KB). Over-cap bodies return 413. |
| Global body cap | `MAX_GLOBAL_BODY_BYTES` | `262144` | Global request body cap in bytes (256 KB). |
| Allowed CORS origins | `ALLOWED_ORIGINS` | **required** | Comma-separated list of allowed browser origins. No default. |
| Per-session request limit | `MAX_SESSION_REQUESTS` | `20` | Max recipe requests allowed per session. |
| Metrics access token | `METRICS_ACCESS_TOKEN` | _(unset)_ | Optional credential protecting `GET /api/metrics/summary`. When unset, the endpoint is not publicly exposed. |
| No-cook max prep | `NO_COOK_MAX_PREP_MINUTES` | `10` | Upper bound on `prep_time_minutes` for no-cook recipes. |
| Quick-recipe max extras | `QUICK_MAX_EXTRA_INGREDIENTS` | `5` | Max additional (non-supplied) ingredients a quick recipe may add. |
| Leftover-remix max extras | `LEFTOVER_MAX_EXTRA_INGREDIENTS` | `3` | Max additional ingredients a leftover remix may add. |
| Max explanation length | `MAX_EXPLANATION_CHARS` | `600` | Max length of the user-facing `reasoning_summary` (no chain-of-thought exposed). |

### Frontend configuration

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| API base URL | `VITE_API_BASE_URL` | `http://localhost:8000` | Public base URL of the Backend_API. **Public URL only — never put AWS credentials here.** |
| Max favorites | `VITE_MAX_FAVORITES` | `50` | Max favorite recipes stored in browser local storage. |

### Infrastructure (CDK) configuration

Deploy-time values are supplied via CDK context (`-c key=value`) or environment
variables — never committed secrets. See [`infra/README.md`](../infra/README.md)
for the full table; the most important are:

| Context key / Env var | Meaning | Default |
|---|---|---|
| `bedrockModelId` / `BEDROCK_MODEL_ID` | Bedrock model id used for generation and IAM scoping | `anthropic.claude-3-5-sonnet-20240620-v1:0` |
| `allowedOrigins` / `ALLOWED_ORIGINS` | CORS origins for the Backend_API | `http://localhost:5173` |
| `deciderMode` / `DECIDER_MODE` | `real` or `mock` | `real` |
| `logRetentionDays` / `LOG_RETENTION_DAYS` | CloudWatch log retention | `30` |

---

## Credential setup

CraveQuest uses no committed secrets. `.env` files are git-ignored; only
`.env.example` templates with placeholder values are tracked. Supply real values
at runtime.

**Local development.** Configure AWS credentials for the AWS CLI / SDK so the
backend can call Bedrock:

```bash
aws configure           # or: aws configure sso
# sets ~/.aws/credentials and ~/.aws/config
```

The backend's boto3 client picks up these credentials automatically. Set your
target region (for example `AWS_REGION=us-east-1`) to match the region where you
enabled Bedrock model access.

**Deployed environment.** The browser never holds AWS keys. In AWS the
container task runs under a **least-privilege IAM task role** (Bedrock
`InvokeModel`/`Converse` on the configured model plus writes to its own
CloudWatch log group — no wildcard or admin actions). Runtime secrets such as a
Decider bearer token are injected from AWS Secrets Manager / SSM Parameter Store,
not baked into the image or committed.

Never commit a populated `.env`, AWS keys, or a Decider API key.

---

## Bedrock model-access instructions

Amazon Bedrock requires you to **enable model access per account and per region**
before the `converse` API will accept calls for a given model.

1. Open the Amazon Bedrock console in your **target region** (the same region
   you will deploy to and set in `AWS_REGION`).
2. Go to **Model access** and request/enable access for the model you plan to
   use (the model whose id you set in `BEDROCK_MODEL_ID`).
3. Wait until the model status shows **Access granted**.
4. Confirm the id in `BEDROCK_MODEL_ID` matches an enabled model in that region.
   Some models are only invokable via an inference profile ARN — if so, set the
   CDK `bedrockModelArns` context accordingly so the IAM policy is scoped
   correctly.

If model access is not enabled, generation calls fail and the frontend surfaces
a distinct **model-access-not-configured** state. See
[Troubleshooting](#troubleshooting).

---

## Deploying to AWS

The deployable topology is defined in `infra/` with **AWS CDK (TypeScript)**. On
success the stack exports two outputs — the **frontend URL** (Amplify) and the
**API URL** (API Gateway).

1. **Enable Bedrock model access** in your target region (above).
2. **Configure AWS credentials** for the deploying account (above).
3. **Bootstrap** the CDK environment once per account/region:

   ```bash
   cd infra
   npm install
   npx cdk bootstrap
   ```

4. **Review** what will be created (including the IAM policies):

   ```bash
   npm run synth      # cdk synth — inspect the CloudFormation template
   npm run diff       # cdk diff  — compare against the deployed stack
   ```

5. **Deploy:**

   ```bash
   npm run deploy     # cdk deploy
   ```

   Example with overrides:

   ```bash
   npx cdk deploy \
     -c bedrockModelId=anthropic.claude-3-5-sonnet-20240620-v1:0 \
     -c allowedOrigins=https://main.d123.amplifyapp.com
   ```

6. Note the stack outputs (`FrontendUrl`, `ApiUrl`). Set the frontend's
   `VITE_API_BASE_URL` to `ApiUrl` and ensure the backend's `ALLOWED_ORIGINS`
   includes `FrontendUrl`.

> Deploying creates paid resources. Set an AWS Budget and billing alarm **before**
> deploying — see the [Budget-monitoring checklist](#budget-monitoring-checklist).

---

## Teardown

Remove all deployed AWS resources to stop ongoing charges:

```bash
cd infra
npm run destroy        # cdk destroy
```

This deletes the Amplify app, API Gateway, the container service, CloudWatch log
groups, and IAM roles created by the stack. After teardown, confirm in the AWS
console (Cost Explorer / the service dashboards) that no running tasks or
hosting resources remain. Bedrock itself is pay-per-use and has nothing to tear
down, but double-check there are no lingering Fargate tasks or Amplify builds.

---

## Lambda-vs-container rationale

Requirement 28.3 prefers AWS Lambda for the Backend_API **where Lambda is
compatible** with the Decider_2B dependencies and runtime; Requirement 28.4
requires a container service with a documented reason otherwise.

Lambda is **not compatible** with the Decider_2B serving process:

| Factor | Lambda limit | Decider_2B / Backend_API need | Compatible? |
|---|---|---|---|
| Deployment package size | 250 MB unzipped (layers included) | 2B model weights + ML/serving dependencies far exceed this | No |
| Memory | up to ~10 GB, billed per invocation | Serving a 2B model needs large, persistently resident memory | Impractical |
| Execution duration | 15-minute hard cap, no persistent process | `strands-decider serve` is a long-running process that must stay warm | No |
| Cold start | re-initialized on cold start | Reloading 2B weights per cold start is prohibitively slow | No |

**Decision:** the Backend_API (co-located with, or calling, the Decider_2B
server) runs on a **container service — ECS Fargate or AWS App Runner**. The
documented reason is the one above: the 2B model weights, the long-running
serving process, and the ML dependencies exceed Lambda's package-size, memory,
and 15-minute duration limits, and cold-start weight reloading is impractical.
App Runner is the simpler default for the MVP (managed scaling, built-in HTTPS);
ECS Fargate is used when finer networking/scaling control is wanted (and is what
the current `infra/` stack provisions).

---

## Paid-resource inventory

Deploying CraveQuest creates billable AWS resources. **This deployment is not
free of charge.** The table distinguishes MVP-required services from optional
ones.

| AWS resource | MVP-required? | Cost note |
|---|---|---|
| Amazon Bedrock (model invocation) | **Required** | Per-token / per-invocation charge; the main variable cost. Scales with recipe generations and retries. |
| Container service (ECS Fargate / App Runner) | **Required** | Per-hour / per-running-task charge; accrues while the service is up, independent of traffic. |
| Amazon API Gateway | **Required** | Per-request charge (plus any data transfer). |
| AWS Amplify Hosting | **Required** | Build-minute and hosting/data-transfer charge. |
| Amazon CloudWatch (logs + metrics) | **Required** | Log ingestion, storage/retention, and metric charges. |
| Amazon DynamoDB | Optional (non-MVP) | Only if server-side persistence is later added. Not provisioned for the MVP, which uses browser local storage. |

The largest and most variable cost is Amazon Bedrock. The container service is a
steady hourly cost that accrues even with no traffic — tear the stack down when
you are not demoing.

---

## Budget-monitoring checklist

Do this **before** deploying and review it after every demo:

- [ ] **Create an AWS Budget** with an alert threshold (e.g. a monthly cost
      budget with email/SNS alerts at 50% / 80% / 100%).
- [ ] **Enable AWS Cost Explorer** so you can see per-service spend over time.
- [ ] **Set a CloudWatch billing alarm** on the `EstimatedCharges` metric.
- [ ] **Review Amazon Bedrock invocation counts** after each demo (Bedrock is
      the main variable cost; watch retries).
- [ ] **Tear down the stack** (`cdk destroy`) when you are not actively demoing,
      so the container service and hosting stop accruing hourly charges.
- [ ] **Confirm no lingering resources** in Cost Explorer / the service
      dashboards after teardown.
- [ ] **Keep `BEDROCK_MAX_RETRIES`, the per-call timeout, and the per-session
      request limit** at sane values to cap worst-case generation spend.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| App fails to start with a config error naming a variable | A required no-default var (`DECIDER_MODEL`, `BEDROCK_MODEL_ID`, `ALLOWED_ORIGINS`) is missing, or `DECISION_CONFIDENCE_THRESHOLD` is outside `[0.0, 1.0]`. | Set the named variable / fix the value in `.env`; startup reports the offending field and value. |
| Generation fails; frontend shows **model-access-not-configured** | Bedrock model access is not enabled for `BEDROCK_MODEL_ID` in the current region, or credentials lack Bedrock permissions. | Enable model access in the Bedrock console for your region (see [Bedrock model-access instructions](#bedrock-model-access-instructions)); confirm the model id and region match. |
| Browser requests blocked / CORS error in the console | The frontend origin is not in the backend's `ALLOWED_ORIGINS`. | Add the exact frontend origin (scheme + host + port) to `ALLOWED_ORIGINS` and restart the backend. |
| Decisions always come back as `fallback` | The Decider_2B server is unavailable, timing out, or returning an out-of-set answer. | Confirm the server is running and `STRANDS_DECIDER_API_BASE` is correct; check `DECISION_TIMEOUT_MS`. For local UI work without the model, set `DECIDER_MODE=mock`. |
| `decision_confidence` is missing from the response | The result came from the fallback path or from Mock_Mode (both legitimately omit it). | This is expected — confidence is reported only for a genuine Decider_2B decision. |
| Generation repeatedly fails after retries | Bedrock throttling, a service error, per-attempt timeout, or schema-invalid output exhausted `BEDROCK_MAX_RETRIES`. | Check CloudWatch logs for the generation stage; consider raising `BEDROCK_ATTEMPT_TIMEOUT_MS` or `BEDROCK_MAX_RETRIES` (watch cost), or verify the model id. |
| Recipe withheld with a safety error | The post-generation dietary check found an excluded/allergen ingredient on all regeneration attempts. | Expected fail-safe behavior. Review the dietary preferences/allergens in the request; the checker never relaxes a restriction for creativity. |
| 413 on a request | The body exceeds the recipe-endpoint cap (`MAX_BODY_BYTES`, 64 KB) or the global cap (256 KB). | Reduce the payload; the per-endpoint and global caps are configurable. |
| `GET /api/metrics/summary` returns unauthorized | `METRICS_ACCESS_TOKEN` is set and the request lacked the credential, or the endpoint is intentionally not exposed. | Provide the credential, or keep the endpoint excluded from public deploys. |
| `cdk deploy` fails to bootstrap | The account/region is not CDK-bootstrapped. | Run `npx cdk bootstrap` once per account/region, then retry. |

For the backend module layout and tests, see [`backend/README.md`](../backend/README.md).
For the infrastructure stack and IAM detail, see [`infra/README.md`](../infra/README.md).
The scripted demo scenario lives in [`docs/demo-scenario.md`](./demo-scenario.md).

# CraveQuest Infrastructure (AWS CDK, TypeScript)

Infrastructure as code for the CraveQuest MVP. One stack, `CraveQuestStack`,
provisions the full AWS deployment topology from the design document.

## Topology

```
User Browser ─▶ AWS Amplify Hosting (Frontend_App)
User Browser ─▶ Amazon API Gateway ─▶ ECS Fargate service
                   (Backend_API + Decider_2B server) ─▶ Amazon Bedrock
Container ─▶ Amazon CloudWatch (logs + metrics)
```

| Resource | Purpose | Requirement |
|---|---|---|
| AWS Amplify Hosting | Serves the React + Vite frontend bundle; public frontend URL | 28.1 |
| Amazon API Gateway (HTTP API) | Public API edge fronting the Backend_API via a VPC Link | 28.2 |
| ECS Fargate service (+ internal ALB) | Runs the FastAPI Backend_API + Decider_2B server | 28.4 |
| Amazon CloudWatch log group + Container Insights | Logs and metrics | 28.5 |
| IAM task/execution roles | Least-privilege (no `*` actions, no admin) | 26.1 |
| CloudFormation outputs | `FrontendUrl`, `ApiUrl` | 28.9 |

No Amazon DynamoDB or other server-side persistence is provisioned: the MVP
stores favorites and preferences in browser local storage (Req 28.8). DynamoDB
is only introduced if a documented non-MVP feature later needs it.

## Why a container instead of AWS Lambda (Req 28.3 / 28.4)

The Decider_2B server loads a 2-billion-parameter model and runs a long-lived
serving process that must stay warm across requests. This exceeds AWS Lambda's
limits: the 250 MB package size, the per-invocation memory model, the 15-minute
execution cap, and impractically slow cold-start weight reloads. The
Backend_API is therefore co-located with the Decider_2B server on **ECS
Fargate**. AWS App Runner is the documented simpler alternative.

## Least-privilege IAM (Req 26.1)

- **Task role** (runtime identity of the Backend_API): granted only
  `bedrock:InvokeModel` / `bedrock:Converse` (and the streaming variants) scoped
  to the configured model's resource ARN, plus CloudWatch writes to its **own**
  log group. No wildcard actions, no administrator-equivalent policies.
- **Execution role**: ECR image pull scoped to this account's repositories plus
  the one unavoidable `ecr:GetAuthorizationToken` action (which only mints a
  short-lived token, not admin), and log writes to the same dedicated log group.

Review the generated policies with `cdk synth` before deploying.

## Configuration

All deploy-time values come from CDK context or environment variables — never
hardcoded secrets (Req 26.3). Secrets such as a Decider bearer token are
supplied at runtime from AWS Secrets Manager / SSM, not committed here.

| Context key / Env var | Meaning | Default |
|---|---|---|
| `bedrockModelId` / `BEDROCK_MODEL_ID` | Bedrock model id used for generation and IAM scoping | `anthropic.claude-3-5-sonnet-20240620-v1:0` |
| `bedrockModelArns` / `BEDROCK_MODEL_ARNS` | Explicit model ARNs (e.g. inference profiles); comma-separated | derived from `bedrockModelId` |
| `allowedOrigins` / `ALLOWED_ORIGINS` | CORS origins for the Backend_API | `http://localhost:5173` |
| `deciderMode` / `DECIDER_MODE` | `real` or `mock` | `real` |
| `deciderModel` / `DECIDER_MODEL` | Decider checkpoint id | placeholder |
| `strandsDeciderApiBase` / `STRANDS_DECIDER_API_BASE` | Decider server base URL | `http://127.0.0.1:8000` |
| `desiredCount` / `DESIRED_COUNT` | Running Fargate tasks | `1` |
| `cpu` / `TASK_CPU` | Task CPU units (PERF-2: right-sized for FastAPI-only image) | `512` |
| `memoryLimitMiB` / `TASK_MEMORY_MIB` | Task memory (MiB) | `1024` |
| `logRetentionDays` / `LOG_RETENTION_DAYS` | CloudWatch retention | `30` |
| `frontendRepository` / `FRONTEND_REPOSITORY` | Git repo URL for Amplify builds | unset (manual deploy) |
| `frontendBranch` / `FRONTEND_BRANCH` | Branch Amplify builds | `main` |
| `environment` / `ENVIRONMENT` | Deployment environment label; also tag + SEC-3 guard | `dev` |
| `owner` / `OWNER` | Resource owner (cost-allocation/operational tag) | `unknown` |
| `budgetMonthlyLimitUsd` / `BUDGET_MONTHLY_LIMIT_USD` | Monthly AWS Budget limit (USD) | `50` |
| `budgetNotificationEmail` / `BUDGET_NOTIFICATION_EMAIL` | Email for budget notifications (80% actual / 100% forecast) | unset |
| `opsAlertEmail` / `OPS_ALERT_EMAIL` | Email subscribed to the ops-alerts SNS topic | unset |
| `fallbackRateAlarmThreshold` / `FALLBACK_RATE_ALARM_THRESHOLD` | Fallback-rate alarm threshold [0,1] | `0.5` |
| `apiThrottleRateLimit` / `API_THROTTLE_RATE_LIMIT` | SEC-2 API edge throttling steady-state rate (req/s) | `20` |
| `apiThrottleBurstLimit` / `API_THROTTLE_BURST_LIMIT` | SEC-2 API edge throttling burst limit | `40` |
| `enableScheduledScaling` / `ENABLE_SCHEDULED_SCALING` | CO-3 scheduled scale-to-zero off-hours toggle | `true` |
| `scheduledScaleDownCron` / `SCHEDULED_SCALE_DOWN_CRON` | CO-3 scale-DOWN cron body (UTC, AWS 6-field) | `0 20 * * ? *` |
| `scheduledScaleUpCron` / `SCHEDULED_SCALE_UP_CRON` | CO-3 scale-UP cron body (UTC, AWS 6-field) | `0 8 * * ? *` |
| `deciderApiKeySecretArn` / `DECIDER_API_KEY_SECRET_ARN` | SEC-5/SEC-7 reference an existing Decider-token secret by ARN | unset → create empty secret |
| `metricsAccessTokenSecretArn` / `METRICS_ACCESS_TOKEN_SECRET_ARN` | SEC-5/SEC-7 reference an existing metrics-token secret by ARN | unset → create empty secret |

> **SEC-3 guard:** for any `environment` other than `dev`, synth FAILS if
> `allowedOrigins` is still the localhost default (`http://localhost:5173`).
> Pass real origins via `-c allowedOrigins=...` (or `ALLOWED_ORIGINS`). Local
> `dev` synth is never blocked.

## Commands

```bash
npm install          # install CDK + deps
npm run build        # tsc compile
npm run synth        # cdk synth (CloudFormation template)
npm run diff         # cdk diff
npm run deploy       # cdk deploy  (requires AWS credentials + bootstrapped env)
npm run destroy      # cdk destroy (teardown)
```

Example deploy with overrides:

```bash
npx cdk deploy \
  -c bedrockModelId=anthropic.claude-3-5-sonnet-20240620-v1:0 \
  -c allowedOrigins=https://main.d123.amplifyapp.com
```

> Deploying creates paid AWS resources (Bedrock invocations, Fargate running
> tasks, API Gateway requests, Amplify builds/hosting, CloudWatch ingestion).
> This deployment is **not** free. The stack now codifies a monthly AWS Budget
> (CO-1); see below.

## Observability and cost guardrails (Well-Architected Bucket A)

The stack provisions, additively and reversibly:

- **CO-1 — Monthly AWS Budget**: an `AWS::Budgets::Budget` with a configurable
  USD limit (`budgetMonthlyLimitUsd`, default `50`). When
  `budgetNotificationEmail` is set, it notifies at 80% ACTUAL and 100%
  FORECASTED spend. The AWS/Billing `EstimatedCharges` CloudWatch metric exists
  only in `us-east-1`, so the stack deliberately does **not** create a
  cross-region billing alarm (it would break synth elsewhere); the Budgets path
  is the cost guardrail.
- **OE-1 — Alarms + dashboard**: CloudWatch alarms for ALB target 5xx,
  unhealthy hosts, ECS CPU/memory, and Bedrock throttles, plus a
  `CraveQuest-Ops` dashboard. All alarms publish to a shared `OpsAlerts` SNS
  topic (subscribe `opsAlertEmail` to receive them).
- **REL-2 — Fallback-rate metric**: the backend emits the Decider fallback rate
  as CloudWatch EMF (`CraveQuest/Agent` / `FallbackRate`) from stdout, and the
  stack alarms on a sustained high rate (`fallbackRateAlarmThreshold`).
- **OE-3 — Container health check**: the ECS container definition includes a
  Python-based health check against `/health` on port 8080.

### Container image scanning (SEC-4)

The backend image is published to the CDK-managed asset ECR repository via
`DockerImageAsset`, which has no `imageScanOnPush` property. Rather than switch
to an explicit `ecr.Repository` (which would rework the asset push flow and risk
breaking `cdk deploy`), enable scanning at the **account level**: turn on
**Amazon Inspector for ECR** in the AWS console (Inspector → Account management →
enable ECR scanning) or via `aws inspector2 enable --resource-types ECR`. This
scans every repository, including the CDK asset repo, on push. This is the
least-disruptive, synth-clean remediation and is intentionally a documented
account step, not a code change.

## Demo hardening (Well-Architected, demo scope)

A focused, additive, reversible pass of cost-protective remediations for the
short-lived demo deployment. No production always-on expense is added (no second
task, no HTTPS/ACM, no VPC endpoints, no WAF/authorizer); teardown stays clean
with `cdk destroy`.

- **SEC-2 — API edge throttling**: the API Gateway HTTP API `$default` stage
  carries default-route throttling (`apiThrottleRateLimit` req/s,
  `apiThrottleBurstLimit` burst; demo defaults `20` / `40`). This caps a traffic
  spike so it can't fan out into paid Bedrock calls. Throttling only — no
  authorizer, WAF, or CloudFront.
- **SEC-5 / SEC-7 — Secrets Manager for tokens**: the Decider bearer token and
  the metrics access token are injected into the container from Secrets Manager
  (`ecs.Secret`), never as plaintext env. The task role gets read scoped to
  exactly those two secret ARNs. See "Populating the secrets after deploy".
- **PERF-2 — right-sized task**: the Fargate task runs at `512` CPU units /
  `1024` MiB (0.5 vCPU / 1 GB). The backend image currently ships only FastAPI
  (no co-located Decider 2B model), so the previous `2048` / `8192` sizing was
  unnecessary for the demo. If the Decider 2B is later co-located in the image,
  revisit the `cpu` / `memoryLimitMiB` defaults.
- **CO-3 — scheduled scale-to-zero off-hours**: when `enableScheduledScaling` is
  `true` (default), an Application Auto Scaling schedule drops the service to 0
  tasks off-hours and back to 1 during business hours. Crons are **UTC** (AWS
  6-field `cron(min hour day-of-month month day-of-week year)` form); defaults
  scale down at 20:00 UTC and up at 08:00 UTC. This is not target-tracking
  autoscaling and does not raise the steady desiredCount above 1. Set
  `enableScheduledScaling=false` to keep the service running around the clock.

### Populating the secrets after deploy

By default the stack creates two **empty** Secrets Manager secrets:

- `cravequest/decider-api-key` → injected as `STRANDS_DECIDER_API_KEY`
- `cravequest/metrics-access-token` → injected as `METRICS_ACCESS_TOKEN`

(If you referenced existing secrets via `deciderApiKeySecretArn` /
`metricsAccessTokenSecretArn`, use those ARNs/names instead.) After `cdk deploy`,
populate the values — they are never committed or set as plaintext env
(SEC-5/SEC-7):

```bash
aws secretsmanager put-secret-value \
  --secret-id cravequest/decider-api-key \
  --secret-string '<decider-bearer-token>'

aws secretsmanager put-secret-value \
  --secret-id cravequest/metrics-access-token \
  --secret-string '<metrics-access-token>'
```

Both backend settings are optional, so synth and container startup stay valid
while the secrets are empty. ECS resolves a secret at task start, so after
changing a value restart or scale the service (e.g. force a new deployment) for
the task to pick it up — no redeploy of the stack is needed.

### Cost-allocation tags (OE-5 / CO-5)

The stack applies `Project`, `ManagedBy`, `Environment`, and `Owner` tags to
every taggable resource. To break costs down by these in Cost Explorer you must
**activate them as cost-allocation tags** in the AWS Billing console (Billing →
Cost allocation tags → activate `Environment`/`Owner`). This is a manual,
account-level step that cannot be performed in CDK/CloudFormation.

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
| `cpu` / `TASK_CPU` | Task CPU units | `2048` |
| `memoryLimitMiB` / `TASK_MEMORY_MIB` | Task memory (MiB) | `8192` |
| `logRetentionDays` / `LOG_RETENTION_DAYS` | CloudWatch retention | `30` |
| `frontendRepository` / `FRONTEND_REPOSITORY` | Git repo URL for Amplify builds | unset (manual deploy) |
| `frontendBranch` / `FRONTEND_BRANCH` | Branch Amplify builds | `main` |

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
> This deployment is **not** free. Set an AWS Budget and billing alarm first.

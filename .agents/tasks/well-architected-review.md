# CraveQuest — AWS Well-Architected Framework Review

Read-only architecture review of the CraveQuest CDK stack and architecture-relevant backend code. No files were modified. Every finding cites the file and construct it is grounded in. "Already done well" items are called out separately from gaps so you can see what not to touch.

## Scope of evidence actually read

- Infra: `infra/bin/cravequest.ts`, `infra/lib/cravequest-stack.ts`, `infra/lib/config.ts`, `infra/cdk.json`, `infra/package.json`, `infra/README.md`
- Backend (architecture signals only): `backend/Dockerfile`, `backend/app/main.py`, `backend/app/config.py`, `backend/app/api/health.py`, `backend/app/api/metrics.py`, `backend/app/agent/orchestrator.py`, `backend/app/agent/cooking_decision.py`, `backend/app/services/bedrock_generator.py`, `backend/app/services/decider_adapter.py`, `backend/app/services/metrics_collector.py`
- Docs: `docs/DEPLOYMENT.md`

Note on the "Decider_2B server": both READMEs and the stack claim the Decider server runs **co-located** in the same container, but `backend/Dockerfile` bundles only the FastAPI app and explicitly defers adding the Decider install/launch steps ("add its install/launch steps here when the Decider package is available"). The default `STRANDS_DECIDER_API_BASE` is `http://127.0.0.1:8000`. So as-shipped the Decider is either absent (every real decision falls back) or reached in-container on a port the stack never maps. This ambiguity drives several reliability/observability findings below.

---

## 1. Executive summary — gaps by pillar and severity

| Pillar | HIGH | MEDIUM | LOW | Headline gap |
|---|---|---|---|---|
| Operational Excellence | 2 | 3 | 1 | No CloudWatch alarms/dashboards; no app-level structured logging; no explicit container health check |
| Security | 2 | 4 | 1 | Internal ALB listener is plaintext HTTP (no TLS in transit); API Gateway has no authn, throttling, or WAF |
| Reliability | 2 | 3 | 1 | `desiredCount: 1` + no service auto scaling = single task, no multi-task redundancy; Decider server not actually provisioned/mapped |
| Performance Efficiency | 0 | 3 | 1 | No autoscaling; fixed 2 vCPU / 8 GB task sized for a Decider model that isn't in the image; no Bedrock client reuse across requests |
| Cost Optimization | 1 | 3 | 1 | Budgets/billing alarms live only in docs, not in IaC; always-on NAT GW + Fargate + ALB accrue 24/7 |
| Sustainability | 0 | 2 | 1 | Oversized always-on task; no scale-to-zero / scheduled teardown codified |

Totals: **7 HIGH, 18 MEDIUM, 6 LOW.**

### Already done well (do not "fix" these)

- **Scoped Bedrock IAM** — `taskRole` grants `bedrock:InvokeModel/Converse` (+stream variants) against resolved foundation-model ARNs, never `*` (`cravequest-stack.ts`, `BedrockConverse` statement + `resolveBedrockModelArns`). This is genuine least-privilege.
- **Own-log-group-only CloudWatch writes** — both roles use `containerLogGroup.grantWrite(...)` rather than a wildcard logs policy.
- **No plaintext secrets in IaC** — `config.ts` sources everything from context/env; the Decider bearer token is intentionally not modeled as plaintext. Amplify env only receives the public API URL.
- **ECS deployment circuit breaker with rollback** — `FargateService` sets `circuitBreaker: { rollback: true }` and `minHealthyPercent: 100 / maxHealthyPercent: 200`.
- **Container Insights enabled** — `ecs.ContainerInsights.ENABLED` on the cluster.
- **Internal ALB (not internet-facing)** — only API Gateway is the public edge.
- **Robust backend resilience primitives** — Bedrock retry/backoff/timeout loop (`bedrock_generator.py`) and a genuinely wired deterministic fallback for the Decider (`cooking_decision.py` + `orchestrator.py`). Backend error handlers never leak stack traces/prompts.
- **Log retention is bounded** — `logRetentionDays` default 30, mapped via `resolveRetention`.

---

## 2. Operational Excellence

### OE-1 (HIGH) — No CloudWatch alarms or dashboards
**Evidence:** `cravequest-stack.ts` creates a log group and enables Container Insights, but defines **zero** `cloudwatch.Alarm` or `cloudwatch.Dashboard` constructs. Nothing watches ALB 5xx, target health, ECS CPU/memory, or Bedrock throttling.
**Impact:** Failures (unhealthy tasks, Bedrock throttling exhaustion, Decider always falling back) are invisible until a user reports them.
**Remediation (CDK TS):** Add alarms on metrics you already emit: `targetGroup.metrics.unhealthyHostCount()`, `alb.metrics.httpCodeTarget(HttpCodeTarget.TARGET_5XX_COUNT)`, `service.metricCpuUtilization()/metricMemoryUtilization()`, and a Bedrock `Invocations`/`InvocationThrottles` metric via `new cloudwatch.Metric({ namespace: 'AWS/Bedrock', ... })`. Wire them to an `sns.Topic` and build a `cloudwatch.Dashboard`. Low risk, additive.

### OE-2 (HIGH) — No structured/application logging surfaced to CloudWatch
**Evidence:** The backend deliberately avoids logging request bodies (`main.py`), and the unhandled-exception handler does `del request, exc` — it logs nothing at all. There is no logger configured anywhere in the app; only the awslogs stdout driver (`LogDrivers.awsLogs` in the stack) captures uvicorn output.
**Impact:** The "watch CloudWatch logs for the generation stage" advice in `DEPLOYMENT.md` troubleshooting has little to read — no request id, stage, latency, fallback, or safe error-category lines reach logs. `MetricsCollector` state is in-process only and only exposed via the optional `/api/metrics/summary` endpoint.
**Remediation:** Add structured JSON logging (stdlib `logging` + a JSON formatter) emitting safe, non-sensitive fields that already exist: `request_id`, `selected_strategy`, `decision_source`, `below_threshold`, stage latencies, and safe error category (the `last_reason`/`GenerationError`/`DeciderError` category strings — never prompts or payloads). This is a backend change; keep it aligned with the existing no-PII logging discipline.

### OE-3 (MEDIUM) — No container-level health check; ALB `/health` is the only probe
**Evidence:** `backend/Dockerfile` has no `HEALTHCHECK`; the ECS container definition in the stack sets no `healthCheck`. Only the ALB target group checks `path: '/health'`. `/health` (`api/health.py`) is a static liveness check that does not probe Bedrock or Decider reachability (by design).
**Impact:** ECS cannot replace a hung container independent of ALB; a task where the Decider dependency is dead still reports healthy.
**Remediation:** Add an ECS container `healthCheck` (`CMD-SHELL curl -f http://localhost:8080/health`) in the task definition. Optionally add a deeper readiness endpoint, but keep liveness cheap.

### OE-4 (MEDIUM) — Log group `removalPolicy: DESTROY`
**Evidence:** `containerLogGroup` uses `cdk.RemovalPolicy.DESTROY`.
**Impact:** Logs are discarded on stack delete — acceptable for a demo MVP, but it eliminates post-mortem/audit history after teardown.
**Remediation:** For anything beyond demo, switch to `RETAIN` (or export to S3). **Tradeoff — needs sign-off** because it changes teardown behavior and leaves a billable log group after `cdk destroy`.

### OE-5 (MEDIUM) — Thin resource tagging / no cost-allocation tags
**Evidence:** `bin/cravequest.ts` sets only stack-level `Project` and `ManagedBy` tags. No `Environment`, `Owner`, or `CostCenter`; no per-resource tags.
**Impact:** Limited filtering in Cost Explorer and operational inventory.
**Remediation:** Add `cdk.Tags.of(this).add('Environment', config.environment)` etc. Cheap, additive. (Also enables Cost Optimization CO-5.)

### OE-6 (LOW) — No deployment/rollback automation beyond the ECS circuit breaker
**Evidence:** Amplify branch `enableAutoBuild` is tied to whether a repo is configured; there is no pipeline, no blue/green, no API Gateway stage strategy. Rollback relies solely on `circuitBreaker.rollback`.
**Remediation:** Acceptable for MVP. If this graduates, add a CodePipeline/`CfnPipeline` or CDK Pipelines stage. Note only.

---

## 3. Security

### SEC-1 (HIGH) — Plaintext HTTP end-to-end behind API Gateway (no TLS in transit internally)
**Evidence:** `alb.addListener('HttpListener', { port: 80, protocol: ApplicationProtocol.HTTP })` and the target is `ApplicationProtocol.HTTP` on 8080. The VPC Link integration therefore carries traffic in cleartext across the VPC from API Gateway → ALB → task.
**Impact:** Only the public API Gateway edge is TLS-terminated; all in-VPC hops are unencrypted. Falls short of encryption-in-transit best practice, especially since recipe prompts/user context traverse it.
**Remediation:** Terminate TLS on the ALB (HTTPS listener + ACM cert, redirect 80→443) or use end-to-end TLS to the task. For a private ALB this needs a cert/domain strategy. **Tradeoff — needs sign-off** (ACM cert, DNS, possibly a private CA).

### SEC-2 (HIGH) — API Gateway has no authentication, no throttling, and no WAF
**Evidence:** `HttpApi` + `addRoutes({ path: '/{proxy+}', methods: [ANY] })` with only `corsPreflight`. No `defaultAuthorizer`, no `throttle` settings on the default stage, no WAF association (WAFv2 web ACLs aren't attachable to HTTP APIs anyway, but no CloudFront/edge protection exists either).
**Impact:** The public API is open and unthrottled at the edge. The backend has an in-process `SessionRequestLimitMiddleware` (per-session cap) and body-size caps (`main.py`), but that is per-task in-memory state — it does not survive multiple tasks and does not stop volumetric abuse before it reaches Bedrock (direct cost + throttling risk).
**Impact note:** This is the single biggest exposure because every unauthenticated request can drive paid Bedrock invocations.
**Remediation:** Add default-stage throttling on the HTTP API (`CfnStage` `defaultRouteSettings.throttlingRateLimit/BurstLimit`, or `apigatewayv2.CfnStage` since L2 HttpApi lacks a throttle prop) and an authorizer (JWT/`HttpLambdaAuthorizer`, or at minimum an API key gateway pattern). If a public edge is required, front with CloudFront + WAFv2. **Tradeoff — needs sign-off** (auth model is a product decision).

### SEC-3 (MEDIUM) — CORS allow-list falls back to localhost and is only as good as the deploy param
**Evidence:** `config.ts` defaults `allowedOrigins` to `http://localhost:5173`; the stack splits it into both API Gateway `corsPreflight.allowOrigins` and the container `ALLOWED_ORIGINS`. Backend CORS uses `allow_methods=["*"], allow_headers=["*"]` (`main.py`).
**Impact:** If deployed without overriding `allowedOrigins`, the API advertises a localhost origin (broken, not dangerous). The `*` methods/headers are broad. `allow_credentials=False` mitigates the worst CORS risks.
**Remediation:** Fail synth/deploy when `allowedOrigins` still equals the localhost default in a non-dev deploy; tighten `allow_methods`/`allow_headers` to the actual set (GET/POST/OPTIONS, content-type/authorization — the gateway already scopes these). Low risk.

### SEC-4 (MEDIUM) — No container image scanning
**Evidence:** Image is built via `ecr_assets.DockerImageAsset`. No `imageScanOnPush` is configurable on the asset, and no explicit ECR repo with scan-on-push is defined; no Inspector integration.
**Impact:** `python:3.11-slim` + pip deps ship unscanned.
**Remediation:** Enable Amazon Inspector for ECR at the account level, or push to an explicit `ecr.Repository({ imageScanOnPush: true })`. Additive; low risk.

### SEC-5 (MEDIUM) — Secrets wiring is documented but not implemented
**Evidence:** The stack comments say to supply the Decider token "via `secrets:`" but `addContainer` sets no `secrets` map; `STRANDS_DECIDER_API_KEY` is read from env in `config.py` and never plumbed from the stack. No `secretsmanager.Secret` or SSM parameter is created or referenced.
**Impact:** The "injected from Secrets Manager/SSM" claim in `DEPLOYMENT.md`/`infra/README.md` is aspirational — there is no mechanism in the IaC. If an operator sets it, the only path is a plaintext env var.
**Remediation:** Create/reference a `secretsmanager.Secret` and pass it via `secrets: { STRANDS_DECIDER_API_KEY: ecs.Secret.fromSecretsManager(secret) }`, and grant the task role `secret.grantRead(taskRole)`. Needed only when the Decider requires a token. Low risk when added correctly.

### SEC-6 (MEDIUM) — No explicit security groups / least-privilege network rules authored
**Evidence:** The stack relies on CDK defaults for the ALB SG, service SG, and VPC Link SG; no SGs are explicitly created or scoped. The VPC uses `natGateways: 1` and PRIVATE_WITH_EGRESS subnets (good) but egress is unrestricted.
**Impact:** Default SGs are reasonable (ALB→task on 8080 is auto-wired by `loadBalancerTarget`), but egress is open to the internet via NAT; there's no VPC endpoint for Bedrock/CloudWatch/ECR, so all AWS calls leave via NAT.
**Remediation:** Add interface VPC endpoints for `bedrock-runtime`, `logs`, `ecr.api`/`ecr.dkr`, and a gateway endpoint for S3; then you can tighten egress. This also reduces NAT cost (see CO-2) and data-exfil surface. **Tradeoff — moderate**: endpoints have hourly cost; net usually positive vs NAT.

### SEC-7 (LOW) — Metrics endpoint auth is sound but token is env-only
**Evidence:** `metrics.py` uses `secrets.compare_digest`, returns 404 when unset, 401 when wrong — good. But `METRICS_ACCESS_TOKEN` is an env var (`config.py`), not a Secrets Manager reference.
**Remediation:** When exposed, source it from Secrets Manager like SEC-5. Minor.

---

## 4. Reliability

### REL-1 (HIGH) — Single task, no service auto scaling
**Evidence:** `desiredCount` defaults to `1` (`config.ts`); `FargateService` has no `autoScaleTaskCount(...)`. The VPC spans `maxAzs: 2`, but one task runs in one AZ at a time.
**Impact:** No redundancy. A task/AZ failure takes the API down until ECS reschedules; no capacity headroom for load.
**Remediation:** `const scaling = service.autoScaleTaskCount({ minCapacity: 2, maxCapacity: N });` then `scaling.scaleOnCpuUtilization(...)` and/or `scaleOnRequestCount(...)` against the ALB target group. Set `desiredCount >= 2` for multi-AZ spread. **Tradeoff — cost**: 2+ always-on tasks roughly multiply the steady Fargate bill; needs sign-off given the MVP's explicit cost sensitivity.

### REL-2 (HIGH) — The Decider_2B server is not actually provisioned or port-mapped
**Evidence:** `backend/Dockerfile` bundles only the FastAPI app and defers the Decider install/launch; the container only maps port 8080; `STRANDS_DECIDER_API_BASE` defaults to `http://127.0.0.1:8000`. The stack maps/targets only 8080.
**Impact:** In `DECIDER_MODE=real` with no Decider process, **every** decision hits the deterministic fallback (`cooking_decision.py`), silently degrading the product's headline feature. There is no alarm on `fallback_rate` (ties to OE-1).
**Remediation:** Either (a) genuinely add the Decider process to the image/task and document the port, or (b) make the architecture honest about an external Decider endpoint and provision/secure it. At minimum, alarm on a high fallback rate using the metric already computed in `MetricsCollector.fallback_rate()` (export it as an EMF/custom metric). **Design-level — needs sign-off.**

### REL-3 (MEDIUM) — Decider retry/circuit-breaking: timeout only, no retries or breaker
**Evidence:** `RealDeciderAdapter.decide` applies a single call with `timeout_ms` and translates failures to `DeciderError`; there is no retry or circuit breaker (by design, per orchestrator "no retry/fallback route"). Good: it does enforce a wall-clock timeout cross-check and falls back deterministically.
**Impact:** Transient Decider blips become immediate fallbacks. Acceptable since fallback is deterministic and safe, but repeated fallback is undetected (see REL-2/OE-1).
**Remediation:** Keep the deterministic fallback (it's correct), but add observability on fallback frequency rather than hidden silent degradation. Low code risk.

### REL-4 (MEDIUM) — In-memory session limit and metrics do not survive scale-out or restart
**Evidence:** `SessionRequestLimitMiddleware` stores counts in `application.state.session_counts` (per-process dict); `MetricsCollector` is a process-wide singleton.
**Impact:** With >1 task (needed for REL-1) the per-session cap and metrics fragment across tasks; a restart resets both. Not a correctness bug for an MVP but limits reliability of the control and the telemetry.
**Remediation:** For multi-task, move the rate limit to the edge (API Gateway throttling, SEC-2) and emit metrics as CloudWatch EMF rather than relying on in-process aggregation.

### REL-5 (MEDIUM) — No DLQ / async durability for failed generations
**Evidence:** The flow is fully synchronous; a failed Bedrock generation after retries raises `GenerationError` to the caller with no capture. No SQS/DLQ exists (none is needed for the synchronous design, but there's no record of failures beyond logs that OE-2 shows are absent).
**Remediation:** Low priority for synchronous MVP; rely on structured logging (OE-2) + alarms. Note only.

### REL-6 (LOW) — No backups required (stateless by design)
**Evidence:** `infra/README.md` and the stack provision no DynamoDB/RDS; favorites live in browser local storage.
**Assessment:** Correct for the MVP — nothing to back up server-side. Documented, not a gap. Listed for completeness.

---

## 5. Performance Efficiency

### PERF-1 (MEDIUM) — No autoscaling triggers
**Evidence:** Same as REL-1 — no `autoScaleTaskCount`. Performance under load depends entirely on the single fixed task.
**Remediation:** Target-tracking scaling on CPU and ALB request count (see REL-1). Pairs cost with demand.

### PERF-2 (MEDIUM) — Fixed 2 vCPU / 8192 MiB sized for a model that isn't in the image
**Evidence:** `config.ts` defaults `cpu: 2048`, `memoryLimitMiB: 8192` with the comment "2B serving needs generous memory/CPU." But the Dockerfile ships only FastAPI (no 2B weights). So the task is provisioned (and billed) for a workload it doesn't currently run.
**Impact:** Over-provisioned steady cost and idle capacity if the Decider isn't actually co-located; under-provisioned if it later is and needs GPU (Fargate has no GPU).
**Remediation:** Right-size to the real footprint. If the Decider truly runs here and needs acceleration, Fargate is the wrong compute (no GPU) — reconsider ECS-on-EC2/g-instances or a separate inference service. **Design-level — needs sign-off.**

### PERF-3 (MEDIUM) — Bedrock client not reused across requests
**Evidence:** `BedrockGenerator._get_client()` lazily builds a boto3 client, but the generator/adapter are constructed per request in the orchestrator path (`_resolve_adapter` / `BedrockGenerator.from_settings` are invoked per `run_request`). There's no module-level cached client, so connection/credential setup can repeat.
**Impact:** Extra latency and TLS/handshake churn to Bedrock per request.
**Remediation:** Cache the boto3 `bedrock-runtime` client at module scope (or on `app.state`) and reuse across requests; boto3 clients are thread-safe for this use. Low risk, pure backend.

### PERF-4 (LOW) — No response/result caching
**Evidence:** No caching layer anywhere; each request fully re-invokes decision + Bedrock. Reasonable for personalized recipes, so caching is limited value.
**Remediation:** Optional — cache only idempotent, non-personalized lookups if any emerge. Note only.

---

## 6. Cost Optimization

### CO-1 (HIGH) — Budgets and billing alarms exist only in docs, not in IaC
**Evidence:** `DEPLOYMENT.md` "Budget-monitoring checklist" tells the operator to manually create an AWS Budget, enable Cost Explorer, and set a billing alarm. The CDK stack defines **no** `aws_budgets.CfnBudget` and **no** `EstimatedCharges` CloudWatch alarm. `infra/README.md` warns "Set an AWS Budget and billing alarm first" — but doesn't codify it.
**Impact:** The primary cost guardrail for a pay-per-use Bedrock + always-on Fargate stack depends on a human remembering a checklist. The biggest variable cost (Bedrock) is unthrottled at the edge (SEC-2).
**Remediation:** Add `new budgets.CfnBudget(this, 'MonthlyBudget', { budget: { budgetType: 'COST', timeUnit: 'MONTHLY', budgetLimit: {...} }, notificationsWithSubscribers: [...] })` and an SNS-backed alarm on `AWS/Billing EstimatedCharges`. Additive, low risk, high value.

### CO-2 (MEDIUM) — Always-on NAT Gateway + ALB + Fargate accrue 24/7
**Evidence:** `natGateways: 1`, an always-running internal ALB, and `desiredCount: 1` Fargate task. `DEPLOYMENT.md` itself notes the container service "accrues even with no traffic."
**Impact:** Three steady hourly charges regardless of traffic — the classic MVP cost trap.
**Remediation:** Replace NAT with VPC endpoints (SEC-6) to drop the NAT hourly + data cost; consider App Runner (documented alternative) which removes the standalone ALB and scales to fewer idle resources; codify scheduled scale-down. **Tradeoff — architecture change; needs sign-off.**

### CO-3 (MEDIUM) — No scheduled teardown / scale-to-zero
**Evidence:** Teardown is a manual `cdk destroy` in `DEPLOYMENT.md`. No `applicationautoscaling` scheduled action to drop `desiredCount` to 0 off-hours.
**Remediation:** Add a scheduled scaling action (e.g., scale to 0 overnight) or document an automated stop. Low risk if additive.

### CO-4 (MEDIUM) — Bedrock cost controls are backend env knobs, not enforced at the edge
**Evidence:** `BEDROCK_MAX_RETRIES`, `BEDROCK_ATTEMPT_TIMEOUT_MS`, and `MAX_SESSION_REQUESTS` cap worst-case spend (`config.py`), which is good, but they're per-process and bypassable by volume (SEC-2).
**Remediation:** Pair with edge throttling (SEC-2) so a traffic spike can't fan out into Bedrock cost. Also surface a Bedrock invocation-count alarm (OE-1).

### CO-5 (LOW) — Missing cost-allocation tags
**Evidence:** Only `Project`/`ManagedBy` tags (see OE-5). No `Environment`/`Owner` for cost breakdown.
**Remediation:** Add tags (shared with OE-5) and activate them as cost-allocation tags in Billing.

---

## 7. Sustainability

### SUS-1 (MEDIUM) — Oversized, always-on compute
**Evidence:** Same as PERF-2/CO-2: 2 vCPU / 8 GB task running 24/7 for a workload the image doesn't currently include, plus always-on NAT + ALB.
**Impact:** Idle resource consumption = wasted energy, not just cost.
**Remediation:** Right-size and scale with demand (PERF-1/REL-1), scale-to-zero off-hours (CO-3), prefer managed serverless (App Runner) where feasible. Shares fixes with cost pillar.

### SUS-2 (MEDIUM) — No scale-to-zero / demand-proportional footprint
**Evidence:** Fixed `desiredCount`, no autoscaling, no schedule.
**Remediation:** Autoscaling + scheduled scale-down so capacity tracks real usage (shared with PERF-1, CO-3).

### SUS-3 (LOW) — Region choice not pinned for carbon/latency
**Evidence:** `bin/cravequest.ts` takes `account/region` from `CDK_DEFAULT_*` with no guidance; Bedrock model access is region-specific (`DEPLOYMENT.md`).
**Remediation:** Document a preferred low-carbon region that also offers the chosen Bedrock model, and pin it. Note only.

---

## 8. Prioritized remediation checklist

Ordered by severity, then by effort/risk. "Safe/low-risk" = additive, reversible, no behavior change for users. "Needs sign-off" = changes behavior, cost, or security posture in ways a human must approve.

### A. Safe / low-risk, do first (additive, high value)
1. **CO-1** — Codify an AWS Budget + `EstimatedCharges` billing alarm in CDK. (HIGH value, trivial, closes the top cost gap.)
2. **OE-1** — Add CloudWatch alarms (ALB 5xx, unhealthy hosts, ECS CPU/mem, Bedrock throttles) + an SNS topic + a dashboard.
3. **OE-2** — Add safe structured JSON logging in the backend (request_id, strategy, source, latencies, safe error category).
4. **REL-2 / OE-1 companion** — Emit `fallback_rate` as a custom/EMF metric and alarm on sustained high fallback (detects the silent Decider-degradation).
5. **OE-3** — Add an ECS container `healthCheck`.
6. **SEC-4** — Enable ECR image scan-on-push / Inspector.
7. **PERF-3** — Cache and reuse the Bedrock boto3 client across requests.
8. **OE-5 / CO-5** — Add `Environment`/`Owner` tags; activate cost-allocation tags.
9. **SEC-3** — Fail deploy if `allowedOrigins` is still the localhost default; tighten CORS methods/headers.

### B. Needs sign-off — security / correctness tradeoffs
10. **SEC-2** (HIGH) — API Gateway throttling + an authorizer (and CloudFront+WAF if a hardened public edge is wanted). Auth model is a product decision.
11. **SEC-1** (HIGH) — TLS in transit to the ALB/task (ACM cert + HTTPS listener). Needs cert/DNS strategy.
12. **SEC-5 / SEC-7** — Move Decider token (and metrics token) to Secrets Manager and wire via `ecs.Secret`.
13. **SEC-6 / CO-2** — Add VPC interface endpoints (Bedrock/Logs/ECR/S3), then tighten egress and drop NAT. Endpoint hourly cost vs NAT savings.

### C. Needs sign-off — reliability / cost / design tradeoffs
14. **REL-1 / PERF-1 / SUS-1-2** (HIGH/MED) — Add service autoscaling and `desiredCount >= 2` for multi-AZ. Multiplies steady Fargate cost — weigh against the MVP's cost sensitivity.
15. **REL-2 / PERF-2** (HIGH/MED) — Resolve the Decider_2B reality gap: actually provision+map the Decider (and pick GPU-capable compute if it needs acceleration — Fargate has none), or make the external-endpoint architecture explicit and secured.
16. **CO-2 / CO-3 / SUS-2** — App Runner migration and/or scheduled scale-to-zero off-hours.
17. **OE-4** — Switch log group `removalPolicy` to `RETAIN` (or S3 export) for audit retention — leaves a billable group after teardown.

### D. Notes / optional (LOW, no action required for MVP)
- **OE-6** deployment pipeline/blue-green, **REL-5** DLQ, **REL-6** backups (correctly none), **PERF-4** caching, **SUS-3** region pinning.

---

*Review method: static read of the CDK stack, config, and architecture-relevant backend modules listed in Section "Scope". No `cdk synth`/`cdk diff` was run (read-only, and synth requires building the Docker asset). Findings about synthesized IAM/network defaults are inferred from the CDK L2 construct behavior, not from a rendered template; where that inference matters it is stated (e.g., SG defaults in SEC-6). Nothing was modified, built, or deployed.*

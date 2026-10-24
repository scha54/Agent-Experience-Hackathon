# Bucket A Review — AWS Well-Architected remediations

Reviewed commit: `a9d5db4` ("feat: implement Well-Architected Bucket A remediations").
Scope: the 9 Bucket A items in `.agents/tasks/bucket-a-plan.md` / section 8.A of
`.agents/tasks/well-architected-review.md`. 13 files changed, all in scope.

Verdict: **APPROVED** — no blocking findings.

## Evidence summary

Read: `infra/bin/cravequest.ts`, `infra/lib/config.ts`, `infra/lib/cravequest-stack.ts`,
`infra/README.md`, `backend/Dockerfile`, `backend/app/main.py`,
`backend/app/logging_config.py`, `backend/app/api/recipes.py`,
`backend/app/services/bedrock_generator.py`, `backend/app/services/metrics_collector.py`
(`fallback_rate`), `backend/app/agent/orchestrator.py` (`run_request` signature), and the
four added/extended test files. Did not re-run suites; read the coder's recorded results.

## (a) Per-item confirmation

1. **CO-1** — PASS. `budgets.CfnBudget` MonthlyBudget: `budgetType:'COST'`, `timeUnit:'MONTHLY'`,
   `budgetLimit.amount: config.budgetMonthlyLimitUsd` (configurable, default 50). Subscribers built
   only when `budgetNotificationEmail` set — two notifications (ACTUAL >=80%, FORECASTED >=100%),
   else `notificationsWithSubscribers: undefined` so synth never emits empty subscribers. SNS
   `OpsAlerts` topic created; email subscribed only if `opsAlertEmail` set. EstimatedCharges alarm
   gated inside `if (this.region === 'us-east-1')` with an explanatory comment and intentionally not
   created. (`infra/lib/cravequest-stack.ts`, `infra/lib/config.ts`.)
2. **OE-1** — PASS. One `sns.Topic 'OpsAlerts'`. Five alarms each `.addAlarmAction(new SnsAction(opsAlerts))`:
   ALB target 5xx, unhealthy hosts, ECS CPU, ECS memory, Bedrock InvocationThrottles. `cloudwatch.Dashboard`
   'CraveQuest-Ops' with widgets for all of the above plus the fallback-rate metric.
3. **OE-2** — PASS. `logging_config.py` configures a single stdout JSON handler once (idempotent
   `_HANDLER_FLAG` guard), serializing only an allow-list (`request_id`, `selected_strategy`,
   `decision_source`, `below_threshold`, `decision_latency_ms`, `generation_latency_ms`, `error_category`).
   `unhandled_exception_handler` logs `error_category` (class name) via `logger.exception` while the
   500 response body is unchanged and non-leaking. `request_id` wired per request in `recipes.py`
   (`X-Request-ID` inbound or generated uuid4, length-capped).
4. **REL-2** — PASS. Fallback rate exported as EMF to stdout (`emit_fallback_rate_emf`, namespace
   `CraveQuest/Agent`, metric `FallbackRate`) — no PutMetricData, no IAM widening. `FallbackRateHighAlarm`
   on `CraveQuest/Agent FallbackRate`, threshold `config.fallbackRateAlarmThreshold`, wired to `OpsAlerts`,
   added to the dashboard. EMF emission is wrapped so telemetry never breaks a request.
5. **OE-3** — PASS. Container `healthCheck` with a Python one-liner hitting `http://localhost:8080/health`
   (no curl/apt layer), interval 30s / timeout 5s / retries 3 / startPeriod 30s. Dockerfile `HEALTHCHECK`
   added for parity.
6. **SEC-4** — PASS. DockerImageAsset flow kept; documented account-level Amazon Inspector for ECR with a
   clear rationale (DockerImageAsset has no `imageScanOnPush`; explicit `ecr.Repository` would rework the
   asset push flow). Code comment + README "Container image scanning" subsection present. No explicit
   `ecr.Repository` created. (Documented recommendation accepted; rationale explained.)
7. **PERF-3** — PASS. Module-level `_CLIENT_CACHE` keyed by `(region_name, attempt_timeout_ms)` with
   double-checked locking; the `botocore.Config` (connect/read timeout, `retries max_attempts=0`)
   moved verbatim into `_build_or_get_client`. Injected clients bypass the cache. Retry/backoff/timeout
   logic and the per-attempt flow are unchanged.
8. **OE-5/CO-5** — PASS. `cdk.Tags.of(stack).add('Environment', ...)` and `.add('Owner', ...)` in
   `infra/bin/cravequest.ts` alongside the existing `Project`/`ManagedBy` StackProps tags. Billing-console
   activation noted as manual in both the bin comment and README; not attempted in code.
9. **SEC-3** — PASS. `loadStackConfig` throws when `environment !== 'dev'` AND `allowedOrigins` equals the
   localhost default; dev/local synth not blocked. Backend CORS tightened to `['GET','POST','OPTIONS']` and
   headers `['content-type','authorization']` with `allow_credentials=False` in both `create_app` and the
   import-safe fallback. (API Gateway `corsPreflight` also tightened to the same set — consistent with SEC-3,
   not scope creep.)

## (b) No Bucket B/C scope creep

Confirmed absent: API Gateway authorizer; ACM/TLS/HTTPS listener (listener stays HTTP:80); Secrets Manager
wiring (`secrets:` still unset); VPC endpoints / NAT change (`natGateways:1` unchanged); autoscaling /
`desiredCount` change; Decider provisioning; log-group `RemovalPolicy` change (still `DESTROY`); edge/backend
throttling. `orchestrator.py` and `cooking_decision.py` are not in the diff.

## (c) No confirmed strength weakened

Scoped Bedrock IAM, own-log-group-only CloudWatch writes, no plaintext secrets, deployment circuit breaker
with rollback, Container Insights, internal ALB, and bounded log retention are all unchanged. Backend Bedrock
retry/backoff and the wired deterministic fallback are untouched (orchestrator/cooking_decision not modified;
PERF-3 preserves `botocore.Config` and the retry loop).

## (d) Recorded verification

Commit message records: infra `npm run build` (tsc) OK, `npx tsc --noEmit` OK, `npx cdk synth` OK (Budget, SNS
topic, 6 alarms, dashboard, container HealthCheck, Environment/Owner tags present; no `ECR::Repository`;
email-set subscription and SEC-3 prod guard both verified); backend `py_compile` on changed modules OK,
`pytest 40 passed`. Four test files added/extended and present (`.pyc` caches confirm execution). Evidence is
present and specific; no suite re-run needed and no articulable doubt justifying a spot-check.

## (e) No secrets / PII in logs

The JSON formatter serializes only the safe allow-list; arbitrary record attributes, prompts, bodies, and
ingredient/user payloads cannot reach structured output. Error paths log only the exception class name, never
the message. Tests assert ingredient/prompt text and secret message strings never appear in captured output.
EMF carries only an aggregate fraction. No request bodies/prompts/payloads are logged.

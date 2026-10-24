# Demo Hardening Review — Well-Architected (demo scope)

Commit reviewed: `66b2140` — *feat: demo-hardening Well-Architected pass (SEC-2, SEC-5/7, CO-3, PERF-2)*
Files changed: `infra/README.md`, `infra/lib/config.ts`, `infra/lib/cravequest-stack.ts` (backend unchanged, as required).

Verdict: **APPROVED**

## Scope check — four required items

### 1. SEC-2 throttling only — PASS
- `config.ts` adds `apiThrottleRateLimit` (ctx `apiThrottleRateLimit` / env `API_THROTTLE_RATE_LIMIT`, default `20`) and `apiThrottleBurstLimit` (env `API_THROTTLE_BURST_LIMIT`, default `40`), both `parseInt(...,10)`.
- `cravequest-stack.ts` reaches `httpApi.defaultStage!.node.defaultChild as apigwv2.CfnStage` and sets `defaultRouteSettings = { throttlingRateLimit: config.apiThrottleRateLimit, throttlingBurstLimit: config.apiThrottleBurstLimit }` on the `$default` stage.
- No authorizer, CloudFront, or WAF added — confirmed by grep (only disclaiming comments reference them).

### 2. SEC-5 / SEC-7 Secrets Manager — PASS
- Two `secretsmanager.Secret` constructs (`cravequest/decider-api-key`, `cravequest/metrics-access-token`) created EMPTY (no `generateSecretString`, no `secretString`, no hardcoded value). Optional ARN override via `deciderApiKeySecretArn` / `metricsAccessTokenSecretArn` uses `fromSecretCompleteArn`.
- Wired into the container via `ecs.Secret.fromSecretsManager` for env var names `STRANDS_DECIDER_API_KEY` and `METRICS_ACCESS_TOKEN`. These match `backend/app/config.py` exactly (`strands_decider_api_key` alias `STRANDS_DECIDER_API_KEY`, `metrics_access_token` alias `METRICS_ACCESS_TOKEN`), both optional (default None) so empty secrets are valid.
- Task role granted scoped read via `deciderSecret.grantRead(taskRole)` / `metricsSecret.grantRead(taskRole)` — no wildcard. (Synth evidence: `GetSecretValue` scoped to the two secret refs only.)
- No plaintext token ever existed in the `environment` map; none added (`environment` map still sets only DECIDER_*/Bedrock/AWS_REGION/ALLOWED_ORIGINS).
- `README.md` documents post-deploy population via `aws secretsmanager put-secret-value` and the restart-to-pick-up note.

### 3. CO-3 scheduled scale-to-zero — PASS
- Gated by `enableScheduledScaling` (env `ENABLE_SCHEDULED_SCALING`, default `true`; only explicit `false`/`0`/`no` disables).
- `service.autoScaleTaskCount({ minCapacity: 0, maxCapacity: 1 })` plus two `scaleOnSchedule` actions: DOWN min0/max0 at `scheduledScaleDownCron` (default `0 20 * * ? *`), UP min1/max1 at `scheduledScaleUpCron` (default `0 8 * * ? *`), both via `Schedule.expression('cron(...)')`.
- No CPU/request target-tracking autoscaling (no `scaleOnCpuUtilization`/`scaleOnRequestCount`/`scaleOnMetric`). `desiredCount: config.desiredCount` unchanged (default 1); not raised to >= 2.

### 4. PERF-2 right-size — PASS
- `config.ts` defaults changed to `cpu` `512` and `memoryLimitMiB` `1024` (valid 0.5 vCPU / 1 GB Fargate combo), still via `fromContextOrEnv` (configurable, not hardcoded in the stack; stack reads `config.cpu` / `config.memoryLimitMiB`).
- JSDoc + inline comment note FastAPI-only sizing and revisit-if-2B-co-located.

## Out-of-scope / strength-preservation check — PASS
- Listener still `HTTP:80` (`alb.addListener('HttpListener', { port: 80, protocol: HTTP })`); no ACM/HTTPS/certificate.
- No authorizer/WAF/CloudFront; no VPC endpoints added; `natGateways: 1` unchanged.
- No target-tracking autoscaling; no `desiredCount >= 2`; `maxAzs: 2` unchanged (not newly introduced multi-AZ expense); no App Runner change.
- Log group `removalPolicy: cdk.RemovalPolicy.DESTROY` intact.
- Preserved strengths: Bedrock IAM scoped to model ARNs via `resolveBedrockModelArns` (never `*`); the only `Resource: "*"` is the pre-existing `ecr:GetAuthorizationToken` (unchanged, out of scope); own-log-group writes (`containerLogGroup.grantWrite(taskRole)`); `circuitBreaker: { rollback: true }`; `containerInsightsV2: ENABLED`; internal ALB (`internetFacing: false`); bounded log retention; backend retry/backoff + fallback untouched.
- No secret value hardcoded anywhere (grep for `secretString`/`generateSecretString`/`SecretValue`/`apiKey=`/`password` returns nothing in the changed infra sources).

## Verification evidence — present and sound
Recorded in the commit message / coder report (not re-run per instructions):
- `infra`: `npm run build` (tsc) exit 0; `npx cdk synth` exit 0 (Docker asset built).
- Synth values confirmed in `cdk.out/CraveQuestStack.template.json`: Stage `DefaultRouteSettings` ThrottlingRateLimit=20 / ThrottlingBurstLimit=40; task def Cpu=512 / Memory=1024; two `AWS::SecretsManager::Secret`; container `Secrets` array wiring `STRANDS_DECIDER_API_KEY` / `METRICS_ACCESS_TOKEN` via `ValueFrom`; `BackendTaskRole` `GetSecretValue` scoped to the two secret refs (no wildcard); `ScalableTarget` min0/max1 on `ecs:service:DesiredCount` with two `ScheduledAction`s (`cron(0 20 * * ? *)` min0/max0, `cron(0 8 * * ? *)` min1/max1).
- Negative/override: `-c enableScheduledScaling=false` emits 0 App Auto Scaling resources; override ARNs create 0 new Secret resources.
- Backend: no changes; `pytest -q` => 40 passed.

No articulable doubt required a spot-check beyond reading the diff and the unchanged guards, which were confirmed directly in source.

## Findings
None (no blockers).

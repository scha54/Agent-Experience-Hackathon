/**
 * Deploy-time configuration for the CraveQuest stack.
 *
 * Every value is sourced from CDK context (`-c key=value` or `cdk.json`
 * context) or environment variables. No secrets, credentials, or API keys are
 * stored here or committed to the repository (Req 26.3). Secrets such as a
 * Decider bearer token are supplied at runtime via a secret manager / SSM and
 * are intentionally NOT modeled as plaintext here.
 */
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

/** Resolved, validated configuration passed into the stack. */
export interface StackConfig {
  /**
   * Amazon Bedrock model id used for recipe generation (maps to the backend
   * `BEDROCK_MODEL_ID` env var). Also used to scope the task role's Bedrock
   * permissions to this specific model (Req 26.1).
   */
  readonly bedrockModelId: string;

  /**
   * Optional explicit Bedrock foundation-model ARNs the task role may invoke.
   * When omitted, the stack derives a least-privilege resource ARN for
   * `bedrockModelId` in the deploy region/account. Supplying this lets you
   * grant cross-region inference-profile ARNs without widening to `*`.
   */
  readonly bedrockModelArns?: string[];

  /** Comma-separated CORS origins for the backend (`ALLOWED_ORIGINS`). */
  readonly allowedOrigins: string;

  /** Decider mode: `real` or `mock` (`DECIDER_MODE`). */
  readonly deciderMode: string;

  /** Decider checkpoint/model id (`DECIDER_MODEL`). */
  readonly deciderModel: string;

  /** Decider server base URL (`STRANDS_DECIDER_API_BASE`). */
  readonly strandsDeciderApiBase: string;

  /** Container image tag applied to the backend image asset. */
  readonly imageTag: string;

  /** Desired number of running Fargate tasks. */
  readonly desiredCount: number;

  /** Task CPU units (1024 = 1 vCPU). 2B serving needs generous memory/CPU. */
  readonly cpu: number;

  /** Task memory (MiB). Must be a valid Fargate CPU/memory combination. */
  readonly memoryLimitMiB: number;

  /** CloudWatch log retention in days. */
  readonly logRetentionDays: number;

  /**
   * Optional GitHub repository URL for the frontend Amplify app
   * (e.g. https://github.com/owner/repo). When omitted the Amplify app is
   * created without a connected branch so the bundle can be deployed manually.
   */
  readonly frontendRepository?: string;

  /** Git branch Amplify builds from when a repository is connected. */
  readonly frontendBranch: string;
}

/**
 * Read a value from CDK context first, then the environment, then a default.
 * Returns `undefined` when none is set and no default is given.
 */
function fromContextOrEnv(
  app: Construct,
  contextKey: string,
  envKey: string,
  fallback?: string,
): string | undefined {
  const ctx = app.node.tryGetContext(contextKey);
  if (ctx !== undefined && ctx !== null && String(ctx).length > 0) {
    return String(ctx);
  }
  const env = process.env[envKey];
  if (env !== undefined && env.length > 0) {
    return env;
  }
  return fallback;
}

/**
 * Build the {@link StackConfig} from CDK context and environment variables.
 *
 * Required values without a safe default (`BEDROCK_MODEL_ID`, `ALLOWED_ORIGINS`,
 * `DECIDER_MODEL`) mirror the backend's startup-required vars; when missing they
 * fall back to clearly-labeled placeholders so `cdk synth` still succeeds in a
 * CI/dev context. Operators override them at deploy time via context or env.
 */
export function loadStackConfig(app: cdk.App): StackConfig {
  const bedrockModelId =
    fromContextOrEnv(app, 'bedrockModelId', 'BEDROCK_MODEL_ID') ??
    'anthropic.claude-3-5-sonnet-20240620-v1:0';

  const bedrockModelArnsRaw = fromContextOrEnv(
    app,
    'bedrockModelArns',
    'BEDROCK_MODEL_ARNS',
  );
  const bedrockModelArns = bedrockModelArnsRaw
    ? bedrockModelArnsRaw.split(',').map((a) => a.trim()).filter((a) => a.length > 0)
    : undefined;

  return {
    bedrockModelId,
    bedrockModelArns,
    allowedOrigins:
      fromContextOrEnv(app, 'allowedOrigins', 'ALLOWED_ORIGINS') ??
      'http://localhost:5173',
    deciderMode: fromContextOrEnv(app, 'deciderMode', 'DECIDER_MODE', 'real')!,
    deciderModel:
      fromContextOrEnv(app, 'deciderModel', 'DECIDER_MODEL') ??
      'strands_decider/your-checkpoint-name',
    strandsDeciderApiBase: fromContextOrEnv(
      app,
      'strandsDeciderApiBase',
      'STRANDS_DECIDER_API_BASE',
      'http://127.0.0.1:8000',
    )!,
    imageTag: fromContextOrEnv(app, 'imageTag', 'IMAGE_TAG', 'latest')!,
    desiredCount: parseInt(
      fromContextOrEnv(app, 'desiredCount', 'DESIRED_COUNT', '1')!,
      10,
    ),
    cpu: parseInt(fromContextOrEnv(app, 'cpu', 'TASK_CPU', '2048')!, 10),
    memoryLimitMiB: parseInt(
      fromContextOrEnv(app, 'memoryLimitMiB', 'TASK_MEMORY_MIB', '8192')!,
      10,
    ),
    logRetentionDays: parseInt(
      fromContextOrEnv(app, 'logRetentionDays', 'LOG_RETENTION_DAYS', '30')!,
      10,
    ),
    frontendRepository: fromContextOrEnv(
      app,
      'frontendRepository',
      'FRONTEND_REPOSITORY',
    ),
    frontendBranch: fromContextOrEnv(
      app,
      'frontendBranch',
      'FRONTEND_BRANCH',
      'main',
    )!,
  };
}

/**
 * CraveQuest infrastructure stack (AWS CDK, TypeScript).
 *
 * Provisions the CraveQuest MVP deployment topology described in design.md
 * ("AWS deployment topology"):
 *
 *   User Browser ─▶ AWS Amplify Hosting (Frontend_App)
 *   User Browser ─▶ Amazon API Gateway ─▶ ECS Fargate container service
 *                     (Backend_API + Decider_2B server) ─▶ Amazon Bedrock
 *   Container ─▶ Amazon CloudWatch (logs + metrics)
 *
 * Requirements satisfied:
 *   - 26.1  Least-privilege IAM (no wildcard/admin actions; Bedrock Converse +
 *           own-log-group writes only).
 *   - 28.1  AWS Amplify Hosting for the frontend.
 *   - 28.2  Amazon API Gateway fronting the Backend_API.
 *   - 28.4  Container service (ECS Fargate) for Backend_API + Decider_2B server,
 *           with the container-choice reason documented below.
 *   - 28.5  Amazon CloudWatch log groups and metrics.
 *   - 28.6  All infrastructure defined as reviewable IaC (this stack).
 *   - 28.8  No DynamoDB / server-side persistence (MVP uses browser local
 *           storage). DynamoDB is intentionally omitted.
 *   - 28.9  Stack outputs exposing the frontend URL and the API URL.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * WHY A CONTAINER (ECS Fargate) INSTEAD OF AWS LAMBDA (Req 28.3 / 28.4)
 * ──────────────────────────────────────────────────────────────────────────
 * The Decider_2B server loads a 2-billion-parameter model and runs a
 * long-lived serving process (`strands-decider serve`) that must stay warm
 * across requests. This exceeds AWS Lambda's limits:
 *   - Deployment package: 2B weights + ML/serving deps far exceed the 250 MB
 *     unzipped limit.
 *   - Memory/duration: serving a 2B model needs large, persistently resident
 *     memory and a process that outlives Lambda's 15-minute execution cap.
 *   - Cold start: reloading 2B weights per cold start is impractically slow.
 * Therefore the Backend_API (co-located with the Decider_2B server) runs on a
 * container service. ECS Fargate is used here; AWS App Runner is the documented
 * alternative for a simpler managed option.
 */
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as apigwv2_integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as amplify from 'aws-cdk-lib/aws-amplify';
import * as path from 'path';
import { StackConfig } from './config';

export interface CraveQuestStackProps extends cdk.StackProps {
  readonly config: StackConfig;
}

export class CraveQuestStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: CraveQuestStackProps) {
    super(scope, id, props);

    const { config } = props;

    // ── Networking ──────────────────────────────────────────────────────
    // A small VPC for the Fargate service. The service runs in private
    // subnets; NAT gateways give it outbound access to Amazon Bedrock and
    // CloudWatch. The internal ALB and the API Gateway VPC Link live here too.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 1,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 24,
        },
      ],
    });

    // ── CloudWatch logging (Req 28.5) ───────────────────────────────────
    // A dedicated log group for the container. The task role is granted write
    // access to THIS group only (Req 26.1), never to all log groups.
    const containerLogGroup = new logs.LogGroup(this, 'BackendLogGroup', {
      logGroupName: `/cravequest/backend`,
      retention: this.resolveRetention(config.logRetentionDays),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── IAM: least-privilege roles (Req 26.1) ───────────────────────────
    // Execution role: used by the ECS agent to pull the image and write the
    // initial container logs. Scoped to the managed ECS execution policy
    // (ECR pull + log stream creation), which contains no wildcard admin
    // actions. We additionally scope its CloudWatch writes to our log group.
    const executionRole = new iam.Role(this, 'TaskExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'ECS task execution role: pull image + write bootstrap logs. No wildcard admin actions.',
    });
    // Grant only ECR image-pull and auth, scoped narrowly. (The repository ARN
    // for an asset image is managed by CDK; auth token requires account-level
    // resource which AWS defines as "*" for GetAuthorizationToken only.)
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrPullImage',
        effect: iam.Effect.ALLOW,
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        // CDK resolves the asset repository; restrict pulls to this account's
        // ECR repositories rather than a bare wildcard.
        resources: [
          `arn:${this.partition}:ecr:${this.region}:${this.account}:repository/*`,
        ],
      }),
    );
    // GetAuthorizationToken has no resource-level scoping in IAM; it is the one
    // ECR action AWS requires on "*". It grants only a short-lived auth token,
    // not admin, so it does not violate the no-admin rule (Req 26.1).
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'EcrAuthToken',
        effect: iam.Effect.ALLOW,
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*'],
      }),
    );
    // Allow the execution role to create/write log streams in OUR log group
    // only — not all log groups.
    containerLogGroup.grantWrite(executionRole);

    // Task role: the identity the Backend_API runs as at runtime. It is
    // granted ONLY (a) Bedrock Converse/InvokeModel on the configured model and
    // (b) CloudWatch writes to its own log group. No wildcard, no admin.
    const taskRole = new iam.Role(this, 'BackendTaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description:
        'Backend_API runtime role: Bedrock Converse on the configured model + own-log-group writes only.',
    });

    // Bedrock Converse / InvokeModel, scoped to specific model resource ARNs
    // (never "*"). Req 26.1 / 28.5.
    taskRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'BedrockConverse',
        effect: iam.Effect.ALLOW,
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: this.resolveBedrockModelArns(config),
      }),
    );
    // Own-log-group writes only (Req 26.1).
    containerLogGroup.grantWrite(taskRole);

    // ── Container image (Backend_API + Decider_2B) ──────────────────────
    // Built from the backend/ directory Dockerfile. The image bundles the
    // FastAPI Backend_API and the Decider_2B serving process.
    const backendImage = new ecr_assets.DockerImageAsset(this, 'BackendImage', {
      directory: path.join(__dirname, '..', '..', 'backend'),
      platform: ecr_assets.Platform.LINUX_AMD64,
    });

    // ── ECS Fargate service (Req 28.4) ──────────────────────────────────
    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      // Enable CloudWatch Container Insights for task/service metrics (Req 28.5).
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    const taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: config.cpu,
      memoryLimitMiB: config.memoryLimitMiB,
      executionRole,
      taskRole,
    });

    // Container environment / config wiring. Secrets (e.g. a Decider bearer
    // token) are intentionally NOT set here as plaintext; supply them at deploy
    // time from AWS Secrets Manager / SSM via `secrets:` (Req 26.3).
    const container = taskDefinition.addContainer('Backend', {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'backend',
        logGroup: containerLogGroup,
      }),
      environment: {
        // Decider (Strands Decider 2B)
        DECIDER_MODE: config.deciderMode,
        DECIDER_MODEL: config.deciderModel,
        STRANDS_DECIDER_API_BASE: config.strandsDeciderApiBase,
        DECISION_TIMEOUT_MS: '5000',
        DECISION_CONFIDENCE_THRESHOLD: '0.5',
        // Amazon Bedrock
        BEDROCK_MODEL_ID: config.bedrockModelId,
        BEDROCK_MAX_RETRIES: '3',
        BEDROCK_BACKOFF_INITIAL_MS: '500',
        BEDROCK_BACKOFF_MAX_MS: '8000',
        BEDROCK_ATTEMPT_TIMEOUT_MS: '30000',
        AWS_REGION: this.region,
        // API / CORS
        ALLOWED_ORIGINS: config.allowedOrigins,
      },
      portMappings: [{ containerPort: 8080 }],
    });

    const service = new ecs.FargateService(this, 'BackendService', {
      cluster,
      taskDefinition,
      desiredCount: config.desiredCount,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      // Keep all tasks healthy during deploys (avoids the default 50% dip),
      // and allow one extra task so a 2B-serving task can roll without an
      // availability gap.
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    // ── Internal Application Load Balancer ──────────────────────────────
    // Fronts the Fargate tasks privately; API Gateway reaches it over a VPC
    // Link. Internal (not internet-facing) so the only public entry is the API.
    const alb = new elbv2.ApplicationLoadBalancer(this, 'InternalAlb', {
      vpc,
      internetFacing: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    const listener = alb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
    });

    listener.addTargets('BackendTargets', {
      port: 8080,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service.loadBalancerTarget({
        containerName: container.containerName,
        containerPort: 8080,
      })],
      healthCheck: {
        path: '/health',
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(30),
      },
    });

    // ── Amazon API Gateway fronting the Backend_API (Req 28.2) ──────────
    // HTTP API with a private integration to the internal ALB via a VPC Link.
    const vpcLink = new apigwv2.VpcLink(this, 'ApiVpcLink', {
      vpc,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    const httpApi = new apigwv2.HttpApi(this, 'BackendApi', {
      apiName: 'cravequest-backend-api',
      description: 'Amazon API Gateway fronting the CraveQuest Backend_API.',
      corsPreflight: {
        allowOrigins: config.allowedOrigins.split(',').map((o) => o.trim()),
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ['content-type', 'authorization'],
      },
    });

    httpApi.addRoutes({
      path: '/{proxy+}',
      methods: [apigwv2.HttpMethod.ANY],
      integration: new apigwv2_integrations.HttpAlbIntegration(
        'AlbIntegration',
        listener,
        { vpcLink },
      ),
    });

    // ── AWS Amplify Hosting for the frontend (Req 28.1) ─────────────────
    // Serves the React + Vite static bundle and provides the public frontend
    // URL. When a repository is configured, Amplify builds from it; otherwise
    // the app is created for manual bundle deploys. No AWS credentials are ever
    // placed in frontend config (Req 26.2) — only the public API URL.
    const amplifyApp = new amplify.CfnApp(this, 'FrontendApp', {
      name: 'cravequest-frontend',
      description: 'AWS Amplify Hosting for the CraveQuest Frontend_App.',
      platform: 'WEB',
      ...(config.frontendRepository
        ? { repository: config.frontendRepository }
        : {}),
      environmentVariables: [
        // Public backend URL only — never credentials (Req 26.2).
        { name: 'VITE_API_BASE_URL', value: httpApi.apiEndpoint },
      ],
      buildSpec: [
        'version: 1',
        'frontend:',
        '  phases:',
        '    preBuild:',
        '      commands:',
        '        - cd frontend',
        '        - npm ci',
        '    build:',
        '      commands:',
        '        - npm run build',
        '  artifacts:',
        '    baseDirectory: frontend/dist',
        '    files:',
        '      - "**/*"',
        '  cache:',
        '    paths:',
        '      - frontend/node_modules/**/*',
      ].join('\n'),
    });

    const amplifyBranch = new amplify.CfnBranch(this, 'FrontendBranch', {
      appId: amplifyApp.attrAppId,
      branchName: config.frontendBranch,
      enableAutoBuild: Boolean(config.frontendRepository),
    });

    // ── Stack outputs (Req 28.9) ────────────────────────────────────────
    const frontendUrl = `https://${amplifyBranch.branchName}.${amplifyApp.attrDefaultDomain}`;

    new cdk.CfnOutput(this, 'FrontendUrl', {
      description: 'Public frontend URL (AWS Amplify Hosting).',
      value: frontendUrl,
    });

    new cdk.CfnOutput(this, 'ApiUrl', {
      description: 'Public API URL (Amazon API Gateway).',
      value: httpApi.apiEndpoint,
    });
  }

  /** Map a retention-day number to the nearest supported {@link logs.RetentionDays}. */
  private resolveRetention(days: number): logs.RetentionDays {
    const supported: Record<number, logs.RetentionDays> = {
      1: logs.RetentionDays.ONE_DAY,
      3: logs.RetentionDays.THREE_DAYS,
      5: logs.RetentionDays.FIVE_DAYS,
      7: logs.RetentionDays.ONE_WEEK,
      14: logs.RetentionDays.TWO_WEEKS,
      30: logs.RetentionDays.ONE_MONTH,
      60: logs.RetentionDays.TWO_MONTHS,
      90: logs.RetentionDays.THREE_MONTHS,
      180: logs.RetentionDays.SIX_MONTHS,
      365: logs.RetentionDays.ONE_YEAR,
    };
    return supported[days] ?? logs.RetentionDays.ONE_MONTH;
  }

  /**
   * Resolve the Bedrock foundation-model resource ARNs the task role may
   * invoke. Uses explicit ARNs when supplied (e.g. cross-region inference
   * profiles); otherwise derives a specific foundation-model ARN for the
   * configured model id. Never returns a bare wildcard (Req 26.1).
   */
  private resolveBedrockModelArns(config: StackConfig): string[] {
    if (config.bedrockModelArns && config.bedrockModelArns.length > 0) {
      return config.bedrockModelArns;
    }
    return [
      `arn:${this.partition}:bedrock:${this.region}::foundation-model/${config.bedrockModelId}`,
    ];
  }
}

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
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatch_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sns_subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
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
    //
    // SEC-4 (container image scanning): this image is pushed to the CDK-managed
    // asset ECR repository (`cdk-*-container-assets-*`). `DockerImageAsset` has
    // no `imageScanOnPush` prop, and switching to an explicit `ecr.Repository`
    // would require reworking the asset push flow (brittle, and risks breaking
    // `cdk deploy`). The least-disruptive, synth-clean remediation is to enable
    // scanning at the ACCOUNT level: turn on Amazon Inspector for ECR so every
    // repository (including the CDK asset repo) is scanned on push. See
    // infra/README.md "Container image scanning". Intentionally no explicit
    // ecr.Repository is created here.
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
      // OE-3: container-level health check so ECS can replace a hung task
      // independently of the ALB target-group probe. Uses a Python one-liner
      // (python:3.11-slim already ships python3) instead of installing curl, so
      // no extra apt layer bloats the image. Hits the same cheap /health
      // liveness endpoint the container serves on 8080 (backend/app/api/health.py).
      healthCheck: {
        command: [
          'CMD-SHELL',
          'python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(\'http://localhost:8080/health\', timeout=2).status==200 else 1)"',
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(30),
      },
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

    const targetGroup = listener.addTargets('BackendTargets', {
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

    // ── CO-1 / OE-1: shared operational-alerts SNS topic ────────────────
    // One topic is reused for both cost (CO-1) and operational (OE-1) alerts,
    // keeping subscriptions and billing simple. An email is subscribed only
    // when configured; otherwise the topic exists with no subscription.
    const opsAlerts = new sns.Topic(this, 'OpsAlerts', {
      displayName: 'CraveQuest operational alerts',
    });
    if (config.opsAlertEmail) {
      opsAlerts.addSubscription(
        new sns_subscriptions.EmailSubscription(config.opsAlertEmail),
      );
    }

    // ── CO-1: monthly AWS Budget (cost guardrail) ───────────────────────
    // The budget is ALWAYS created. Subscriber notifications are wired only
    // when a notification email is configured; when none is set we omit
    // `notificationsWithSubscribers` entirely so synth never emits an
    // empty-subscriber notification (which would fail deploy). Default monthly
    // limit is `budgetMonthlyLimitUsd` (≈50 USD) from config.ts.
    const budgetSubscribers = config.budgetNotificationEmail
      ? [
          {
            notification: {
              notificationType: 'ACTUAL',
              comparisonOperator: 'GREATER_THAN_OR_EQUAL_TO',
              threshold: 80,
              thresholdType: 'PERCENTAGE',
            },
            subscribers: [
              {
                subscriptionType: 'EMAIL',
                address: config.budgetNotificationEmail,
              },
            ],
          },
          {
            notification: {
              notificationType: 'FORECASTED',
              comparisonOperator: 'GREATER_THAN_OR_EQUAL_TO',
              threshold: 100,
              thresholdType: 'PERCENTAGE',
            },
            subscribers: [
              {
                subscriptionType: 'EMAIL',
                address: config.budgetNotificationEmail,
              },
            ],
          },
        ]
      : undefined;

    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: {
          amount: config.budgetMonthlyLimitUsd,
          unit: 'USD',
        },
      },
      notificationsWithSubscribers: budgetSubscribers,
    });

    // The AWS/Billing `EstimatedCharges` metric is published ONLY in us-east-1,
    // so a cross-region `cloudwatch.Alarm` on it would break synth/deploy in any
    // other region. We prefer the AWS Budgets notification path above. If an
    // EstimatedCharges alarm is ever wanted, gate it on region to stay safe:
    if (this.region === 'us-east-1') {
      // Optional, region-gated EstimatedCharges alarm could be created here.
      // Intentionally left out; AWS Budgets (above) is the cost guardrail.
    }

    // ── OE-1: CloudWatch alarms wired to the SNS topic ──────────────────
    const snsAction = new cloudwatch_actions.SnsAction(opsAlerts);

    // ALB target 5xx (backend-origin errors).
    new cloudwatch.Alarm(this, 'Alb5xxAlarm', {
      alarmDescription: 'Backend target 5xx responses are elevated.',
      metric: alb.metrics.httpCodeTarget(
        elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
        { period: cdk.Duration.minutes(5), statistic: 'Sum' },
      ),
      threshold: 5,
      evaluationPeriods: 2,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // Unhealthy hosts behind the target group.
    new cloudwatch.Alarm(this, 'UnhealthyHostsAlarm', {
      alarmDescription: 'One or more backend targets are unhealthy.',
      metric: targetGroup.metrics.unhealthyHostCount({
        period: cdk.Duration.minutes(1),
      }),
      threshold: 1,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // ECS CPU high.
    new cloudwatch.Alarm(this, 'EcsCpuHighAlarm', {
      alarmDescription: 'Backend service CPU utilization is high.',
      metric: service.metricCpuUtilization({
        period: cdk.Duration.minutes(5),
      }),
      threshold: 85,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // ECS memory high.
    new cloudwatch.Alarm(this, 'EcsMemoryHighAlarm', {
      alarmDescription: 'Backend service memory utilization is high.',
      metric: service.metricMemoryUtilization({
        period: cdk.Duration.minutes(5),
      }),
      threshold: 85,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // Bedrock invocation throttles (model throttling exhaustion visibility).
    const bedrockThrottlesMetric = new cloudwatch.Metric({
      namespace: 'AWS/Bedrock',
      metricName: 'InvocationThrottles',
      statistic: 'Sum',
      period: cdk.Duration.minutes(5),
    });
    new cloudwatch.Alarm(this, 'BedrockThrottlesAlarm', {
      alarmDescription: 'Amazon Bedrock is throttling invocations.',
      metric: bedrockThrottlesMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // ── REL-2 observability: fallback-rate custom metric + alarm ────────
    // The backend emits FallbackRate as CloudWatch EMF to stdout under the
    // namespace 'CraveQuest/Agent' (no extra IAM; CloudWatch extracts it from
    // the container logs). A sustained high fallback rate means the Decider is
    // silently degrading to the deterministic fallback — alarm on it.
    const fallbackRateMetric = new cloudwatch.Metric({
      namespace: 'CraveQuest/Agent',
      metricName: 'FallbackRate',
      statistic: 'Average',
      period: cdk.Duration.minutes(5),
    });
    new cloudwatch.Alarm(this, 'FallbackRateHighAlarm', {
      alarmDescription:
        'Decision fallback rate is sustained high (Decider likely degraded).',
      metric: fallbackRateMetric,
      threshold: config.fallbackRateAlarmThreshold,
      evaluationPeriods: 3,
      comparisonOperator:
        cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(snsAction);

    // ── OE-1: operational dashboard ─────────────────────────────────────
    const dashboard = new cloudwatch.Dashboard(this, 'OpsDashboard', {
      dashboardName: 'CraveQuest-Ops',
    });
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'ALB target 5xx',
        left: [
          alb.metrics.httpCodeTarget(elbv2.HttpCodeTarget.TARGET_5XX_COUNT, {
            period: cdk.Duration.minutes(5),
            statistic: 'Sum',
          }),
          alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, {
            period: cdk.Duration.minutes(5),
            statistic: 'Sum',
          }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: 'Unhealthy hosts',
        left: [
          targetGroup.metrics.unhealthyHostCount({
            period: cdk.Duration.minutes(1),
          }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: 'ECS CPU / memory',
        left: [
          service.metricCpuUtilization({ period: cdk.Duration.minutes(5) }),
          service.metricMemoryUtilization({ period: cdk.Duration.minutes(5) }),
        ],
      }),
      new cloudwatch.GraphWidget({
        title: 'Bedrock invocations / throttles',
        left: [
          new cloudwatch.Metric({
            namespace: 'AWS/Bedrock',
            metricName: 'Invocations',
            statistic: 'Sum',
            period: cdk.Duration.minutes(5),
          }),
          bedrockThrottlesMetric,
        ],
      }),
      new cloudwatch.GraphWidget({
        title: 'Decision fallback rate',
        left: [fallbackRateMetric],
      }),
    );

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

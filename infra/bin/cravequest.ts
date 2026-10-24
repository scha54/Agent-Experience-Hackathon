#!/usr/bin/env node
/**
 * CDK application entry point for CraveQuest.
 *
 * Synthesizes the single {@link CraveQuestStack} which provisions the full MVP
 * topology: Amplify Hosting (frontend), API Gateway (API edge), an ECS Fargate
 * container service running the FastAPI Backend_API + Decider_2B server,
 * CloudWatch log groups, and least-privilege IAM.
 *
 * Requirements: 26.1, 28.1, 28.2, 28.4, 28.5, 28.6, 28.8, 28.9
 */
import * as cdk from 'aws-cdk-lib';
import { CraveQuestStack } from '../lib/cravequest-stack';
import { loadStackConfig } from '../lib/config';

const app = new cdk.App();

// All deploy-time configuration is sourced from CDK context or environment
// variables — never hardcoded secrets or credentials (Req 26.3).
const config = loadStackConfig(app);

new CraveQuestStack(app, 'CraveQuestStack', {
  config,
  // Resolve the target account/region from the standard CDK environment
  // variables so the same source deploys to any configured account.
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description:
    'CraveQuest MVP: Amplify frontend, API Gateway, ECS Fargate (Backend_API + Decider_2B), CloudWatch, least-privilege IAM.',
  tags: {
    Project: 'CraveQuest',
    ManagedBy: 'aws-cdk',
  },
});

app.synth();

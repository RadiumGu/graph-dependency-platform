#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { NeptuneClusterStack } from '../lib/neptune-cluster-stack';
import { NeptuneEtlStack } from '../lib/neptune-etl-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_ACCOUNT_ID || process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_REGION || process.env.CDK_DEFAULT_REGION || 'ap-northeast-1',
};

// --- Neptune cluster (deploy first) ---
new NeptuneClusterStack(app, 'NeptuneClusterStack', {
  env,
  description: 'Amazon Neptune graph database cluster for dependency graph',
});

// --- ETL Lambda functions (deploy after cluster is ready) ---
new NeptuneEtlStack(app, 'NeptuneEtlStack', {
  env,
  description: 'Neptune ETL Lambda functions - DeepFlow, AWS, CFN pipelines',
});

app.synth();

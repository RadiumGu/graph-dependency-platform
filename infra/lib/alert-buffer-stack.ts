import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import { Construct } from 'constructs';
import * as path from 'path';

export class AlertBufferStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =========================================================
    // 引用已有资源（与 NeptuneEtlStack 保持一致）
    // =========================================================

    const vpcId = this.node.tryGetContext('vpcId') as string;
    const lambdaSgId = this.node.tryGetContext('lambdaSgId') as string;
    const neptuneEtlPolicyArn = this.node.tryGetContext('neptuneEtlPolicyArn') as string
      || `arn:aws:iam::${this.account}:policy/NeptuneETLPolicy`;

    const vpc = ec2.Vpc.fromLookup(this, 'ExistingVpc', { vpcId });

    const lambdaSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'LambdaSg',
      lambdaSgId,
      { allowAllOutbound: true },
    );

    const neptuneEtlPolicy = iam.ManagedPolicy.fromManagedPolicyArn(
      this,
      'NeptuneETLPolicy',
      neptuneEtlPolicyArn,
    );

    // =========================================================
    // 公共配置（从 context 读取）
    // =========================================================

    const neptuneEndpoint = this.node.tryGetContext('neptuneEndpoint') as string || 'YOUR_NEPTUNE_ENDPOINT';
    const neptunePort = this.node.tryGetContext('neptunePort') as string || '8182';
    const clickhouseHost = this.node.tryGetContext('clickhouseHost') as string || 'YOUR_CLICKHOUSE_HOST';
    const eksClusterName = this.node.tryGetContext('eksClusterName') as string || 'YOUR_EKS_CLUSTER_NAME';
    const bedrockModel = this.node.tryGetContext('bedrockModel') as string || 'global.anthropic.claude-sonnet-4-6';
    const bedrockKbId = this.node.tryGetContext('bedrockKbId') as string || '';
    const slackWebhookUrl = this.node.tryGetContext('slackWebhookUrl') as string || '';

    const vpcSubnets: ec2.SubnetSelection = {
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    };

    // =========================================================
    // DynamoDB 表：gp-alert-buffer
    // =========================================================

    const alertBufferTable = new dynamodb.Table(this, 'AlertBufferTable', {
      tableName: 'gp-alert-buffer',
      partitionKey: { name: 'window_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'alert_fingerprint', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    alertBufferTable.addGlobalSecondaryIndex({
      indexName: 'window_id-index',
      partitionKey: { name: 'window_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // =========================================================
    // Lambda Execution Role：gp-window-flush
    // =========================================================

    const windowFlushRole = new iam.Role(this, 'WindowFlushLambdaRole', {
      roleName: 'gp-window-flush-lambda-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole'),
        neptuneEtlPolicy,
      ],
    });

    // DynamoDB gp-alert-buffer 读写权限
    windowFlushRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AlertBufferDynamoDBAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:PutItem',
        'dynamodb:GetItem',
        'dynamodb:Query',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:BatchWriteItem',
        'dynamodb:BatchGetItem',
      ],
      resources: [
        alertBufferTable.tableArn,
        `${alertBufferTable.tableArn}/index/*`,
      ],
    }));

    // Bedrock InvokeModel 权限
    windowFlushRole.addToPolicy(new iam.PolicyStatement({
      sid: 'BedrockInvokeModel',
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: ['*'],
    }));

    // CloudTrail LookupEvents 权限
    windowFlushRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudTrailLookupEvents',
      effect: iam.Effect.ALLOW,
      actions: ['cloudtrail:LookupEvents'],
      resources: ['*'],
    }));

    // CloudWatch GetMetricData + Logs 查询权限
    windowFlushRole.addToPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchQueryAccess',
      effect: iam.Effect.ALLOW,
      actions: [
        'cloudwatch:GetMetricData',
        'cloudwatch:GetMetricStatistics',
        'cloudwatch:ListMetrics',
        'logs:StartQuery',
        'logs:GetQueryResults',
        'logs:StopQuery',
        'logs:DescribeLogGroups',
        'logs:FilterLogEvents',
      ],
      resources: ['*'],
    }));

    // EC2 DescribeInstances 权限
    windowFlushRole.addToPolicy(new iam.PolicyStatement({
      sid: 'EC2DescribeAccess',
      effect: iam.Effect.ALLOW,
      actions: ['ec2:DescribeInstances'],
      resources: ['*'],
    }));

    // =========================================================
    // Lambda：gp-window-flush
    // =========================================================

    const windowFlushFn = new lambda.Function(this, 'WindowFlushLambda', {
      functionName: 'gp-window-flush',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'window_flush_handler.window_flush_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/rca_window_flush')),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      role: windowFlushRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: this.region,
        CLICKHOUSE_HOST: clickhouseHost,
        CLICKHOUSE_PORT: '8123',
        BUFFER_TABLE_NAME: alertBufferTable.tableName,
        BEDROCK_MODEL: bedrockModel,
        BEDROCK_KB_ID: bedrockKbId,
        SLACK_WEBHOOK_URL: slackWebhookUrl,
        EKS_CLUSTER_NAME: eksClusterName,
      },
      description: 'Alert window flush: aggregate buffered alerts → RCA Lambda trigger',
    });

    // =========================================================
    // EventBridge Scheduler IAM Role（触发 window-flush Lambda）
    // =========================================================

    const schedulerRole = new iam.Role(this, 'AlertWindowSchedulerRole', {
      roleName: 'gp-alert-window-scheduler-role',
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
    });

    schedulerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeWindowFlushLambda',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [
        windowFlushFn.functionArn,
        `${windowFlushFn.functionArn}:*`,
      ],
    }));

    // =========================================================
    // CloudFormation Outputs
    // =========================================================

    new cdk.CfnOutput(this, 'AlertBufferTableName', {
      exportName: 'gp-alert-buffer-table-name',
      value: alertBufferTable.tableName,
      description: 'DynamoDB alert buffer table name',
    });

    new cdk.CfnOutput(this, 'AlertBufferTableArn', {
      exportName: 'gp-alert-buffer-table-arn',
      value: alertBufferTable.tableArn,
      description: 'DynamoDB alert buffer table ARN',
    });

    new cdk.CfnOutput(this, 'WindowFlushFunctionArn', {
      exportName: 'gp-window-flush-function-arn',
      value: windowFlushFn.functionArn,
      description: 'gp-window-flush Lambda ARN (set WINDOW_FLUSH_FUNCTION_ARN in rca/.env)',
    });

    new cdk.CfnOutput(this, 'SchedulerRoleArn', {
      exportName: 'gp-alert-window-scheduler-role-arn',
      value: schedulerRole.roleArn,
      description: 'EventBridge Scheduler role ARN (set SCHEDULER_ROLE_ARN in rca/.env)',
    });

    // RCA Lambda 需要的权限摘要（供 deploy.sh 参考）
    new cdk.CfnOutput(this, 'RcaLambdaRequiredPermissions', {
      value: JSON.stringify({
        dynamodb: [
          `arn:aws:dynamodb:${this.region}:${this.account}:table/gp-alert-buffer`,
        ],
        actions: [
          'dynamodb:PutItem',
          'dynamodb:Query',
          'dynamodb:GetItem',
          'scheduler:CreateSchedule',
          'scheduler:DeleteSchedule',
          'iam:PassRole (scheduler role)',
        ],
      }),
      description: 'Permissions to add to RCA Lambda role (see deploy.sh comments)',
    });

    // Tags
    cdk.Tags.of(this).add('Project', 'graph-dp');
    cdk.Tags.of(this).add('Phase', 'alert-aggregation');
    cdk.Tags.of(this).add('CreatedBy', 'openclaw-agent');
  }
}

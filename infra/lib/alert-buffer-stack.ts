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
    // slackWebhookUrl 故意不写进 cdk.json —— 它是 Slack 机密，cdk.json 进 git。
    // 部署时用 -c slackWebhookUrl=... 传入，否则这里回退成空串，
    // window_flush_handler 会静默跳过通知（if not webhook: return），
    // 表现为"RCA 分析完成但无人收到告警"。
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
      // Graviton2（arm64）：与 EKS 数据面（t4g.large / AL2023_ARM_64_STANDARD）一致，
      // 同规格约省 20% 费用。必须显式声明 —— CDK 默认是 X86_64，
      // 缺这一行会把线上已迁移到 arm64 的函数在下次 deploy 时打回 x86_64，
      // 而资产里的 .so 是 aarch64 编译产物，架构不符会静默退化成纯 Python 回退实现。
      architecture: lambda.Architecture.ARM_64,
      handler: 'window_flush_handler.window_flush_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/rca_window_flush')),
      // ⚠️ 2026-09-20 从 60s 提到 300s —— 实测依据而非估算。
      //
      // 线上切到 strands（Layer 2 Prober 走 ReAct 编排）后的一次真实执行：
      //
      //     15:25:24  WindowFlush 启动
      //     15:25:32  RCA 分析开始              （启动开销 8s）
      //     15:25:43  Creating Strands MetricsClient
      //     15:26:19  RCA complete in 46.3s     （此时已用 55s）
      //               之后还要写图谱 / 发通知   → 60s 撞墙
      //     结果：Sandbox.Timedout after 60.00 seconds
      //
      // 结构性矛盾：`rca/core/rca_engine.py:781` 给 strands 的 Step 3d
      // timeout **本身就是 60s**，而整个 Lambda 也只有 60s ——
      // 加上启动开销与 RCA 前后步骤，永远不可能在预算内完成。
      // 这个组合在 direct 时代能过（Step 3d 只给 12s），切 strands 后必然超时。
      //
      // 300s 的取法：Step 3d 最坏 60s + RCA 其余步骤实测约 15s +
      // 启动 8s + 写图谱与通知，留约 3 倍余量。
      // 不设更大：这是**批处理**窗口 flush，不是交互路径，
      // 但超时过长会让真正卡死的调用占着并发额度不放。
      timeout: cdk.Duration.seconds(300),
      // 256MB → 512MB：实测 strands 引擎构造峰值 67MB，本身够用；
      // 提一档是因为 Lambda 的 CPU 配额随内存线性分配，
      // 而 ReAct 多轮调用是 CPU 敏感的 —— 加内存反而可能降低总耗时与成本。
      memorySize: 512,
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
        // EKS_CLUSTER 是 actions/action_executor.py 读的键名，
        // 而其余 collectors 读 EKS_CLUSTER_NAME。两个都给，避免半自动动作拿到空串。
        EKS_CLUSTER: eksClusterName,
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

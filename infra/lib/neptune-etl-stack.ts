import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as eks from 'aws-cdk-lib/aws-eks';
import { Construct } from 'constructs';
import * as path from 'path';

export class NeptuneEtlStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // =========================================================
    // 引用已有资源（不重建）
    // =========================================================

    const vpcId = this.node.tryGetContext('vpcId') as string;
    const lambdaSgId = this.node.tryGetContext('lambdaSgId') as string;
    const neptuneEtlPolicyArn = this.node.tryGetContext('neptuneEtlPolicyArn') as string
      || `arn:aws:iam::${this.account}:policy/NeptuneETLPolicy`;

    // VPC（引用已有）
    const vpc = ec2.Vpc.fromLookup(this, 'ExistingVpc', {
      vpcId,
    });

    // Lambda 安全组（已有 neptune-lambda-sg）
    const lambdaSg = ec2.SecurityGroup.fromSecurityGroupId(
      this,
      'NeptuneLambdaSg',
      lambdaSgId,
      { allowAllOutbound: true },
    );

    // 已有 NeptuneETLPolicy（neptune-db:connect 等权限）
    const neptuneEtlPolicy = iam.ManagedPolicy.fromManagedPolicyArn(
      this,
      'NeptuneETLPolicy',
      neptuneEtlPolicyArn,
    );

    // =========================================================
    // Lambda Execution Role（新建，附加已有 Policy + 额外只读权限）
    // =========================================================
    const lambdaRole = new iam.Role(this, 'NeptuneEtlLambdaRole', {
      roleName: 'neptune-etl-lambda-role',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaVPCAccessExecutionRole'),
        neptuneEtlPolicy,
      ],
    });

    // EC2/EKS/ELBv2/Lambda/StepFn/DynamoDB/CFN 只读权限
    lambdaRole.addToPolicy(new iam.PolicyStatement({
      sid: 'AWSReadOnlyForETL',
      effect: iam.Effect.ALLOW,
      actions: [
        // EC2
        'ec2:DescribeInstances',
        'ec2:DescribeSubnets',
        'ec2:DescribeVpcs',
        'ec2:DescribeSecurityGroups',
        // EKS
        'eks:DescribeCluster',
        'eks:ListClusters',
        'eks:ListNodegroups',
        'eks:DescribeNodegroup',
        // ELBv2 (ALB)
        'elasticloadbalancing:DescribeLoadBalancers',
        'elasticloadbalancing:DescribeTargetGroups',
        'elasticloadbalancing:DescribeTargetHealth',
        'elasticloadbalancing:DescribeListeners',
        'elasticloadbalancing:DescribeRules',
        'elasticloadbalancing:DescribeTags',
        // Lambda
        'lambda:ListFunctions',
        'lambda:GetFunctionConfiguration',
        'lambda:ListTags',
        // Step Functions
        'states:ListStateMachines',
        'states:DescribeStateMachine',
        'states:ListTagsForResource',
        // DynamoDB
        'dynamodb:ListTables',
        'dynamodb:DescribeTable',
        'dynamodb:ListTagsOfResource',
        // CloudFormation
        'cloudformation:GetTemplate',
        'cloudformation:ListStackResources',
        'cloudformation:DescribeStacks',
        'cloudformation:DescribeStackResources',
        // AutoScaling（EKS NodeGroup 解析）
        'autoscaling:DescribeAutoScalingGroups',
        // STS（EKS token）
        'sts:GetCallerIdentity',
        // RDS / Aurora / Neptune
        'rds:DescribeDBInstances',
        'rds:DescribeDBClusters',
        'rds:ListTagsForResource',
        // S3
        's3:ListAllMyBuckets',
        's3:GetBucketLocation',
        's3:GetBucketTagging',
        // SQS
        'sqs:ListQueues',
        'sqs:GetQueueAttributes',
        'sqs:ListQueueTags',
        // SNS
        'sns:ListTopics',
        'sns:GetTopicAttributes',
        'sns:ListTagsForResource',
        // ECR
        'ecr:DescribeRepositories',
        'ecr:ListTagsForResource',
        // CloudWatch（EC2/Lambda 性能指标）
        'cloudwatch:GetMetricStatistics',
        'cloudwatch:GetMetricData',
        'cloudwatch:ListMetrics',
        'cloudwatch:DescribeAlarms',
        // Lambda Event Source（SQS trigger 映射）
        'lambda:ListEventSourceMappings',
        // Network Flow Monitor（EC2 网络 RTT/健康指标）
        'networkflowmonitor:ListMonitors',
        'networkflowmonitor:GetMonitor',
      ],
      resources: ['*'],
    }));

    // =========================================================
    // 公共配置
    // =========================================================
    const neptuneEndpoint = this.node.tryGetContext('neptuneEndpoint') as string || 'YOUR_NEPTUNE_ENDPOINT';
    const neptunePort = this.node.tryGetContext('neptunePort') as string || '8182';
    const awsRegion = this.region;
    const clickhouseHost = this.node.tryGetContext('clickhouseHost') as string || 'YOUR_CLICKHOUSE_HOST';
    const eksClusterName = this.node.tryGetContext('eksClusterName') as string || 'YOUR_EKS_CLUSTER_NAME';
    const cfnStackNames = this.node.tryGetContext('cfnStackNames') as string || 'YOUR_CFN_STACK1,YOUR_CFN_STACK2';

    // VPC 私有子网选择（通过已有 neptune-lambda-sg 所在子网，使用 subnetType=PRIVATE_WITH_EGRESS）
    // VPC.fromLookup 会在 synth 时从 context 读取，部署时从实际 VPC 读取私有子网
    const vpcSubnets: ec2.SubnetSelection = {
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    };

    // =========================================================
    // Shared Lambda Layer: neptune-client-base
    // =========================================================
    const neptuneClientLayer = new lambda.LayerVersion(this, 'NeptuneClientBaseLayer', {
      layerVersionName: 'neptune-client-base',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/shared')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: 'Shared Neptune Gremlin client utilities (neptune_query, safe_str, extract_value)',
    });

    // =========================================================
    // Lambda 1: neptune-etl-from-deepflow（每5分钟）
    // =========================================================
    const deepflowEtlFn = new lambda.Function(this, 'NeptuneEtlFromDeepflow', {
      functionName: 'neptune-etl-from-deepflow',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_deepflow.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_deepflow')),
      timeout: cdk.Duration.minutes(4),
      memorySize: 256,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        CLICKHOUSE_HOST: clickhouseHost,
        CLICKHOUSE_PORT: '8123',
        CH_HOST: clickhouseHost,
        CH_PORT: '8123',
        INTERVAL_MIN: '6',
        EKS_CLUSTER_ARN: `arn:aws:eks:${this.region}:${this.account}:cluster/${eksClusterName}`,
      },
      layers: [neptuneClientLayer],
      description: 'ETL: ClickHouse L7 flow_log → Neptune Calls/HasMetrics edges + perf metrics (every 5min)',
    });

    new events.Rule(this, 'DeepflowEtlSchedule', {
      ruleName: 'neptune-etl-every-5min',
      description: 'Trigger neptune-etl-from-deepflow every 5 minutes',
      schedule: events.Schedule.rate(cdk.Duration.minutes(5)),
      targets: [new targets.LambdaFunction(deepflowEtlFn)],
    });

    // =========================================================
    // Lambda 2: neptune-etl-from-aws（每15分钟）
    // =========================================================
    const awsEtlFn = new lambda.Function(this, 'NeptuneEtlFromAws', {
      functionName: 'neptune-etl-from-aws',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_aws')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        EKS_CLUSTER_NAME: eksClusterName,
      },
      layers: [neptuneClientLayer],
      description: 'ETL: AWS API static topology → Neptune nodes/edges (every 15min)',
    });

    new events.Rule(this, 'AwsEtlSchedule', {
      ruleName: 'neptune-etl-every-15min',
      description: 'Trigger neptune-etl-from-aws every 15 minutes',
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      targets: [new targets.LambdaFunction(awsEtlFn)],
    });

    // EKS Access Entry: allow ETL Lambda to call K8s API (read-only)
    // 同时绑 kubernetesGroups (映射到自定义 ClusterRole neptune-etl-reader)
    // 和 AmazonEKSViewPolicy (防御性，提供 namespace-scoped 只读)
    // ClusterRoleBinding 在 one-observability-demo/PetAdoptions/k8s-manifests/06-rbac.yaml
    new eks.CfnAccessEntry(this, 'EtlLambdaEksAccessEntry', {
      clusterName: eksClusterName,
      principalArn: lambdaRole.roleArn,
      type: 'STANDARD',
      kubernetesGroups: ['neptune-etl-readers'],
      accessPolicies: [{
        policyArn: 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy',
        accessScope: { type: 'cluster' },
      }],
    });

    // =========================================================
    // Lambda 3: neptune-etl-from-cfn（CFN 部署后 + 每日 2:00 CST）
    // =========================================================
    const cfnEtlFn = new lambda.Function(this, 'NeptuneEtlFromCfn', {
      functionName: 'neptune-etl-from-cfn',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_cfn.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_cfn')),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        CFN_STACK_NAMES: cfnStackNames,
      },
      layers: [neptuneClientLayer],
      description: 'ETL: CFN template declared deps → Neptune DependsOn edges (on deploy + daily)',
    });

    // 触发方式1: CFN 部署完成后自动触发（ServicesEks2 或 Applications 更新/创建完成）
    new events.Rule(this, 'CfnEtlOnStackUpdate', {
      ruleName: 'neptune-etl-cfn-on-deploy',
      description: 'Trigger neptune-etl-from-cfn on CFN stack update/create complete',
      eventPattern: {
        source: ['aws.cloudformation'],
        detailType: ['CloudFormation Stack Status Change'],
        detail: {
          'status-details': {
            status: ['UPDATE_COMPLETE', 'CREATE_COMPLETE'],
          },
          'stack-id': cfnStackNames.split(',').map(name => ({
            prefix: `arn:aws:cloudformation:${this.region}:${this.account}:stack/${name.trim()}/`,
          })),
        },
      },
      targets: [new targets.LambdaFunction(cfnEtlFn)],
    });

    // 触发方式2: 每日 2:00 AM CST = UTC 18:00 前一天
    new events.Rule(this, 'CfnEtlDailySync', {
      ruleName: 'neptune-etl-cfn-daily',
      description: 'Trigger neptune-etl-from-cfn daily at 2:00 AM CST (UTC 18:00)',
      schedule: events.Schedule.cron({ hour: '18', minute: '0' }),
      targets: [new targets.LambdaFunction(cfnEtlFn)],
    });

    // =========================================================
    // CloudFormation Outputs
    // =========================================================
    new cdk.CfnOutput(this, 'DeepflowEtlFunctionArn', {
      value: deepflowEtlFn.functionArn,
      description: 'neptune-etl-from-deepflow Lambda ARN',
    });
    new cdk.CfnOutput(this, 'AwsEtlFunctionArn', {
      value: awsEtlFn.functionArn,
      description: 'neptune-etl-from-aws Lambda ARN',
    });
    new cdk.CfnOutput(this, 'CfnEtlFunctionArn', {
      value: cfnEtlFn.functionArn,
      description: 'neptune-etl-from-cfn Lambda ARN',
    });
    new cdk.CfnOutput(this, 'LambdaRoleArn', {
      value: lambdaRole.roleArn,
      description: 'Shared Lambda Execution Role ARN',
    });

    // Tags
    cdk.Tags.of(this).add('Project', 'graph-dp');
    cdk.Tags.of(this).add('Phase', 'exploration');
    cdk.Tags.of(this).add('CreatedBy', 'openclaw-agent');

    // =========================================================
    // Lambda 4: neptune-etl-trigger（事件驱动，AWS 基础设施变更触发）
    // =========================================================

    // 独立 Role（避免与共享 lambdaRole 形成 CFN 循环依赖）
    const etlTriggerRole = new iam.Role(this, 'NeptuneEtlTriggerRole', {
      roleName: 'NeptuneEtlTriggerRole',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    etlTriggerRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeAwsEtlLambda',
      effect: iam.Effect.ALLOW,
      actions: ['lambda:InvokeFunction'],
      resources: [awsEtlFn.functionArn],
    }));

    // SQS DLQ（失败消息保留 7 天）
    const etlTriggerDlq = new sqs.Queue(this, 'EtlTriggerDlq', {
      queueName: 'neptune-etl-trigger-dlq',
      retentionPeriod: cdk.Duration.days(7),
    });

    // SQS 去重缓冲队列
    // visibilityTimeout 必须 >= Lambda timeout（90s），取 300s 留余量
    const etlTriggerQueue = new sqs.Queue(this, 'EtlTriggerQueue', {
      queueName: 'neptune-etl-trigger-queue',
      visibilityTimeout: cdk.Duration.seconds(300),
      deadLetterQueue: {
        queue: etlTriggerDlq,
        maxReceiveCount: 2,
      },
    });

    // 允许 EventBridge 向 SQS 发送消息
    etlTriggerQueue.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowEventBridgeSend',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('events.amazonaws.com')],
      actions: ['sqs:SendMessage'],
      resources: [etlTriggerQueue.queueArn],
    }));

    // 触发器 Lambda（不在 VPC 内，不需访问 Neptune）
    const etlTriggerFn = new lambda.Function(this, 'NeptuneEtlTrigger', {
      functionName: 'neptune-etl-trigger',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_trigger.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_trigger')),
      timeout: cdk.Duration.seconds(90),       // 30s 延迟 + invoke 开销
      memorySize: 128,
      role: etlTriggerRole,
      reservedConcurrentExecutions: 1,         // 防止并发写入 Neptune
      environment: {
        ETL_FUNCTION_NAME: awsEtlFn.functionName,
        TRIGGER_DELAY_SECONDS: '30',
        REGION: awsRegion,
      },
      description: 'Event-driven trigger: AWS infra change → 30s delay → neptune-etl-from-aws',
    });

    // SQS → Lambda 事件源
    // maxBatchingWindow=30s：批量收集同一波变更的多个事件，避免触发多次 ETL
    etlTriggerFn.addEventSource(new lambdaEventSources.SqsEventSource(etlTriggerQueue, {
      batchSize: 10,
      maxBatchingWindow: cdk.Duration.seconds(30),
    }));

    // ---- EventBridge Rules → SQS ----

    // Rule 1: RDS 实例/集群事件（failover、修改等）
    new events.Rule(this, 'EtlTriggerRdsEvents', {
      ruleName: 'neptune-etl-trigger-rds',
      description: 'Trigger ETL on RDS DB instance/cluster events (failover, modification)',
      eventPattern: {
        source: ['aws.rds'],
        detailType: ['RDS DB Instance Event', 'RDS DB Cluster Event'],
      },
      targets: [new targets.SqsQueue(etlTriggerQueue)],
    });

    // Rule 2: EC2 实例状态变更
    new events.Rule(this, 'EtlTriggerEc2Events', {
      ruleName: 'neptune-etl-trigger-ec2',
      description: 'Trigger ETL on EC2 instance state change',
      eventPattern: {
        source: ['aws.ec2'],
        detailType: ['EC2 Instance State-change Notification'],
        detail: {
          state: ['running', 'terminated', 'stopped'],
        },
      },
      targets: [new targets.SqsQueue(etlTriggerQueue)],
    });

    // Rule 3: EKS 托管节点组状态变更（扩缩容）
    new events.Rule(this, 'EtlTriggerEksEvents', {
      ruleName: 'neptune-etl-trigger-eks',
      description: 'Trigger ETL on EKS managed node group status change',
      eventPattern: {
        source: ['aws.eks'],
        detailType: ['EKS Managed Node Group Status Change'],
      },
      targets: [new targets.SqsQueue(etlTriggerQueue)],
    });

    // Rule 4: ElastiCache 节点替换/重启事件
    new events.Rule(this, 'EtlTriggerElastiCacheEvents', {
      ruleName: 'neptune-etl-trigger-elasticache',
      description: 'Trigger ETL on ElastiCache node replacement / reboot',
      eventPattern: {
        source: ['aws.elasticache'],
        detailType: [
          'ElastiCache Replication Group Events',
          'ElastiCache Cache Cluster Events',
        ],
      },
      targets: [new targets.SqsQueue(etlTriggerQueue)],
    });

    // Rule 5: ALB Target Group 变更（通过 CloudTrail API 事件）
    new events.Rule(this, 'EtlTriggerAlbEvents', {
      ruleName: 'neptune-etl-trigger-alb',
      description: 'Trigger ETL on ALB target group register/deregister (via CloudTrail)',
      eventPattern: {
        source: ['aws.elasticloadbalancing'],
        detailType: ['AWS API Call via CloudTrail'],
        detail: {
          eventSource: ['elasticloadbalancing.amazonaws.com'],
          eventName: [
            'RegisterTargets',
            'DeregisterTargets',
            'CreateTargetGroup',
            'DeleteTargetGroup',
          ],
        },
      },
      targets: [new targets.SqsQueue(etlTriggerQueue)],
    });

    new cdk.CfnOutput(this, 'EtlTriggerFunctionArn', {
      value: etlTriggerFn.functionArn,
      description: 'neptune-etl-trigger Lambda ARN',
    });
    new cdk.CfnOutput(this, 'EtlTriggerQueueUrl', {
      value: etlTriggerQueue.queueUrl,
      description: 'neptune-etl-trigger SQS queue URL',
    });
  }
}

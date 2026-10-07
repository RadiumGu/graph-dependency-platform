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
    // AgentCore 的 Nutrition 知识库 ID。值取自 2026-10-06 对
    // neptune-etl-from-agentcore 的实测环境变量 —— cdk import 要求逐项一致。
    const agentcoreNutritionKbId = this.node.tryGetContext('agentcoreNutritionKbId') as string || 'YOUR_AGENTCORE_KB_ID';

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
      // ⚠️ fromAsset 打包这个目录的**当前内容**。部署前必须先跑
      //     bash ../lambda/shared/build.sh
      // 它把 requirements.txt 的依赖装进 python/。
      //
      // 不跑的后果（2026-10-05 用 cdk synth 实测）：asset 只有 10 个文件，
      // 而线上 Layer 有 90 个条目 —— 其中 77 个是依赖文件。
      // python/neptune_client_base.py 第一行就 `import requests, urllib3`，
      // 它们不由 Lambda 运行时提供，所以那次 deploy 会让**挂载本 Layer 的
      // 6 个 ETL 函数在 import 阶段全部挂掉**，而 cdk diff 只显示一行
      // `[~] Content (requires replacement)`，与正常的内容更新无从区分。
      //
      // 同族故障 2026-10-04 在 gp-window-flush 上真实发生过一次，
      // 见 docs/lessons/cdk-fromasset-packages-ungitted-deps.md。
      // 判据由 tests/test_128_cdk_layer_asset_must_carry_deps.py 守着。
      // exclude __pycache__：`fromAsset` 默认**不排除**它。
      //
      // 2026-10-06 实测：build.sh 构建完 Layer 之后，我在本机用
      // `PYTHONPATH=infra/lambda/shared/python python3.11 -m pytest` 跑测试，
      // 导入这些模块就在同目录生成了 __pycache__/*.cpython-311.pyc。
      // 随后的 cdk deploy 把 **69 个 .pyc 打进了 Layer 23**。
      //
      // 这些 .pyc 是 3.11 的，而运行时是 3.12，解释器直接忽略它们 ——
      // 所以不会坏，只是白占体积并让 Layer 内容不可复现。
      //
      // 根因在时序：build.sh 的 __pycache__ 清理发生在**构建时**，
      // 而污染可以发生在构建之后、部署之前的任意时刻（跑一次测试即可）。
      // 所以「记得先 clean 再 deploy」治不了根 —— 在 asset 层排除才行。
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/shared'), {
        exclude: ['**/__pycache__/**', '**/*.pyc'],
      }),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      // ⚠️ 架构：这个 Layer **不是**完全架构无关的。
      //
      // 5 个业务模块（graph_* / neptune_client_base）是纯 Python，
      // 但依赖里 charset_normalizer 带 2 个预编译扩展：
      //     python/charset_normalizer/md.cpython-312-<arch>-linux-gnu.so
      //     python/charset_normalizer/md__mypyc.cpython-312-<arch>-linux-gnu.so
      //
      // 2026-10-06 实测：线上 Layer 20/21 里这两个是 **aarch64**，
      // 而挂这个 Layer 的 6 个 ETL **全部是 x86_64** —— 也就是说
      // charset_normalizer 的 C 加速在它们身上一直是失效的。
      // 不报错是因为它有纯 Python 回退（PyYAML 同一机制：
      // `try: from .cyaml import * except ImportError:` 吞掉架构不匹配）。
      //
      // 所以双架构声明在**功能上**成立（回退保证可用），代价是在不匹配的
      // 那一侧失去 C 加速。这是一次静默降级，不是故障 —— 但它来自
      // 「在 ARM 构建机上为 x86_64 函数构建」，而不是有意的取舍。
      // build.sh 用 --platform 固定目标架构，判据由
      // tests/test_128_cdk_layer_asset_must_carry_deps.py 守着。
      //
      // 未声明双架构时 AWS 视为仅 x86_64，会阻止 arm64 函数挂载 ——
      // gp-window-flush 是本仓唯一的 arm64 函数，所以这个声明要留着。
      compatibleArchitectures: [lambda.Architecture.X86_64, lambda.Architecture.ARM_64],
      description: 'Shared Neptune Gremlin client + graph contract utilities (deps via build.sh)',
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
      description: 'ETL: ClickHouse L7 flow_log -> Neptune Calls/HasMetrics edges + perf metrics (every 5min)',
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
      description: 'ETL: AWS API static topology -> Neptune nodes/edges (every 15min)',
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
      description: 'ETL: CFN template declared deps -> Neptune DependsOn edges (on deploy + daily)',
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
    // Lambda 5-7: 原先手工创建的 3 个 ETL —— 2026-10-06 纳入本栈
    // =========================================================
    //
    // 这三个函数是 2026-09-06 用 `aws lambda create-function` 手工建的，
    // 不属于任何 CloudFormation 栈。立卡见
    // docs/lessons/tech-debt-etl-lambdas-outside-cfn.md。它记录的后果：
    //
    //   1. 重建栈会漏掉它们 —— 从 NeptuneEtlStack 重建环境只会得到 4 个 ETL，
    //      X-Ray 与 Application Signals 两个数据源静默缺失，图上少一批依赖边
    //      而没有任何东西报错。
    //   2. 配置漂移无人看管 —— 2026-09-06 实测 4 个函数在 Layer v19 而
    //      appsignals 还在 v15，因为重指 Layer 的人手工逐个改、漏了一个。
    //   3. 契约里的 node_scope.name_prefix_map 是为它们开的后门，
    //      栈归属能判出来后那两条就该退场。
    //
    // 下面的属性全部取自 2026-10-06 对线上的实测（cdk import 要求逐项一致，
    // 不一致会要求替换资源，而替换 Lambda 会让 EventBridge target 失联）：
    //
    //   三者共同: python3.12 / x86_64 / neptune-etl-lambda-role /
    //            subnet-0f801fa79077eb277 + subnet-047a94f9c5ab6302a /
    //            sg-078f24929b25f09cd
    //   各自不同: handler、memorySize、timeout、各自的环境变量
    //
    // ⚠️ etl_appsignals 的包必须先跑 infra/lambda/etl_appsignals/build.sh ——
    //    它仓库里只有 1 个被跟踪文件而线上包有 43 条，直接 fromAsset 会发布
    //    一个缺依赖的空壳。见 docs/lessons/cdk-fromasset-packages-ungitted-deps.md。
    //    etl_xray（2/2）与 etl_agentcore（1/1）仓库内容与线上一致，可直接打包。

    const xrayEtlFn = new lambda.Function(this, 'NeptuneEtlFromXray', {
      functionName: 'neptune-etl-from-xray',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_xray.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_xray'), {
        exclude: ['**/__pycache__/**', '**/*.pyc'],
      }),
      timeout: cdk.Duration.seconds(180),
      memorySize: 256,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        XRAY_LOOKBACK_HOURS: '24',
        XRAY_STALE_SECONDS: '21600',
      },
      layers: [neptuneClientLayer],
      description: 'ETL: X-Ray service graph -> Neptune (hourly)',
    });


    const appsignalsEtlFn = new lambda.Function(this, 'NeptuneEtlFromAppsignals', {
      functionName: 'neptune-etl-from-appsignals',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_appsignals.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_appsignals'), {
        // build.sh / requirements.txt 是构建期文件，不该进运行时包。
        // 2026-10-06 实测：线上包 38 个文件，本地资产目录跑完 build.sh 后是
        // 40 个 —— 多出的正是这两个。排除后文件集与线上逐项一致。
        exclude: ['**/__pycache__/**', '**/*.pyc', 'build.sh', 'requirements.txt'],
      }),
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        APPSIGNALS_LOOKBACK_SECONDS: '86400',
      },
      layers: [neptuneClientLayer],
      description: 'ETL: Application Signals service map -> Neptune (every 15 min)',
    });


    // botocore-current：agentcore ETL 专用，不可省。
    //
    // 2026-10-06 我亲手踩了这个坑：把 3 个函数纳入栈后修复代码漂移时，我读的是
    // `Layers[0].Arn`（单数），于是 `update-function-configuration --layers` 只
    // 写回了一个层，**把 botocore-current:1 丢掉了**。
    //
    // 后果不是报错，而是 tests/test_75_no_phantom_gateway_targets 变红：
    // Lambda 自带的 botocore 版本不认识 bedrock-agentcore 的
    // targetConfiguration 这个 tagged union 的 `http` 成员，会**静默剥掉**它
    // （日志里只有一句 "Received a tagged union response with member unknown
    // to client: http"），于是 5 个 AGENTCORE_RUNTIME 类型的网关 target 被
    // 建成了 AgentTool 节点而不是 RoutesToRuntime 边。
    //
    // 所以它必须写进声明 —— 否则下一次 cdk deploy 会再丢一次，
    // 而症状是图上多了 5 个幻影节点，不是任何一次部署失败。
    const botocoreCurrentLayer = lambda.LayerVersion.fromLayerVersionArn(
      this, 'BotocoreCurrentLayer',
      `arn:aws:lambda:${this.region}:${this.account}:layer:botocore-current:1`,
    );

    const agentcoreEtlFn = new lambda.Function(this, 'NeptuneEtlFromAgentcore', {
      functionName: 'neptune-etl-from-agentcore',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'neptune_etl_agentcore.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/etl_agentcore'), {
        exclude: ['**/__pycache__/**', '**/*.pyc'],
      }),
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      role: lambdaRole,
      vpc,
      vpcSubnets,
      securityGroups: [lambdaSg],
      environment: {
        NEPTUNE_ENDPOINT: neptuneEndpoint,
        NEPTUNE_PORT: neptunePort,
        REGION: awsRegion,
        AGENTCORE_NUTRITION_KB_ID: agentcoreNutritionKbId,
      },
      layers: [neptuneClientLayer, botocoreCurrentLayer],
      description: 'ETL: Bedrock AgentCore runtimes/gateways -> Neptune (every 15 min)',
    });


    // =========================================================
    // 上面 3 个 ETL 的调度规则
    // =========================================================
    //
    // 这 3 条规则与函数本身一样，2026-09-06 是手工 `aws events put-rule`
    // 建的。2026-10-06 把函数 cdk import 进栈时它们进不来：CFN 的 IMPORT
    // changeset 只能导入、不能创建，而 targets.LambdaFunction 必然附带一个
    // AWS::Lambda::Permission —— CDK 把它标成 skipping，但它仍在模板里，
    // 于是 CFN 报 `Cannot invoke "String.split(String)" because "pid" is null`。
    //
    // 所以这里换了条路：**删掉手工规则，让 CDK 新建**。代价是一段约 2-3 分钟
    // 的调度空窗，而这几个 ETL 是按回溯窗口做幂等 upsert 的
    // （appsignals lookback=86400s、xray 24h），漏掉一个 tick 会被下一次运行
    // 完整补回；AccessesData 边的 expires_seconds 是 21600s，比空窗大两个数量级。
    // 另一条路（先 import 无 target 的规则、再 deploy 补 target）步骤更多，
    // 且中间有一段「栈认为规则没有 target」的**漂移**窗口 ——
    // 用漂移窗口换停机窗口，方向是错的。
    //
    // description 一律 ASCII。线上那 3 条手工规则的 description 里有中文和 `→`，
    // 而非 ASCII 在 CDK→CFN 路径上会被转成 `?`，造成永不收敛的脏 diff
    // （本文件 Lambda description 已为此全部改成 `->`，见上）。
    // 排期理由因此写在下面的注释里 —— 仓库本来也比 AWS 资源描述更适合存这个。

    // 1 小时一轮：X-Ray 拓扑按部署节奏变化而非按秒，回看窗口本就 24h，
    // 失效阈值 6h —— 每小时刷新留了 6 次余量。
    new events.Rule(this, 'XrayEtlSchedule', {
      ruleName: 'neptune-etl-xray-hourly',
      description: 'Trigger neptune-etl-from-xray hourly (lookback 24h, edge TTL 6h)',
      schedule: events.Schedule.rate(cdk.Duration.hours(1)),
      targets: [new targets.LambdaFunction(xrayEtlFn)],
    });

    // 15 分钟一次：比 Calls 边的 1800s TTL 勤，
    // 避免边被 graph_cleanup 误置 active=false。
    new events.Rule(this, 'AppsignalsEtlSchedule', {
      ruleName: 'neptune-etl-appsignals-every-15min',
      description: 'Trigger neptune-etl-from-appsignals every 15 minutes (Calls edge TTL 1800s)',
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      targets: [new targets.LambdaFunction(appsignalsEtlFn)],
    });

    // 15 分钟一次：span 回看 6h = 边 TTL 6h。
    new events.Rule(this, 'AgentcoreEtlSchedule', {
      ruleName: 'neptune-etl-agentcore-every-15min',
      description: 'Trigger neptune-etl-from-agentcore every 15 minutes (span lookback 6h = edge TTL 6h)',
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      targets: [new targets.LambdaFunction(agentcoreEtlFn)],
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
      description: 'Event-driven trigger: AWS infra change -> 30s delay -> neptune-etl-from-aws',
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

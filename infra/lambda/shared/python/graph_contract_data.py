"""图谱契约数据 —— 自动生成，请勿手工编辑。

来源：profiles/graph_contract.yaml
生成：python3 scripts/gen_graph_contract.py --write
校验：tests/test_35_graph_contract.py 断言本文件与来源一致（--check）

改契约请改 profiles/graph_contract.yaml 然后重新生成。
直接改本文件会在下一次 --check 时被测试打回。
"""

CONTRACT_VERSION = 1

TIMESTAMP_FIELD = 'last_seen'

# 历史遗留的时间戳字段名。读取侧要兼容，写入侧只写 TIMESTAMP_FIELD。
TIMESTAMP_LEGACY_ALIASES = ('last_updated', 'last_scanned')

SOURCES = frozenset(['agentcore-etl', 'appsignals-etl', 'aws-etl', 'aws-etl-static', 'business-layer', 'cfn-etl', 'deepflow-dns', 'deepflow-etl', 'deepflow-l4', 'eks-etl', 'manual-fix', 'nfm', 'xray'])

# 写一次属性：边上这些属性只由**首个发现者**写入，后续任何源都不得覆盖。
# source 记录的是谁首先发现了这条依赖 —— 被覆盖等于抹掉发现史。
EDGE_WRITE_ONCE_ATTRS = frozenset(['dependency_kind', 'first_seen', 'source'])

# 节点属性的权威来源。列表外的来源不得覆盖该属性。
NODE_ATTR_AUTHORITY = {   'Microservice': {   'az': ['aws-etl'],
                        'fault_boundary': ['aws-etl'],
                        'recovery_priority': ['aws-etl', 'business-layer']}}

NODE_TYPES = {   'AWSServiceEndpoint': {   'expires_seconds': 604800,
                              'identity': 'name',
                              'immutable': True,
                              'note': '归一化后的服务名（ssm 与 SimpleSystemsManagement 已归一）'},
    'AgentGateway': {   'expires_seconds': 604800,
                        'identity': 'arn',
                        'immutable': True,
                        'note': 'AgentCore Gateway。把 Lambda / OpenAPI / 外部 MCP server 等聚合成单一虚拟 '
                                'MCP server',
                        'writer': 'etl_agentcore'},
    'AgentMemory': {   'expires_seconds': 604800,
                       'identity': 'arn',
                       'immutable': True,
                       'note': 'AgentCore Memory。实测东京已有一个 ACTIVE '
                               '实例（xgg_memory-5M0VYBCeFS，2026-07-22 建）',
                       'writer': 'etl_agentcore'},
    'AgentRuntime': {   'expires_seconds': 604800,
                        'identity': 'arn',
                        'immutable': True,
                        'note': 'Bedrock AgentCore Runtime。arn 形如 '
                                'arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>',
                        'writer': 'etl_agentcore'},
    'AgentTool': {   'expires_seconds': 604800,
                     'identity': 'tool_key',
                     'immutable': True,
                     'note': 'tool **没有 ARN** —— 它是 agent 进程内注册的函数，或 Gateway 聚合出来的\n'
                             '虚拟 MCP 工具。所以身份键是复合键 `<owner_arn>#<tool_name>`：\n'
                             'owner 是 AgentGateway 的 arn（Gateway 前置的工具）或 AgentRuntime 的 arn\n'
                             '（进程内工具）。**不能只用 tool_name** —— 两个 agent 各注册一个同名\n'
                             'get_pet 会被并成一个节点，那正是 FIS chaos 模板里「tool 名精确匹配」\n'
                             '踩过的同一个坑（名字对不上就零注入而实验仍报成功）。\n',
                     'writer': 'etl_agentcore'},
    'AvailabilityZone': {   'expires_seconds': None,
                            'expiry_note': 'AWS 可用区不会消失',
                            'identity': 'name',
                            'immutable': True,
                            'note': 'az name，AWS 侧不可变'},
    'BusinessCapability': {   'expires_seconds': None,
                              'expiry_note': 'business_config.json 的声明，同 Microservice',
                              'identity': 'name',
                              'immutable': True,
                              'note': '业务声明，来自 business_config.json'},
    'ChaosExperiment': {   'expires_seconds': None,
                           'expiry_note': '追加式事件日志，实验记录是历史证据',
                           'identity': 'experiment_id',
                           'immutable': True,
                           'writer': 'chaos'},
    'Database': {   'expires_seconds': 604800,
                    'identity': 'name',
                    'immutable': True,
                    'note': '逻辑库名'},
    'Deployment': {   'expires_seconds': 604800,
                      'identity': 'name',
                      'immutable': 'lifetime',
                      'scope_note': '未按 namespace 限定'},
    'DynamoDBTable': {   'expires_seconds': 604800,
                         'identity': 'name',
                         'immutable': True,
                         'preferred': 'arn',
                         'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 '
                                                 'name 匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                                 'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 arn '
                                                 '会让两个 ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                                 'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 '
                                                 'ARN'},
    'EC2Instance': {   'expires_seconds': 604800,
                       'identity': 'instance_id',
                       'immutable': True,
                       'note': 'name 取自 Name 标签，已于 2026-08-29 改以 instance_id 为身份'},
    'ECRRepository': {   'expires_seconds': 604800,
                         'identity': 'arn',
                         'immutable': True,
                         'note': '2026-09-04 从 name 切到 arn。前提已由 tests/test_42::m08 对活图谱核验（12 '
                                 '个节点全带唯一 arn），且该类型**只有 etl_aws 写**（不在 etl_cfn 的 TYPE_TO_LABEL '
                                 '里），不存在两个 ETL 用不同身份键的风险'},
    'EKSCluster': {'expires_seconds': 604800, 'identity': 'name', 'immutable': True},
    'Guardrail': {   'expires_seconds': 604800,
                     'identity': 'arn',
                     'immutable': True,
                     'note': 'Bedrock Guardrail。不是 agent 专属 —— 故不加 Agent 前缀',
                     'writer': 'etl_agentcore'},
    'HPA': {   'expires_seconds': 604800,
               'identity': 'name',
               'immutable': 'lifetime',
               'scope_note': '未按 namespace 限定'},
    'Incident': {   'expires_seconds': None,
                    'expiry_note': '追加式事件日志，历史事件不该过期',
                    'identity': 'id',
                    'immutable': True,
                    'writer': 'rca_window_flush'},
    'K8sService': {   'expires_seconds': 604800,
                      'identity': 'name',
                      'immutable': 'lifetime',
                      'scope_note': '未按 namespace 限定'},
    'KnowledgeBase': {   'expires_seconds': 604800,
                         'identity': 'arn',
                         'immutable': True,
                         'note': 'Bedrock Knowledge Base。本环境向量库是 S3 Vectors（非 OpenSearch '
                                 'Serverless），已有 gp-incident-kb 范式',
                         'writer': 'etl_agentcore'},
    'LambdaFunction': {   'expires_seconds': 604800,
                          'identity': 'name',
                          'immutable': True,
                          'preferred': 'arn',
                          'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 '
                                                  'name 匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                                  'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 '
                                                  'arn 会让两个 ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                                  'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 '
                                                  'ARN'},
    'ListenerRule': {   'expires_seconds': 604800,
                        'identity': 'name',
                        'immutable': True,
                        'note': 'name 传入的就是 rule_arn（handler.py:273）'},
    'LoadBalancer': {   'expires_seconds': 604800,
                        'identity': 'name',
                        'immutable': True,
                        'note': 'ALB 创建后不可改名；dict 里已有 arn，可升级',
                        'preferred': 'arn',
                        'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 name '
                                                '匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                                'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 arn '
                                                '会让两个 ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                                'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 '
                                                'ARN'},
    'Microservice': {   'expires_seconds': None,
                        'expiry_note': 'service_mappings.json 的声明，不是观测值；删声明才该消失',
                        'identity': 'name',
                        'immutable': True,
                        'note': '规范服务名，来自 service_mappings.json，是声明而非观测值'},
    'Namespace': {   'expires_seconds': 604800,
                     'identity': 'name',
                     'immutable': True,
                     'scope_note': '未按 cluster 限定；多集群场景同名 namespace 会碰撞'},
    'NeptuneCluster': {'expires_seconds': 604800, 'identity': 'name', 'immutable': True},
    'NeptuneInstance': {'expires_seconds': 604800, 'identity': 'name', 'immutable': True},
    'Pod': {   'expires_seconds': 259200,
               'identity': 'name',
               'immutable': 'lifetime',
               'scope_note': '未按 namespace 限定；Pod 重建即换名，属预期'},
    'RDSCluster': {   'expires_seconds': 604800,
                      'identity': 'name',
                      'immutable': True,
                      'note': 'DBClusterIdentifier'},
    'RDSInstance': {   'expires_seconds': 604800,
                       'identity': 'name',
                       'immutable': True,
                       'note': 'DBInstanceIdentifier'},
    'Region': {   'expires_seconds': None,
                  'expiry_note': 'AWS 区域不会消失',
                  'identity': 'name',
                  'immutable': True,
                  'note': 'region code，AWS 侧不可变'},
    'S3Bucket': {   'expires_seconds': 604800,
                    'identity': 'name',
                    'immutable': True,
                    'note': '桶名全局唯一且不可变'},
    'SNSTopic': {   'expires_seconds': 604800,
                    'identity': 'name',
                    'immutable': True,
                    'preferred': 'arn',
                    'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 name '
                                            '匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                            'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 arn 会让两个 '
                                            'ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                            'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 ARN'},
    'SQSQueue': {   'expires_seconds': 604800,
                    'identity': 'name',
                    'immutable': True,
                    'preferred': 'arn',
                    'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 name '
                                            '匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                            'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 arn 会让两个 '
                                            'ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                            'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 ARN'},
    'SecurityGroup': {   'expires_seconds': 604800,
                         'identity': 'sg_id',
                         'immutable': True,
                         'note': 'GroupName 创建后不可改，但 sg_id 更稳且已在 dict 里'},
    'StepFunction': {   'expires_seconds': 604800,
                        'identity': 'name',
                        'immutable': True,
                        'preferred': 'arn',
                        'preferred_blocked_by': 'etl_cfn 也写该类型且 get_or_create_vertex 硬编码按 name '
                                                '匹配，且它手上只有被规范化成短名的 physical_id（Lambda 的 '
                                                'PhysicalResourceId 就是函数名，本地拿不到 ARN）。单方面切 arn '
                                                '会让两个 ETL 用不同身份键写同一类节点，图里必然裂成两份 —— 见 '
                                                'test_35::g13。解锁前提：先让 etl_cfn 也从契约派生身份键并取得 '
                                                'ARN'},
    'Subnet': {   'expires_seconds': 604800,
                  'identity': 'subnet_id',
                  'immutable': True,
                  'note': 'name 取自 Name 标签（collectors/ec2.py:56）是可变的，必须以 subnet_id 为身份'},
    'TargetGroup': {   'expires_seconds': 604800,
                       'identity': 'arn',
                       'immutable': True,
                       'note': '2026-09-04 从 name 切到 arn（14 个节点全带唯一 arn，且只有 etl_aws 写）。原 '
                               'note：TargetGroupName 在 AWS 侧创建后不可改，所以 name 是合法身份键。arn '
                               '更稳（跨账号/区域唯一）。2026-09-04 起**切换条件已满足**：活图谱 14 个节点全部带唯一 arn，由 '
                               'tests/test_42 的 m08 用例对活图谱自动核验（GRAPH_LIVE_AUDIT=true）。此前 note '
                               '写的「18 个节点全部没有 arn，待存量都带上 arn 后再切」有两处错——一是 2026-09-04 实况已是 '
                               '14/18 有 arn，note 过期半个月无人发现（`preferred` '
                               '当时没有任何读取方）；二是那个条件**不可满足**：剩下 4 个节点里 3 个（nginx-tg-1/2/3）在 AWS '
                               '侧已删除、1 个（openclaw-tg-v2）被 SKIP_TG_PREFIXES 刻意排除采集，ETL '
                               '永远不会再碰它们、永远补不上 arn。已用 infra/reap_stale_nodes.py 回收这 4 个残留节点'},
    'TopologyChange': {   'expires_seconds': None,
                          'expiry_note': '追加式事件日志，稳态可 0 实例',
                          'identity': 'change_id',
                          'immutable': True,
                          'note': '追加式事件日志，稳态可 0 实例',
                          'writer': 'etl_deepflow'},
    'VPC': {   'expires_seconds': 604800,
               'identity': 'vpc_id',
               'immutable': True,
               'note': "name 取自 Name 标签（collectors/ec2.py:76 tags.get('Name', "
                       "v['VpcId'])）是可变的，必须以 vpc_id 为身份"}}

EDGE_TYPES = {   'AccessesData': {   'dependency': True,
                        'dst': [   'AWSServiceEndpoint',
                                   'AgentMemory',
                                   'DynamoDBTable',
                                   'LambdaFunction',
                                   'NeptuneCluster',
                                   'RDSCluster',
                                   'RDSInstance',
                                   'S3Bucket',
                                   'StepFunction'],
                        'expires_seconds': 21600,
                        'note': '四个源都写（aws/cfn 静态，deepflow 动态，xray 补度量）；取最长源窗口 = xray 的 '
                                '6h，否则会误杀 xray 发现的边',
                        'pairs': [   ['AgentRuntime', 'AgentMemory'],
                                     ['AgentRuntime', 'AWSServiceEndpoint'],
                                     ['LambdaFunction', 'AWSServiceEndpoint'],
                                     ['LambdaFunction', 'DynamoDBTable'],
                                     ['LambdaFunction', 'LambdaFunction'],
                                     ['LambdaFunction', 'NeptuneCluster'],
                                     ['LambdaFunction', 'RDSCluster'],
                                     ['LambdaFunction', 'S3Bucket'],
                                     ['Microservice', 'AWSServiceEndpoint'],
                                     ['Microservice', 'DynamoDBTable'],
                                     ['Microservice', 'RDSCluster'],
                                     ['Microservice', 'RDSInstance'],
                                     ['Microservice', 'S3Bucket'],
                                     ['Microservice', 'StepFunction'],
                                     ['StepFunction', 'LambdaFunction']],
                        'src': [   'AgentRuntime',
                                   'LambdaFunction',
                                   'Microservice',
                                   'StepFunction']},
    'AffectedService': {   'dependency': False,
                           'dst': ['Microservice'],
                           'expires_seconds': None,
                           'pairs': [['Incident', 'Microservice']],
                           'src': ['Incident']},
    'BelongsTo': {   'dependency': False,
                     'dst': ['EKSCluster', 'K8sService', 'NeptuneCluster', 'RDSCluster'],
                     'expires_seconds': None,
                     'pairs': [   ['Database', 'RDSCluster'],
                                  ['EC2Instance', 'EKSCluster'],
                                  ['Namespace', 'EKSCluster'],
                                  ['NeptuneInstance', 'NeptuneCluster'],
                                  ['Pod', 'K8sService'],
                                  ['RDSInstance', 'RDSCluster']],
                     'src': [   'Database',
                                'EC2Instance',
                                'Namespace',
                                'NeptuneInstance',
                                'Pod',
                                'RDSInstance']},
    'Calls': {   'dependency': True,
                 'dst': ['LambdaFunction', 'Microservice'],
                 'expires_seconds': 1800,
                 'note': '唯一写入源是 deepflow，阈值同 CALLS_INACTIVE_AFTER_SECONDS',
                 'pairs': [   ['LambdaFunction', 'LambdaFunction'],
                              ['Microservice', 'Microservice']],
                 'retention_seconds': 604800,
                 'src': ['LambdaFunction', 'Microservice']},
    'ConnectsTo': {   'dependency': False,
                      'dst': ['Database'],
                      'expires_seconds': None,
                      'pairs': [['Microservice', 'Database']],
                      'src': ['Microservice']},
    'Contains': {   'dependency': False,
                    'dst': ['AvailabilityZone'],
                    'expires_seconds': None,
                    'pairs': [['Region', 'AvailabilityZone']],
                    'src': ['Region']},
    'Delegates': {   'dependency': True,
                     'dst': ['AgentRuntime'],
                     'expires_seconds': 21600,
                     'note': '编排 agent 路由到子 agent（本环境是 orchestrator_strands → '
                             'nutrition/ordering/\n'
                             'adoption/concierge 四个）。唯一写入源是 aws/spans —— 控制面 API 看不到\n'
                             '「谁调了谁」，那只存在于运行时 span 里。\n'
                             'TTL 取 21600（6h）与 AccessesData 一致：同为 span 派生，用同一个源窗口，\n'
                             '否则会出现「一条边失效了、同源的另一条还在」的不一致。\n'
                             '⚠️ 判据陷阱：tool 级/agent 级故障被优雅处理后**完成为成功调用**，对基础设施\n'
                             '错误指标不可见。验证这条边真实存在要看 agent 自己的信号，不是 HTTP 5xx。\n',
                     'pairs': [['AgentRuntime', 'AgentRuntime']],
                     'src': ['AgentRuntime'],
                     'transitive': True},
    'DependsOn': {   'dependency': True,
                     'dst': [   'AgentRuntime',
                                'ECRRepository',
                                'LambdaFunction',
                                'Microservice',
                                'RDSCluster',
                                'SNSTopic',
                                'SQSQueue'],
                     'expires_seconds': 21600,
                     'note': 'aws + cfn 静态 + deepflow 动态',
                     'pairs': [   ['AgentTool', 'LambdaFunction'],
                                  ['AgentTool', 'Microservice'],
                                  ['BusinessCapability', 'RDSCluster'],
                                  ['BusinessCapability', 'SNSTopic'],
                                  ['BusinessCapability', 'SQSQueue'],
                                  ['Microservice', 'AgentRuntime'],
                                  ['Microservice', 'ECRRepository'],
                                  ['Microservice', 'SQSQueue']],
                     'src': ['AgentTool', 'BusinessCapability', 'Microservice']},
    'ForwardsTo': {   'dependency': False,
                      'dst': ['Microservice', 'TargetGroup'],
                      'expires_seconds': None,
                      'pairs': [   ['ListenerRule', 'TargetGroup'],
                                   ['LoadBalancer', 'TargetGroup'],
                                   ['TargetGroup', 'Microservice']],
                      'src': ['ListenerRule', 'LoadBalancer', 'TargetGroup']},
    'HasRule': {   'dependency': False,
                   'dst': ['ListenerRule'],
                   'expires_seconds': None,
                   'pairs': [['LoadBalancer', 'ListenerRule']],
                   'src': ['LoadBalancer']},
    'HasSG': {   'dependency': False,
                 'dst': ['SecurityGroup'],
                 'expires_seconds': None,
                 'pairs': [   ['EC2Instance', 'SecurityGroup'],
                              ['EKSCluster', 'SecurityGroup'],
                              ['LoadBalancer', 'SecurityGroup'],
                              ['NeptuneCluster', 'SecurityGroup'],
                              ['RDSCluster', 'SecurityGroup']],
                 'src': [   'EC2Instance',
                            'EKSCluster',
                            'LoadBalancer',
                            'NeptuneCluster',
                            'RDSCluster']},
    'Implements': {   'dependency': False,
                      'dst': ['BusinessCapability', 'LambdaFunction', 'Microservice'],
                      'expires_seconds': None,
                      'pairs': [   ['K8sService', 'LambdaFunction'],
                                   ['K8sService', 'Microservice'],
                                   ['LambdaFunction', 'BusinessCapability'],
                                   ['Microservice', 'BusinessCapability']],
                      'src': ['K8sService', 'LambdaFunction', 'Microservice']},
    'Invokes': {   'dependency': True,
                   'dst': ['LambdaFunction'],
                   'expires_seconds': 21600,
                   'pairs': [   ['LambdaFunction', 'LambdaFunction'],
                                ['SNSTopic', 'LambdaFunction'],
                                ['StepFunction', 'LambdaFunction']],
                   'src': ['LambdaFunction', 'SNSTopic', 'StepFunction']},
    'InvokesTool': {   'dependency': True,
                       'dst': ['AgentTool'],
                       'expires_seconds': 21600,
                       'note': 'agent → tool 的调用。**刻意不复用现有 Invokes**。\n'
                               '2026-09-05 更新：此前这里的理由是「Invokes 那个 dependency: false」，\n'
                               '但同日 Invokes 已改判为 dependency: true（StepFunction/SNS → '
                               'LambdaFunction\n'
                               '按任何定义都是真依赖），**那条理由已失效**。不复用的理由改为下面两条，\n'
                               '两条都仍然成立：\n'
                               '(1) 端点类型不同：Invokes 的目标是 LambdaFunction，本边是 AgentTool；\n'
                               '(2) **失效语义不同**：本边的 dependency_kind 是 inference（LLM 按 query\n'
                               '在运行时决定），由 mark_stale_inference_edges 标记观测静默、**不置\n'
                               'active=false**；而 Invokes 是 static（配置声明），走的是另一条失效路径。\n'
                               '把两者塞进一个标签，会让「按 dependency_kind 过滤失效」的清理逻辑\n'
                               '对两种本质不同的边施加同一套判据 —— 实测代价见 graph_cleanup 里\n'
                               'Retrieves → nutrition-kb 被误置 active=false 那段。\n',
                       'pairs': [['AgentRuntime', 'AgentTool']],
                       'src': ['AgentRuntime']},
    'InvokesVia': {   'dependency': False,
                      'dst': ['StepFunction'],
                      'expires_seconds': 21600,
                      'note': '受 drift 对账覆盖',
                      'pairs': [['Microservice', 'StepFunction']],
                      'src': ['Microservice']},
    'Involves': {   'dependency': False,
                    'dst': ['Microservice'],
                    'expires_seconds': None,
                    'pairs': [['Incident', 'Microservice']],
                    'src': ['Incident']},
    'LocatedIn': {   'dependency': False,
                     'dst': ['AvailabilityZone', 'EKSCluster', 'Region', 'Subnet', 'VPC'],
                     'expires_seconds': None,
                     'pairs': [   ['AWSServiceEndpoint', 'Region'],
                                  ['EC2Instance', 'AvailabilityZone'],
                                  ['EC2Instance', 'Subnet'],
                                  ['ECRRepository', 'Region'],
                                  ['EKSCluster', 'Region'],
                                  ['LoadBalancer', 'AvailabilityZone'],
                                  ['Microservice', 'EKSCluster'],
                                  ['NeptuneCluster', 'Region'],
                                  ['NeptuneInstance', 'AvailabilityZone'],
                                  ['Pod', 'AvailabilityZone'],
                                  ['RDSCluster', 'Region'],
                                  ['RDSInstance', 'AvailabilityZone'],
                                  ['S3Bucket', 'Region'],
                                  ['SNSTopic', 'Region'],
                                  ['SQSQueue', 'Region'],
                                  ['Subnet', 'AvailabilityZone'],
                                  ['Subnet', 'VPC'],
                                  ['VPC', 'Region']],
                     'src': [   'AWSServiceEndpoint',
                                'EC2Instance',
                                'ECRRepository',
                                'EKSCluster',
                                'LoadBalancer',
                                'Microservice',
                                'NeptuneCluster',
                                'NeptuneInstance',
                                'Pod',
                                'RDSCluster',
                                'RDSInstance',
                                'S3Bucket',
                                'SNSTopic',
                                'SQSQueue',
                                'Subnet',
                                'VPC']},
    'Manages': {   'dependency': False,
                   'dst': ['Deployment', 'Microservice', 'Pod'],
                   'expires_seconds': None,
                   'pairs': [   ['Deployment', 'Microservice'],
                                ['Deployment', 'Pod'],
                                ['HPA', 'Deployment']],
                   'src': ['Deployment', 'HPA']},
    'MentionsResource': {   'dependency': False,
                            'dst': ['EC2Instance', 'Microservice'],
                            'expires_seconds': None,
                            'pairs': [   ['Incident', 'EC2Instance'],
                                         ['Incident', 'Microservice']],
                            'src': ['Incident']},
    'OwnedBy': {   'dependency': False,
                   'dst': ['Namespace'],
                   'expires_seconds': None,
                   'pairs': [['Microservice', 'Namespace']],
                   'src': ['Microservice']},
    'ProtectsAccess': {   'dependency': False,
                          'dst': ['AgentRuntime', 'LambdaFunction'],
                          'expires_seconds': None,
                          'pairs': [   ['Guardrail', 'AgentRuntime'],
                                       ['SecurityGroup', 'LambdaFunction']],
                          'src': ['Guardrail', 'SecurityGroup']},
    'PublishesTo': {   'dependency': True,
                       'dst': ['SNSTopic', 'SQSQueue'],
                       'expires_seconds': 21600,
                       'note': '受 drift 对账覆盖。\n'
                               '2026-09-08 从 dependency: false 翻为 true。理由是原状态存在一处无正当理由的 '
                               '不对称：etl_xray 的 RESOURCE_NODE_TO_EDGE 把 SQSQueue 映射成 DependsOn '
                               '（算依赖边）、把 SNSTopic 映射成 PublishesTo（不算）—— 同一个源、同一种 语义关系（服务使用 AWS '
                               '托管消息服务），两种边类型、两种待遇。\n'
                               '「发布到 topic」是实打实的服务消费：SNS 不可用则发布失败，与调用一个 下游服务失败没有区别。它也不像 '
                               'LocatedIn/RunsOn 那样普遍为真 （999/792 条，对所有资源都成立、因而不携带判别信息），实测只有 2 '
                               '条， 判别力强。\n'
                               '系统内另有两处早已按依赖对待它，翻转是**消除分歧而非引入新意见**： · '
                               'rca_window_flush/neptune/schema_prompt.py:135 的依赖遍历包含它 · '
                               'etl_deepflow:810 的 drift 对账把它与 AccessesData 并列\n'
                               '合规视角上这一步是必需的：SNS/SQS 是 DORA 要登记的第三方 ICT 服务， '
                               '而「服务支撑哪些业务功能」那一层要靠依赖边穿透才能算出来。翻转前 SNSTopic 追溯到业务功能的结果是 0 个。\n'
                               '刻意**不同时翻 InvokesVia**：它在整个 infra/ 里没有任何写入方 （历史只有 cffbe45 '
                               '引入契约那一个提交），翻成依赖边只会造出一条 没人维护、立刻被 TTL 判陈旧的墓碑边。该边类型是留还是删是另一个问题。',
                       'pairs': [['Microservice', 'SNSTopic'], ['Microservice', 'SQSQueue']],
                       'src': ['Microservice']},
    'Retrieves': {   'dependency': True,
                     'dst': ['KnowledgeBase'],
                     'expires_seconds': 21600,
                     'note': 'agent → Knowledge Base 的检索（本环境是 nutrition agent 查 10 篇宠物营养\n'
                             '知识文档）。是依赖边：KB 检索失败会让 agent 的回答质量下降。\n'
                             '⚠️ 这条边的失效模式与微服务不同 —— KB 返回空结果时调用**是成功的**，\n'
                             '表现为输出质量下降而非错误率上升。所以它的证伪判据要看\n'
                             'response-quality / task-success，不是 infrastructure error rate。\n',
                     'pairs': [['AgentRuntime', 'KnowledgeBase']],
                     'src': ['AgentRuntime']},
    'Routes': {   'dependency': False,
                  'dst': ['Pod'],
                  'expires_seconds': None,
                  'pairs': [['K8sService', 'Pod']],
                  'src': ['K8sService']},
    'RoutesTo': {   'dependency': False,
                    'dst': ['AgentTool', 'TargetGroup'],
                    'expires_seconds': None,
                    'note': '⚠️ **迁移期状态（2026-09-07）**：本标签正在被拆分。\n'
                            '[AgentGateway, AgentTool] 这一对是**错的**，实测（bedrock-agentcore-control '
                            '的 GetGatewayTarget）证明网关 target 的 targetType 全是 AGENTCORE_RUNTIME、 '
                            '配置指向 runtime ARN —— 目标应是 AgentRuntime，且那是**真依赖**， 已另立 '
                            'RoutesToRuntime。\n'
                            '**这一对暂时保留不能删**：删了之后 assert_edge_type 会在生产里拒写并抛错， 因为已部署的 '
                            'etl_agentcore 还在写这种边。删除必须排在 「ETL 改完 + 部署 + 存量清理」之后，顺序见 '
                            'todo/agentobv/agent-layer-taxonomy-design_20260905-1640.md §4。\n'
                            '保留的那一对 [LoadBalancer, TargetGroup] 是纯转发配置，dependency: false 正确。',
                    'pairs': [['AgentGateway', 'AgentTool'], ['LoadBalancer', 'TargetGroup']],
                    'src': ['AgentGateway', 'LoadBalancer']},
    'RoutesToRuntime': {   'dependency': True,
                           'dst': ['AgentRuntime'],
                           'expires_seconds': 21600,
                           'note': '网关到 agent runtime 的路由。真值来源是控制面 ListGatewayTargets + '
                                   'GetGatewayTarget（targetConfiguration.http.agentcoreRuntime.arn）。\n'
                                   '**刻意不复用 RoutesTo**：那个标签同时用于 LoadBalancer → TargetGroup 的转发 '
                                   '配置且 dependency: false。本边是真依赖 —— runtime 不可用时网关这条路由就是 断的。而 '
                                   'dependency 是**边类型级**的标志，一个标签无法同时取两个值 —— 这正是 2026-09-05 那 5 '
                                   '条边越界携带 dependency_kind 的根因。\n'
                                   'target 的元信息（target_id / target_name / target_type / '
                                   'credential_provider / target_status）作为**边属性**承载， **不**单独建 '
                                   'AgentGatewayTarget 节点 —— 那一层没有独立失效语义， '
                                   '两端都已是节点，插一层只会让所有影响面查询多一跳。\n'
                                   'dependency_kind 取 static：控制面声明，与流量无关。',
                           'pairs': [['AgentGateway', 'AgentRuntime']],
                           'src': ['AgentGateway']},
    'RoutesVia': {   'dependency': True,
                     'dst': ['AgentGateway'],
                     'expires_seconds': 21600,
                     'note': 'agent 的出站调用经由网关路由。**这条边补的是一个已确认的活体单点故障**。\n'
                             '实测依据（2026-09-07，orchestrator 的运行时日志组）： '
                             'scope=opentelemetry.instrumentation.httpx / name=POST / '
                             'kind=CLIENT 的 span， attributes.aws.remote.service 指向网关主机名， '
                             'attributes.aws.remote.operation 形如 "POST /adoption" 直接给出 target '
                             '名。 窗口 09-04→09-07 四个子 agent 全部出现（adoption 145 / nutrition 61 / '
                             'ordering 42 / concierge 17 次），全部 HTTP 200。\n'
                             '**为什么必须单独建这条边**：网关承载全部 agent 间流量，它失效 ⇒ orchestrator 到 4 个子 agent '
                             '全断 ⇒ 多 agent 系统整体失效。 在这条边存在之前，图对此完全沉默，影响面分析会给出偏乐观的错误答案。\n'
                             '**为什么不复用 DependsOn**：本边断裂会让多 agent 协作整体瓦解， 而单 agent '
                             '直调仍可用；两者失效影响面不同，合成一个标签就无法分别推导。\n'
                             'dependency_kind 取 dynamic（**不是 static**）：已实测到持续流量 （跨 4 天、约每 5 '
                             '分钟、无中断），按本项目定义 dynamic = 持续观测到流量。 这也让它落入 '
                             'deactivate_stale_dynamic_edges 的失效管辖，而非 inference '
                             '那条「稀疏突发、不判失效」通道 —— 对稳态流量是正确的选择。',
                     'pairs': [['AgentRuntime', 'AgentGateway']],
                     'src': ['AgentRuntime']},
    'RunsOn': {   'dependency': False,
                  'dst': ['EC2Instance', 'Pod'],
                  'expires_seconds': None,
                  'pairs': [['Microservice', 'Pod'], ['Pod', 'EC2Instance']],
                  'src': ['Microservice', 'Pod']},
    'TestedBy': {   'dependency': False,
                    'dst': ['ChaosExperiment'],
                    'expires_seconds': None,
                    'pairs': [['Microservice', 'ChaosExperiment']],
                    'src': ['Microservice']},
    'TriggeredBy': {   'dependency': False,
                       'dst': ['LambdaFunction', 'Microservice'],
                       'expires_seconds': None,
                       'pairs': [['Incident', 'Microservice'], ['SQSQueue', 'LambdaFunction']],
                       'src': ['Incident', 'SQSQueue']},
    'WritesTo': {   'dependency': False,
                    'dst': ['NeptuneCluster', 'S3Bucket', 'SNSTopic', 'SQSQueue'],
                    'expires_seconds': None,
                    'pairs': [   ['LambdaFunction', 'NeptuneCluster'],
                                 ['Microservice', 'S3Bucket'],
                                 ['Microservice', 'SNSTopic'],
                                 ['Microservice', 'SQSQueue']],
                    'src': ['LambdaFunction', 'Microservice']}}


# 依赖边验证与置信度的判据。混沌注入与 ETL 共用同一份声明 ——
# 否则「谁能写 verify_* 属性」「confirmed 的阈值」会各处一份、悄悄漂移。
EDGE_VERIFICATION = {   'attrs': [   'verify_status',
                 'verify_confidence',
                 'verify_last',
                 'verify_by',
                 'verify_experiment',
                 'verify_degradation'],
    'authority': ['chaos-runner'],
    'evidence_weights': {   'intervention_confirmed': 4.0,
                            'intervention_refuted': -4.0,
                            'observed_cap': 1.5,
                            'observed_per_source': 0.5,
                            'static_declaration': 1.0},
    'statuses': ['untested', 'confirmed', 'refuted', 'inconclusive'],
    'thresholds': {   'confirm_degradation_pct': 20.0,
                      'hard_degradation_pct': 70.0,
                      'min_observation_requests': 20,
                      'refute_degradation_pct': 5.0,
                      'stale_verification_seconds': 2592000}}


# 节点 scope：算不算「被观测系统」的一部分（第五个正交维度）。
# 判据是权威归属而非名字模式 —— AWS 资源走 CloudFormation 栈归属
# （ParentId 非空即嵌套栈 ⇒ scaffolding），K8s 对象走 namespace。
# 靶点选择 / 爆炸半径 / DR 计划只该看 primary_query_scope。
NODE_SCOPE = {   'attr': 'scope',
    'authority': ['scope-labeler'],
    'name_prefix_map': {'neptune-etl-from-': 'platform', 'neptune-etl-trigger': 'platform'},
    'namespace_map': {   'amazon-cloudwatch': 'observability',
                         'amazon-guardduty': 'observability',
                         'amazon-network-flow-monitor': 'observability',
                         'awesomeshop': 'observed',
                         'chaos-mesh': 'platform',
                         'deepflow': 'observability',
                         'kube-system': 'cluster-infra',
                         'petadoptions': 'observed'},
    'nested_stack_scope': 'scaffolding',
    'primary_query_scope': 'observed',
    'resolvers': [   'k8s-namespace',
                     'cloudformation-stack-membership',
                     'node-type',
                     'profile-declaration'],
    'stack_map': {   'AlertBufferStack': 'platform',
                     'Applications': 'observed',
                     'AwesomeShopInfra': 'observed',
                     'CDKToolkit': 'scaffolding',
                     'NeptuneEtlStack': 'platform',
                     'ServicesEks2': 'observed',
                     'WaggleAIAgents': 'observed'},
    'type_map': {   'AWSServiceEndpoint': 'external',
                    'AvailabilityZone': 'external',
                    'BusinessCapability': 'observed',
                    'ChaosExperiment': 'platform',
                    'Incident': 'platform',
                    'NeptuneCluster': 'platform',
                    'NeptuneInstance': 'platform',
                    'Region': 'external',
                    'SaaSEndpoint': 'external',
                    'TopologyChange': 'platform'},
    'unresolved_value': 'unknown',
    'values': [   'observed',
                  'observability',
                  'platform',
                  'scaffolding',
                  'cluster-infra',
                  'external',
                  'unknown'],
    'workload_map': {   'aws-otel-collector': 'observability',
                        'cloudwatch-agent': 'observability',
                        'fluent-bit': 'observability',
                        'otel-collector': 'observability',
                        'xray-daemon': 'observability'}}


# 观测节奏稀疏的 source：「窗口内没看到」推不出「依赖消失」。
# 这些源的边只能标 drift_status='observed_then_silent'，
# **不得**走 active=false —— 后者断言依赖不存在，而我们只能证明
# 窗口内没观测到。判据与理由见 profiles/graph_contract.yaml。
SPARSE_OBSERVATION_SOURCES = ['deepflow-dns']

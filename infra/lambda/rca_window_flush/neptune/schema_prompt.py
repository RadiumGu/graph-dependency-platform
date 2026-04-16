"""
schema_prompt.py - Neptune 图 schema 描述，供 NL→openCypher 使用。

Schema 基于 Neptune 实际数据自动对齐（2026-04-16）。
图 schema 变化时更新此文件即可。
"""

GRAPH_SCHEMA = """
## 节点类型（27 种）

### 基础设施层
- Region: name(str), region_name(str)
- AvailabilityZone: name(str), az_name(str)
- VPC: name(str), vpc_id(str), cidr(str)
- Subnet: name(str), subnet_id(str), az(str)
- SecurityGroup: name(str), sg_id(str), description(str)

### 计算层
- EC2Instance: instance_id(str), name(str), state(str), az(str), health_status(str), instance_type(str), private_ip(str)
- EKSCluster: name(str), version(str)
- Pod: name(str), status(str), node_name(str), namespace(str), ip(str), restarts(int)
- Microservice: name(str), recovery_priority(Tier0|Tier1|Tier2), fault_boundary(str), az(str), replica_count(int), role(str), port(int)

### 网络层
- LoadBalancer: name(str), dns(str), lb_type(str), scheme(str)
- TargetGroup: name(str), arn(str)
- ListenerRule: name(str), listener_arn(str), priority(int)

### 数据层
- RDSCluster: name(str), engine(str), status(str), endpoint(str), reader_endpoint(str)
- RDSInstance: name(str), instance_class(str), engine(str), status(str)
- Database: name(str)  // 逻辑数据库（BelongsTo RDSCluster）
- DynamoDBTable: name(str)
- NeptuneCluster: name(str), engine(str), endpoint(str)
- NeptuneInstance: name(str)
- S3Bucket: name(str)

### 消息层
- SQSQueue: name(str), is_dlq(bool)
- SNSTopic: name(str), subscriptions_confirmed(int)

### 计算（Serverless）
- LambdaFunction: name(str), runtime(str), memory_size_mb(int), concurrent_executions(int)
- StepFunction: name(str), arn(str)

### 容器镜像
- ECRRepository: name(str), uri(str)

### 业务层
- BusinessCapability: name(str), recovery_priority(str), layer(str)

### 运维层
- Incident: id(str), severity(P0|P1|P2), root_cause(str), resolution(str), start_time(str), status(str), affected_service(str)
- ChaosExperiment: experiment_id(str), fault_type(str), result(passed|failed), recovery_time_sec(int), degradation_rate(float), timestamp(str)

## 边类型（21 种）

### 拓扑/位置关系
- (:Region)-[:Contains]->(:AvailabilityZone)
- (:VPC)-[:LocatedIn]->(:Region)
- (:Subnet)-[:LocatedIn]->(:VPC)
- (:Subnet)-[:LocatedIn]->(:AvailabilityZone)
- (:EC2Instance)-[:LocatedIn]->(:Subnet|:AvailabilityZone)
- (:Pod)-[:LocatedIn]->(:AvailabilityZone)
- (:RDSCluster)-[:LocatedIn]->(:Region)
- (:RDSInstance)-[:LocatedIn]->(:AvailabilityZone)
- (:NeptuneCluster)-[:LocatedIn]->(:Region)
- (:NeptuneInstance)-[:LocatedIn]->(:AvailabilityZone)
- (:S3Bucket|:SNSTopic|:SQSQueue|:ECRRepository|:EKSCluster)-[:LocatedIn]->(:Region)
- (:LoadBalancer)-[:LocatedIn]->(:AvailabilityZone)
- (:Microservice)-[:LocatedIn]->(:EKSCluster)

### 归属关系
- (:EC2Instance)-[:BelongsTo]->(:EKSCluster)
- (:RDSInstance)-[:BelongsTo]->(:RDSCluster)
- (:NeptuneInstance)-[:BelongsTo]->(:NeptuneCluster)
- (:Database)-[:BelongsTo]->(:RDSCluster)

### 安全组关系
- (:EC2Instance|:RDSCluster|:NeptuneCluster|:LoadBalancer|:EKSCluster)-[:HasSG]->(:SecurityGroup)
- (:SecurityGroup)-[:ProtectsAccess]->(:LambdaFunction)

### 运行关系
- (:Microservice)-[:RunsOn]->(:Pod)
- (:Pod)-[:RunsOn]->(:EC2Instance)

### 网络/路由关系
- (:LoadBalancer)-[:HasRule]->(:ListenerRule)
- (:ListenerRule)-[:ForwardsTo]->(:TargetGroup)
- (:LoadBalancer)-[:ForwardsTo|:RoutesTo]->(:TargetGroup)
- (:TargetGroup)-[:ForwardsTo]->(:Microservice)

### 调用关系（运行时，来自 DeepFlow 或 X-Ray）
- (:Microservice)-[:Calls]->(:Microservice)

### 数据访问关系（⚠️ 注意：微服务到数据库用 AccessesData，不是 DependsOn）
- (:Microservice)-[:AccessesData]->(:RDSCluster|:DynamoDBTable|:S3Bucket|:StepFunction)
- (:LambdaFunction)-[:AccessesData]->(:DynamoDBTable|:RDSCluster|:LambdaFunction)
- (:Microservice)-[:ConnectsTo]->(:Database)

### 依赖关系（DependsOn 用于镜像依赖和队列依赖，不用于数据库）
- (:Microservice)-[:DependsOn]->(:ECRRepository|:SQSQueue)
- (:BusinessCapability)-[:DependsOn]->(:RDSCluster|:SNSTopic|:SQSQueue)

### 消息/事件关系
- (:Microservice)-[:PublishesTo]->(:SNSTopic|:SQSQueue)
- (:Microservice)-[:WritesTo]->(:SQS|:SNS|:S3)
- (:LambdaFunction)-[:WritesTo]->(:NeptuneCluster)
- (:Microservice)-[:InvokesVia]->(:StepFunction)
- (:SNSTopic)-[:Invokes]->(:LambdaFunction)
- (:LambdaFunction)-[:Invokes]->(:LambdaFunction)
- (:StepFunction)-[:Invokes]->(:LambdaFunction)
- (:SQSQueue)-[:TriggeredBy]->(:LambdaFunction)

### 业务关系
- (:Microservice)-[:Implements]->(:BusinessCapability)
- (:LambdaFunction)-[:Implements]->(:BusinessCapability)

### 运维关系
- (:Incident)-[:TriggeredBy]->(:Microservice)
- (:Incident)-[:AffectedService]->(:Microservice)
- (:Microservice)-[:TestedBy]->(:ChaosExperiment)

## 已知服务名（PetSite 应用）
petsite, petsearch, payforadoption, petlistadoptions, pethistory, petstatusupdater, trafficgenerator, artillery, artillery-write, auth-service, gateway-service, order-service, points-service, product-service
"""

FEW_SHOT_EXAMPLES = [
    {
        "q": "petsite 依赖哪些数据库？",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:AccessesData]->(db) WHERE db:RDSCluster OR db:DynamoDBTable RETURN db.name AS database, labels(db)[0] AS type",
    },
    {
        "q": "petsite 的所有下游依赖有哪些？",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:Calls|AccessesData|PublishesTo|InvokesVia|DependsOn]->(d) RETURN d.name AS dependency, labels(d)[0] AS type, type(r) AS relation",
    },
    {
        "q": "AZ ap-northeast-1a 有哪些服务？",
        "cypher": "MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:RunsOn]->(e:EC2Instance)-[:LocatedIn]->(az:AvailabilityZone {name:'ap-northeast-1a'}) RETURN DISTINCT s.name AS service, count(p) AS pod_count",
    },
    {
        "q": "payforadoption 的上游调用者有哪些？",
        "cypher": "MATCH (caller)-[:Calls]->(s:Microservice {name:'payforadoption'}) RETURN caller.name AS caller, labels(caller)[0] AS type",
    },
    {
        "q": "上个月 petsite 发生过几次故障？",
        "cypher": "MATCH (inc:Incident)-[:TriggeredBy]->(s:Microservice {name:'petsite'}) WHERE inc.start_time >= $since RETURN inc.id AS incident, inc.severity AS severity, inc.root_cause AS root_cause ORDER BY inc.start_time DESC",
    },
    {
        "q": "哪些服务从未做过混沌实验？",
        "cypher": "MATCH (s:Microservice) WHERE NOT (s)-[:TestedBy]->(:ChaosExperiment) RETURN s.name AS service, s.recovery_priority AS priority ORDER BY CASE s.recovery_priority WHEN 'Tier0' THEN 0 WHEN 'Tier1' THEN 1 ELSE 2 END",
    },
    {
        "q": "petsite 完整的基础设施路径（到 AZ）",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:RunsOn]->(p:Pod)-[:RunsOn]->(e:EC2Instance)-[:LocatedIn]->(az:AvailabilityZone) RETURN p.name AS pod, p.status AS pod_status, e.instance_id AS ec2, e.state AS ec2_state, az.name AS az",
    },
    {
        "q": "所有 P0 故障及其根因",
        "cypher": "MATCH (inc:Incident) WHERE inc.severity = 'P0' RETURN inc.id AS incident, inc.root_cause AS root_cause, inc.affected_service AS service, inc.start_time AS start_time ORDER BY inc.start_time DESC LIMIT 20",
    },
    {
        "q": "哪些 RDS 集群有安全组关联？",
        "cypher": "MATCH (r:RDSCluster)-[:HasSG]->(sg:SecurityGroup) RETURN r.name AS cluster, sg.name AS security_group, sg.sg_id AS sg_id",
    },
    {
        "q": "petsite 用了哪些 ECR 镜像？",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(ecr:ECRRepository) RETURN ecr.name AS repository, ecr.uri AS uri",
    },
]


def build_system_prompt() -> str:
    """构建 NL→openCypher 的 system prompt。

    Returns:
        包含 schema、规则和 few-shot 示例的完整 system prompt 字符串
    """
    examples = "\n".join(
        [f"Q: {ex['q']}\nCypher: {ex['cypher']}" for ex in FEW_SHOT_EXAMPLES]
    )

    return f"""你是一个 Amazon Neptune openCypher 查询生成器。根据用户的自然语言问题，生成正确的 openCypher 查询。

{GRAPH_SCHEMA}

## 规则
1. 只生成 READ 查询（MATCH/OPTIONAL MATCH/RETURN/WHERE/ORDER BY/LIMIT），绝对禁止 CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL
2. 参数化查询用 $param 语法
3. RETURN 必须带有意义的别名（AS ...）
4. 多跳遍历限制 *1..5，避免路径爆炸
5. 不确定的查询加 LIMIT 50 兜底
6. 只输出 openCypher 查询语句，不要解释，不要加 markdown 代码块
7. ⚠️ 微服务访问数据库（RDS、DynamoDB、S3）用 AccessesData 关系，不是 DependsOn
8. ⚠️ DependsOn 仅用于 ECR 镜像依赖和 SQS 队列依赖

## 示例
{examples}

现在根据用户问题生成查询："""

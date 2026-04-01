"""
schema_prompt.py - Neptune 图 schema 描述，供 NL→openCypher 使用。

Schema 是静态的（22 种节点、19 种边），直接硬编码在此文件中。
图 schema 变化时更新此文件即可。
"""

GRAPH_SCHEMA = """
## 节点类型（22 种）

### 基础设施层
- Region: name(str)
- AvailabilityZone: name(str)  // 例: "ap-northeast-1a"
- VPC: id(str), name(str), cidr(str)
- Subnet: id(str), name(str), az(str)
- SecurityGroup: id(str), name(str)

### 计算层
- EC2Instance: instance_id(str), name(str), state(str), az(str), health_status(str), instance_type(str)
- EKSCluster: name(str), version(str)
- Pod: name(str), status(str), node_name(str), namespace(str)
- Microservice: name(str), recovery_priority(Tier0|Tier1|Tier2), fault_boundary(str), az(str), replicas(int), tier(str), description(str)

### 网络层
- ALB: name(str), dns_name(str)
- TargetGroup: name(str), arn(str)

### 数据层
- RDSCluster: name(str), engine(str), status(str)
- DynamoDB: name(str)
- Neptune: name(str)
- S3: name(str)

### 消息层
- SQS: name(str), queue_url(str)
- SNS: name(str), topic_arn(str)

### 计算（Serverless）
- Lambda: name(str), runtime(str)
- StepFunction: name(str), arn(str)

### 业务层
- BusinessCapability: name(str), recovery_priority(str)

### 运维层
- Incident: id(str), severity(P0|P1|P2), root_cause(str), resolution(str), start_time(str), status(str), affected_service(str)
- ChaosExperiment: experiment_id(str), fault_type(str), result(passed|failed), recovery_time_sec(int), degradation_rate(float), timestamp(str)

## 边类型（19 种）

### 拓扑关系
- (:EC2Instance)-[:LocatedIn]->(:AvailabilityZone)
- (:Subnet)-[:BelongsTo]->(:VPC)
- (:EC2Instance)-[:BelongsTo]->(:EKSCluster)

### 运行关系
- (:Microservice)-[:RunsOn]->(:Pod)
- (:Pod)-[:RunsOn]->(:EC2Instance)

### 网络关系
- (:ALB)-[:RoutesTo]->(:TargetGroup)
- (:TargetGroup)-[:ForwardsTo]->(:Microservice)
- (:SecurityGroup)-[:HasRule]->()

### 调用/依赖关系
- (:Microservice)-[:Calls]->(:Microservice)                     // 运行时调用（来自 DeepFlow）
- (:Microservice)-[:DependsOn]->(:DynamoDB|:RDSCluster|:SQS|:S3|:Lambda)  // 数据/服务依赖
- (:Lambda)-[:Invokes]->(:Lambda|:StepFunction)
- (:Microservice)-[:WritesTo]->(:SQS|:SNS|:S3)
- (:Microservice)-[:PublishesTo]->(:SNS)

### 业务关系
- (:BusinessCapability)-[:Serves]->(:Microservice)
- (:Microservice)-[:Implements]->(:BusinessCapability)

### 运维关系
- (:Incident)-[:TriggeredBy]->(:Microservice)
- (:Incident)-[:MentionsResource]->(any)    // Phase A 新增
- (:Microservice)-[:TestedBy]->(:ChaosExperiment)  // Phase A 新增

## 已知服务名（PetSite 应用）
petsite, petsearch, payforadoption, petlistadoptions, petadoptionshistory, petfood, trafficgenerator
"""

FEW_SHOT_EXAMPLES = [
    {
        "q": "petsite 依赖哪些数据库？",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(db) WHERE db:RDSCluster OR db:DynamoDB RETURN db.name AS database, labels(db)[0] AS type",
    },
    {
        "q": "AZ ap-northeast-1a 有哪些 Tier0 服务？",
        "cypher": "MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:RunsOn]->(e:EC2Instance)-[:LocatedIn]->(az:AvailabilityZone {name:'ap-northeast-1a'}) WHERE s.recovery_priority = 'Tier0' RETURN DISTINCT s.name AS service, count(p) AS pod_count",
    },
    {
        "q": "payforadoption 的上游调用者有哪些？",
        "cypher": "MATCH (caller)-[:Calls]->(s:Microservice {name:'payforadoption'}) RETURN caller.name AS caller, labels(caller)[0] AS type",
    },
    {
        "q": "上个月 petsite 发生过几次故障？",
        "cypher": "MATCH (inc:Incident)-[:TriggeredBy]->(s:Microservice {name:'petsite'}) WHERE inc.start_time >= '2026-03-01' RETURN inc.id AS incident, inc.severity AS severity, inc.root_cause AS root_cause ORDER BY inc.start_time DESC",
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
        "q": "petfood 的下游依赖有哪些？",
        "cypher": "MATCH (s:Microservice {name:'petfood'})-[:Calls|DependsOn]->(d) RETURN d.name AS dependency, labels(d)[0] AS type",
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

## 示例
{examples}

现在根据用户问题生成查询："""

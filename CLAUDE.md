# CLAUDE.md — Graph Dependency Platform 改进任务指引

> **给 Claude Code 的工作指引。编码前必读。**

## 项目概述

在 `/home/ubuntu/tech/graph-dependency-platform`（简称 **GP**）上实施两个改进：
- **Phase A**: 非结构化数据整合 — 把 RCA incident 报告、混沌实验记录跟 Neptune 图节点打通
- **Phase B**: 自然语言图查询 — NL→openCypher 中间层

**参考实现**在 `/home/ubuntu/tech/sample-semantic-layer-using-agents`（简称 **SL**），已 clone 到本地。

**完整 PRD**: `/home/ubuntu/tech/blog/gp-improve/PRD-改进计划.md`

---

## ⚠️ 关键约束

1. **GP 用 openCypher（HTTP API + SigV4）**，SL 用 Gremlin — 不要照搬 Gremlin 查询，要转成 openCypher
2. **GP 的 Neptune client** 在 `rca/neptune/neptune_client.py`，用 `requests` + SigV4Auth — 所有新查询必须复用这个 client
3. **GP 的 Lambda 运行时是 Python 3.12**，依赖尽量用标准库 + boto3，避免引入重型依赖
4. **Region**: `ap-northeast-1`，Bedrock model: `global.anthropic.claude-sonnet-4-6`
5. **不要动现有的 Q1–Q16 查询逻辑**，只新增 Q17/Q18 和 NL 查询层

---

## Phase A: 非结构化数据整合 ✅ 已完成

Phase A 的 4 个任务（A1-A4）已全部实现。

### 任务 A1: Incident Entity Extraction + Neptune 关联

**要改的文件**: `rca/actions/incident_writer.py`

**当前逻辑** (已有):
- `write_incident()` 创建 `Incident` 节点，建 `TriggeredBy` 边到 affected_service
- 完整 RCA 报告文本在调用时已可获取（从 `graph_rag_reporter.py` 生成）

**要加的逻辑**:
1. 从 RCA 报告文本中提取实体（服务名、AWS 资源 ID）
2. 为每个提取到的实体创建 `Incident -[:MentionsResource]-> Resource` 边

**参考 SL 代码**:
- `SL/src/agents/entity-resolution-agent/agent.py` — **重点看**:
  - `EntityResolutionAgent.resolve_entities()` 方法（~行 200-350）：如何用 LLM 做实体匹配和消歧
  - `calculate_similarity_score()` 方法：多信号相似度打分
  - `create_resolved_links()` 方法：写 Neptune 关联边
- `SL/src/lambda/document-processing/index.py` — **重点看**:
  - `UnstructuredDataProcessor.extract_entities_with_comprehend()` 方法（~行 90-150）：Comprehend 实体提取
  - `process_text_document()` 方法：完整的文本处理管线

**但是 GP 不需要这么复杂**，因为：
- GP 的实体集是封闭的（已知的服务名 + AWS 资源 ID 格式固定）
- 用正则 + config.py 的 CANONICAL 映射就能覆盖 80% 场景

**建议实现** (在 `incident_writer.py` 中新增):
```python
import re

# 已知实体模式
RESOURCE_PATTERNS = {
    'ec2': re.compile(r'i-[0-9a-f]{8,17}'),
    'rds': re.compile(r'(?:arn:aws:rds:[^:]+:[^:]+:cluster:)?([a-zA-Z][a-zA-Z0-9-]+)'),
    'dynamodb': re.compile(r'(?:table[/:])?([a-zA-Z][a-zA-Z0-9_.-]+)'),
}

def _extract_entities(report_text: str) -> list[dict]:
    """从 RCA 报告中提取实体引用"""
    entities = []
    
    # 1. 精确匹配：服务名（从 config.CANONICAL 反查）
    from config import CANONICAL
    all_service_names = set(CANONICAL.values())
    for svc in all_service_names:
        if svc in report_text:
            entities.append({'type': 'Microservice', 'name': svc})
    
    # 2. 正则匹配：AWS 资源 ID
    for ec2_id in RESOURCE_PATTERNS['ec2'].findall(report_text):
        entities.append({'type': 'EC2Instance', 'id': ec2_id})
    
    return entities

def _link_entities_to_incident(incident_id: str, entities: list[dict]):
    """创建 Incident -[:MentionsResource]-> Resource 边"""
    from neptune import neptune_client as nc
    for ent in entities:
        if ent['type'] == 'Microservice':
            nc.results("""
                MATCH (inc:Incident {id: $inc_id})
                MATCH (svc:Microservice {name: $name})
                MERGE (inc)-[:MentionsResource]->(svc)
            """, {'inc_id': incident_id, 'name': ent['name']})
        elif ent['type'] == 'EC2Instance':
            nc.results("""
                MATCH (inc:Incident {id: $inc_id})
                MATCH (ec2:EC2Instance {instance_id: $id})
                MERGE (inc)-[:MentionsResource]->(ec2)
            """, {'inc_id': incident_id, 'id': ent['id']})
```

然后在 `write_incident()` 末尾调用：
```python
entities = _extract_entities(report_text)
_link_entities_to_incident(incident_id, entities)
```

---

### 任务 A2: 混沌实验写入 Neptune

**要新建的文件**: `chaos/code/neptune_sync.py`

**要改的文件**: `chaos/code/orchestrator.py`

**参考 SL 代码**:
- `SL/src/lambda/neptune-populator/populate_neptune_dynamic.py` — **重点看**:
  - 如何动态创建节点和边
  - 幂等写入模式（MERGE 而非 CREATE）
  - 属性序列化

**GP 的 Neptune 写入模式**参考 `infra/lambda/etl_aws/neptune_client.py`（idempotent upsert），新文件应复用 `rca/neptune/neptune_client.py`。

**neptune_sync.py 核心实现**:
```python
"""将 DynamoDB chaos-experiments 记录同步到 Neptune"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../rca'))
from neptune import neptune_client as nc

def write_experiment(experiment: dict):
    """写入 ChaosExperiment 节点 + TestedBy 边"""
    exp_id = experiment['experiment_id']
    service = experiment['target_service']
    
    # 幂等写入（MERGE）
    nc.results("""
        MERGE (exp:ChaosExperiment {experiment_id: $exp_id})
        ON CREATE SET
            exp.fault_type = $fault_type,
            exp.result = $result,
            exp.recovery_time_sec = $recovery_time,
            exp.degradation_rate = $degradation,
            exp.timestamp = $timestamp
        ON MATCH SET
            exp.result = $result,
            exp.recovery_time_sec = $recovery_time,
            exp.degradation_rate = $degradation
    """, {
        'exp_id': exp_id,
        'fault_type': experiment.get('fault_type', ''),
        'result': experiment.get('result', ''),
        'recovery_time': experiment.get('recovery_time_sec', 0),
        'degradation': experiment.get('degradation_rate', 0.0),
        'timestamp': experiment.get('timestamp', ''),
    })
    
    # 建立 Microservice -[:TestedBy]-> ChaosExperiment 边
    nc.results("""
        MATCH (svc:Microservice {name: $svc})
        MATCH (exp:ChaosExperiment {experiment_id: $exp_id})
        MERGE (svc)-[:TestedBy]->(exp)
    """, {'svc': service, 'exp_id': exp_id})
```

**在 orchestrator.py 中集成**（Phase 5 Report 阶段结束后）:
```python
# 在 _phase5_report() 末尾添加
try:
    from neptune_sync import write_experiment
    write_experiment({
        'experiment_id': self.experiment_id,
        'target_service': self.target_service,
        'fault_type': self.fault_type,
        'result': self.result,
        'recovery_time_sec': self.recovery_time,
        'degradation_rate': self.degradation_rate,
        'timestamp': datetime.utcnow().isoformat(),
    })
    logger.info(f"Experiment {self.experiment_id} synced to Neptune")
except Exception as e:
    logger.warning(f"Neptune sync failed (non-fatal): {e}")
```

---

### 任务 A3: 新增 Q17/Q18 查询

**要改的文件**: `rca/neptune/neptune_queries.py`

```python
def q17_incidents_by_resource(resource_name: str, limit: int = 5) -> list:
    """Q17: 查找涉及相同资源的历史 incident"""
    cypher = """
    MATCH (inc:Incident)-[:MentionsResource]->(r {name: $name})
    WHERE inc.status = 'resolved'
    RETURN inc.id AS id, inc.severity AS severity,
           inc.root_cause AS root_cause, inc.resolution AS resolution,
           inc.start_time AS start_time
    ORDER BY inc.start_time DESC
    LIMIT $limit
    """
    return nc.results(cypher, {"name": resource_name, "limit": limit})

def q18_chaos_history(service_name: str, limit: int = 5) -> list:
    """Q18: 查询服务的混沌实验历史"""
    cypher = """
    MATCH (svc:Microservice {name: $svc})-[:TestedBy]->(exp:ChaosExperiment)
    RETURN exp.experiment_id AS id, exp.fault_type AS fault_type,
           exp.result AS result, exp.recovery_time_sec AS recovery_time,
           exp.degradation_rate AS degradation, exp.timestamp AS timestamp
    ORDER BY exp.timestamp DESC
    LIMIT $limit
    """
    return nc.results(cypher, {"svc": service_name, "limit": limit})
```

---

### 任务 A4: 增强 RCA 报告上下文

**要改的文件**: `rca/core/graph_rag_reporter.py`

在 `_get_neptune_subgraph()` 方法中，**在现有的基础设施层查询之后**，添加：

```python
# === 历史上下文（Phase A 新增） ===
try:
    from neptune import neptune_queries as nq
    
    # 同资源历史 incident
    hist_incidents = nq.q17_incidents_by_resource(affected_service, limit=3)
    if hist_incidents:
        lines.append("")
        lines.append("[历史故障记录（同服务）]")
        for inc in hist_incidents:
            lines.append(
                f"- {inc.get('id','?')} | {inc.get('severity','?')} | "
                f"根因: {inc.get('root_cause','?')} | 修复: {inc.get('resolution','?')}"
            )
    
    # 混沌实验历史
    chaos_hist = nq.q18_chaos_history(affected_service, limit=3)
    if chaos_hist:
        lines.append("")
        lines.append("[混沌实验历史]")
        for exp in chaos_hist:
            lines.append(
                f"- {exp.get('fault_type','?')} | 结果: {exp.get('result','?')} | "
                f"恢复: {exp.get('recovery_time',0)}s | 降级率: {exp.get('degradation',0):.1%}"
            )
except Exception as e:
    logger.warning(f"Historical context query failed: {e}")
```

---

## Phase B: 自然语言图查询

### 任务 B1: Schema Prompt 构建

**要新建的文件**: `rca/neptune/schema_prompt.py`

**参考 SL 代码**:
- `SL/src/agents/schema-discovery-agent/agent.py` — **重点看**:
  - 如何从数据库 schema 构建 LLM 可理解的 prompt
  - schema 描述格式
- `SL/src/lambda/general-query-agent/index.py` — **重点看**:
  - `GeneralQueryAgent._get_system_prompt()` 方法（~行 70-100）：system prompt 结构
  - `query_knowledge_graph()` 工具定义：如何描述图查询能力

**但 GP 不需要动态发现 schema**，因为图的 schema 是固定的（22 种节点、17+ 种边）。直接硬编码 schema prompt 即可，后续图 schema 变化时更新这个文件。

```python
"""schema_prompt.py - Neptune 图 schema 描述，供 NL→openCypher 使用"""

GRAPH_SCHEMA = """
## 节点类型（22 种）

### 基础设施层
- Region: name(str)
- AvailabilityZone: name(str)  // e.g. "ap-northeast-1a"
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
- (:Microservice)-[:Calls]->(:Microservice)                    // 运行时调用（来自 DeepFlow）
- (:Microservice)-[:DependsOn]->(:DynamoDB|:RDSCluster|:SQS|:S3|:Lambda)  // 数据/服务依赖
- (:Lambda)-[:Invokes]->(:Lambda|:StepFunction)
- (:Microservice)-[:WritesTo]->(:SQS|:SNS|:S3)
- (:Microservice)-[:PublishesTo]->(:SNS)

### 业务关系
- (:BusinessCapability)-[:Serves]->(:Microservice)
- (:Microservice)-[:Implements]->(:BusinessCapability)

### 运维关系
- (:Incident)-[:TriggeredBy]->(:Microservice)
- (:Incident)-[:MentionsResource]->(any)                        // Phase A 新增
- (:Microservice)-[:TestedBy]->(:ChaosExperiment)               // Phase A 新增

## 已知服务名（PetSite 应用）
petsite, petsearch, payforadoption, petlistadoptions, petadoptionshistory, petfood, trafficgenerator
"""

FEW_SHOT_EXAMPLES = [
    {
        "q": "petsite 依赖哪些数据库？",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(db) WHERE db:RDSCluster OR db:DynamoDB RETURN db.name AS database, labels(db)[0] AS type"
    },
    {
        "q": "AZ ap-northeast-1a 有哪些 Tier0 服务？",
        "cypher": "MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:RunsOn]->(e:EC2Instance)-[:LocatedIn]->(az:AvailabilityZone {name:'ap-northeast-1a'}) WHERE s.recovery_priority = 'Tier0' RETURN DISTINCT s.name AS service, count(p) AS pod_count"
    },
    {
        "q": "payforadoption 的上游调用者有哪些？",
        "cypher": "MATCH (caller)-[:Calls]->(s:Microservice {name:'payforadoption'}) RETURN caller.name AS caller, labels(caller)[0] AS type"
    },
    {
        "q": "上个月 petsite 发生过几次故障？",
        "cypher": "MATCH (inc:Incident)-[:TriggeredBy]->(s:Microservice {name:'petsite'}) WHERE inc.start_time >= '2026-03-01' RETURN inc.id AS incident, inc.severity AS severity, inc.root_cause AS root_cause ORDER BY inc.start_time DESC"
    },
    {
        "q": "哪些服务从未做过混沌实验？",
        "cypher": "MATCH (s:Microservice) WHERE NOT (s)-[:TestedBy]->(:ChaosExperiment) RETURN s.name AS service, s.recovery_priority AS priority ORDER BY CASE s.recovery_priority WHEN 'Tier0' THEN 0 WHEN 'Tier1' THEN 1 ELSE 2 END"
    },
    {
        "q": "petsite 完整的基础设施路径（到 AZ）",
        "cypher": "MATCH (s:Microservice {name:'petsite'})-[:RunsOn]->(p:Pod)-[:RunsOn]->(e:EC2Instance)-[:LocatedIn]->(az:AvailabilityZone) RETURN p.name AS pod, p.status AS pod_status, e.instance_id AS ec2, e.state AS ec2_state, az.name AS az"
    },
]

def build_system_prompt() -> str:
    """构建 NL→openCypher 的 system prompt"""
    examples = "\\n".join([
        f"Q: {ex['q']}\\nCypher: {ex['cypher']}" for ex in FEW_SHOT_EXAMPLES
    ])
    
    return f"""你是一个 Amazon Neptune openCypher 查询生成器。根据用户的自然语言问题，生成正确的 openCypher 查询。

{GRAPH_SCHEMA}

## 规则
1. 只生成 READ 查询（MATCH/OPTIONAL MATCH/RETURN/WHERE/ORDER BY/LIMIT），绝对禁止 CREATE/DELETE/SET/MERGE/REMOVE/DROP
2. 使用参数化查询时用 $param 语法
3. RETURN 必须带有意义的别名（AS ...）
4. 多跳遍历限制 *1..5，避免爆炸
5. 不确定的查询加 LIMIT 50 兜底
6. 只输出 openCypher 查询语句，不要解释

## 示例
{examples}

现在根据用户问题生成查询："""
```

---

### 任务 B2: NL Query Engine + 安全校验

**要新建的文件**: `rca/neptune/nl_query.py`, `rca/neptune/query_guard.py`

**参考 SL 代码**:
- `SL/src/lambda/general-query-agent/index.py` — **重点看**:
  - `GeneralQueryAgent.query()` 方法：完整的查询编排流程
  - `query_structured_data()` 工具：Text-to-SQL 的实现模式（NL→查询→执行→格式化）
  - `_analyze_query_intent()` 方法：意图分析
- `SL/src/lambda/bedrock-integration/index.py` — **重点看**:
  - Bedrock Claude 调用封装
  - Prompt 构建和 response 解析
- `SL/src/agentcore/multi-hop-reasoning/agent_logic.py` — **重点看**:
  - `MultiHopReasoningAgent.multi_hop_analysis()` 方法（~行 150-250）：多步推理编排
  - `_generate_reasoning_chain()` 方法：LLM 推理链生成

**GP 的实现比 SL 简单得多**（不需要 Strands/AgentCore，一个函数调用链就够）:

**query_guard.py**:
```python
"""openCypher 查询安全校验"""
import re

WRITE_KEYWORDS = re.compile(
    r'\b(CREATE|DELETE|DETACH|SET|MERGE|REMOVE|DROP|CALL)\b',
    re.IGNORECASE
)

MAX_RESULT_LIMIT = 200

def is_safe(cypher: str) -> tuple[bool, str]:
    """校验生成的 openCypher 是否安全"""
    if WRITE_KEYWORDS.search(cypher):
        return False, "查询包含写操作关键字"
    if '*1..' in cypher:
        # 检查跳数上限
        hops = re.findall(r'\*1\.\.(\d+)', cypher)
        for h in hops:
            if int(h) > 6:
                return False, f"遍历深度 {h} 超过限制（最大 6）"
    return True, "OK"

def ensure_limit(cypher: str) -> str:
    """确保查询有 LIMIT 子句"""
    if 'LIMIT' not in cypher.upper():
        cypher = cypher.rstrip().rstrip(';') + f' LIMIT {MAX_RESULT_LIMIT}'
    return cypher
```

**nl_query.py**:
```python
"""自然语言 → openCypher 查询引擎"""
import json, logging, boto3, os
from neptune import neptune_client as nc
from neptune.schema_prompt import build_system_prompt
from neptune import query_guard

logger = logging.getLogger(__name__)
REGION = os.environ.get('REGION', 'ap-northeast-1')
MODEL = os.environ.get('BEDROCK_MODEL', 'global.anthropic.claude-sonnet-4-6')

class NLQueryEngine:
    def __init__(self):
        self.bedrock = boto3.client('bedrock-runtime', region_name=REGION)
        self.system_prompt = build_system_prompt()
    
    def query(self, question: str) -> dict:
        # 1. LLM 生成 openCypher
        cypher = self._generate_cypher(question)
        
        # 2. 安全校验
        safe, reason = query_guard.is_safe(cypher)
        if not safe:
            return {"error": reason, "cypher": cypher}
        cypher = query_guard.ensure_limit(cypher)
        
        # 3. 执行查询
        try:
            results = nc.results(cypher)
        except Exception as e:
            return {"error": str(e), "cypher": cypher}
        
        # 4. 生成自然语言摘要
        summary = self._summarize(question, results)
        
        return {
            "question": question,
            "cypher": cypher,
            "results": results,
            "summary": summary,
        }
    
    def _generate_cypher(self, question: str) -> str:
        resp = self.bedrock.invoke_model(
            modelId=MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": self.system_prompt,
                "messages": [{"role": "user", "content": question}],
            })
        )
        body = json.loads(resp['body'].read())
        text = body['content'][0]['text'].strip()
        # 清理 markdown 代码块
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return text
    
    def _summarize(self, question: str, results: list) -> str:
        if not results:
            return "查询无结果。"
        resp = self.bedrock.invoke_model(
            modelId=MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": 
                    f"用户问题: {question}\n查询结果: {json.dumps(results, ensure_ascii=False, default=str)[:3000]}\n\n用简洁中文总结查询结果:"}],
            })
        )
        body = json.loads(resp['body'].read())
        return body['content'][0]['text'].strip()
```

---

### 任务 B3: CLI 工具

**要新建的文件**: `rca/scripts/graph-ask.py`

```python
#!/usr/bin/env python3
"""命令行工具：自然语言查询 Neptune 图谱"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from neptune.nl_query import NLQueryEngine

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 graph-ask.py '你的问题'")
        sys.exit(1)
    
    question = ' '.join(sys.argv[1:])
    engine = NLQueryEngine()
    result = engine.query(question)
    
    if 'error' in result:
        print(f"❌ 错误: {result['error']}")
        print(f"   生成的 Cypher: {result.get('cypher', 'N/A')}")
        sys.exit(1)
    
    print(f"📝 问题: {result['question']}")
    print(f"🔍 Cypher: {result['cypher']}")
    print(f"📊 结果 ({len(result['results'])} 条):")
    print(json.dumps(result['results'], indent=2, ensure_ascii=False, default=str))
    print(f"\n💡 摘要: {result['summary']}")

if __name__ == '__main__':
    main()
```

---

## SL 参考文件速查表

编码时如果需要看 SL 的具体实现细节，按这个表查找：

| GP 任务 | 参考文件 | 看什么 |
|---------|---------|--------|
| A1 实体提取 | `SL/src/lambda/document-processing/index.py` | `extract_entities_with_comprehend()` 实体提取管线 |
| A1 实体关联 | `SL/src/agents/entity-resolution-agent/agent.py` | `resolve_entities()` 匹配 + `create_resolved_links()` 写边 |
| A2 节点写入 | `SL/src/lambda/neptune-populator/populate_neptune_dynamic.py` | 动态节点创建 + 幂等 MERGE 模式 |
| B1 Schema Prompt | `SL/src/lambda/general-query-agent/index.py` | `_get_system_prompt()` prompt 结构 |
| B2 查询引擎 | `SL/src/lambda/bedrock-integration/index.py` | Bedrock 调用封装 + response 解析 |
| B2 多跳推理 | `SL/src/agentcore/multi-hop-reasoning/agent_logic.py` | `multi_hop_analysis()` 推理编排 |
| B5 向量索引 | `/home/ubuntu/tech/s3-vector-skill/scripts/embed.py` | Bedrock Titan v2 embedding（带缓存） |
| B5 分块 | `/home/ubuntu/tech/s3-vector-skill/scripts/chunker.py` | Recursive splitting 512 tokens |
| B5 摄入管线 | `/home/ubuntu/tech/s3-vector-skill/scripts/ingest.py` | 完整文档摄入流程 |
| B5 语义搜索 | `/home/ubuntu/tech/s3-vector-skill/scripts/search.py` | Query → embedding → S3 Vectors → 格式化 |

---

---

### 任务 B5: Incident 向量搜索索引（S3 Vectors）

**背景**: Q17 通过图边做精确匹配，但无法做语义模糊搜索。使用 S3 Vectors（< $0.02/月）替代 OpenSearch Serverless（~$30+/月）。

**参考实现**: `/home/ubuntu/tech/s3-vector-skill/scripts/` — 完整的 S3 Vectors 工具链（已部署可用）

**要新建的文件**: `rca/search/incident_vectordb.py`

**参考 s3-vector-skill 代码**:
- `s3-vector-skill/scripts/embed.py` — Bedrock Titan v2 embedding 生成（带磁盘缓存）
- `s3-vector-skill/scripts/chunker.py` — 文本分块（recursive splitting, 512 tokens）
- `s3-vector-skill/scripts/ingest.py` — 完整的文档摄入管线
- `s3-vector-skill/scripts/search.py` — 语义搜索（query → embedding → S3 Vectors → 格式化）
- `s3-vector-skill/scripts/common.py` — S3 Vectors 客户端创建
- `s3-vector-skill/scripts/create_vector_bucket.py` — 向量桶创建
- `s3-vector-skill/scripts/create_index.py` — 索引创建

**核心实现** (`rca/search/incident_vectordb.py`):
```python
"""RCA Incident 向量索引 — 基于 S3 Vectors

复用 s3-vector-skill 的 embed.py 和 chunker.py:
  sys.path.insert(0, '/home/ubuntu/tech/s3-vector-skill/scripts')
  from embed import embed_text
  from chunker import chunk_text
"""
import sys, os, boto3, logging

sys.path.insert(0, '/home/ubuntu/tech/s3-vector-skill/scripts')
from embed import embed_text
from chunker import chunk_text

logger = logging.getLogger(__name__)
REGION = os.environ.get('REGION', 'ap-northeast-1')
BUCKET = os.environ.get('INCIDENT_VECTOR_BUCKET', 'gp-incident-kb')
INDEX = 'incidents-v1'

def _get_client():
    return boto3.client('s3vectors', region_name=REGION)

def ensure_bucket_and_index():
    """首次使用时创建向量桶和索引（幂等）"""
    client = _get_client()
    try:
        client.get_vector_bucket(vectorBucketName=BUCKET)
    except client.exceptions.NotFoundException:
        client.create_vector_bucket(vectorBucketName=BUCKET)
        logger.info(f"Created vector bucket: {BUCKET}")
    try:
        client.get_index(vectorBucketName=BUCKET, indexName=INDEX)
    except client.exceptions.NotFoundException:
        client.create_index(
            vectorBucketName=BUCKET,
            indexName=INDEX,
            dimension=1024,
            distanceMetric='cosine',
        )
        logger.info(f"Created index: {INDEX}")

def index_incident(incident_id: str, report_text: str, metadata: dict):
    """将 RCA 报告分块 + 向量化 + 写入 S3 Vectors"""
    ensure_bucket_and_index()
    chunks = chunk_text(report_text, chunk_size=512, chunk_overlap=64)
    client = _get_client()
    
    vectors = []
    for i, chunk in enumerate(chunks):
        vec = embed_text(chunk['content'])
        vectors.append({
            'key': f"{incident_id}.chunk-{i:04d}",
            'data': {'float32': vec},
            'metadata': {
                'incident_id': incident_id,
                'severity': metadata.get('severity', ''),
                'affected_service': metadata.get('affected_service', ''),
                'root_cause': metadata.get('root_cause', ''),
                'content': chunk['content'][:2000],
                'timestamp': metadata.get('timestamp', ''),
            }
        })
    
    # 批量写入（S3 Vectors 支持一次 put 多个）
    BATCH = 20
    for j in range(0, len(vectors), BATCH):
        client.put_vectors(
            vectorBucketName=BUCKET,
            indexName=INDEX,
            vectors=vectors[j:j+BATCH]
        )
    logger.info(f"Indexed {len(vectors)} chunks for incident {incident_id}")

def search_similar(query: str, top_k: int = 3, threshold: float = 0.6) -> list:
    """语义搜索相似历史 incident"""
    vec = embed_text(query)
    client = _get_client()
    resp = client.query_vectors(
        vectorBucketName=BUCKET,
        indexName=INDEX,
        queryVector={'float32': vec},
        topK=top_k,
        returnDistance=True,
        returnMetadata=True,
    )
    results = []
    for r in resp.get('vectors', []):
        score = 1 - (r.get('distance', 0) / 2)  # cosine distance → similarity
        if score >= threshold:
            results.append({**r.get('metadata', {}), 'score': score})
    return results
```

**集成到 incident_writer.py** — 在 `write_incident()` 末尾（entity linking 之后）:
```python
# 向量索引（non-fatal）
try:
    from search.incident_vectordb import index_incident as vec_index
    vec_index(incident_id, report_text, {
        'severity': severity,
        'affected_service': svc,
        'root_cause': root_cause,
        'timestamp': now_iso,
    })
except Exception as e:
    logger.warning(f"Vector indexing failed (non-fatal): {e}")
```

**集成到 graph_rag_reporter.py** — 在历史故障记录之后:
```python
# 语义搜索相似历史 incident
try:
    from search.incident_vectordb import search_similar
    fault_desc = f"{affected_service} 故障: {' '.join(lines[:5])}"
    similar = search_similar(fault_desc, top_k=3)
    if similar:
        lines.append("")
        lines.append("[语义相似历史故障（向量搜索）]")
        for s in similar:
            lines.append(
                f"- {s.get('incident_id','?')} | {s.get('severity','?')} | "
                f"服务: {s.get('affected_service','?')} | 根因: {s.get('root_cause','?')} | "
                f"相似度: {s.get('score',0):.2f}"
            )
except Exception as e:
    logger.warning(f"Semantic incident search failed: {e}")
```

---

## 测试验证

### Phase A 验证
```bash
# 1. 手动触发一次 RCA，检查 Neptune 是否新增了 MentionsResource 边
# 2. 查询验证
python3 -c "
from neptune import neptune_queries as nq
print('Q17:', nq.q17_incidents_by_resource('petsite'))
print('Q18:', nq.q18_chaos_history('petsite'))
"
```

### Phase B 验证
```bash
# 测试 NL 查询
cd rca
python3 scripts/graph-ask.py "petsite 的所有下游依赖有哪些？"
python3 scripts/graph-ask.py "AZ ap-northeast-1a 有多少个 Pod？"
python3 scripts/graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
python3 scripts/graph-ask.py "最近一周发生了几次 P0 故障？"
python3 scripts/graph-ask.py "payforadoption 的完整调用链"

# 测试向量搜索
python3 -c "
from search.incident_vectordb import search_similar, ensure_bucket_and_index
ensure_bucket_and_index()
print('Bucket/Index ready')
results = search_similar('DynamoDB 限流导致服务超时')
print('Similar incidents:', results)
"
```

[中文文档](./README_CN.md) | English

# rca_engine — Neptune Graph + AIOps Root Cause Analysis Engine

An AWS Lambda-based AIOps engine that:
1. Receives CloudWatch/SNS alerts (e.g., `HTTPCode_Target_5XX_Count > 5`)
2. Classifies fault severity (P0/P1/P2)
3. Runs multi-layer RCA: DeepFlow L7/L4 → CloudTrail → Neptune graph traversal → **Plugin-based AWS Service Probers**
4. Generates a Graph RAG report via Bedrock Claude
5. Sends Slack notification with evidence and recommended actions
6. Writes incident to Neptune knowledge base

> **Prerequisite**: The Neptune dependency graph must be built first using the companion [infra/](../infra/) directory.

---

## Ecosystem — Four Directories, One Platform

This directory (`rca/`) is the **AIOps RCA engine** of a larger observability + resilience platform built around PetSite on AWS EKS. The platform is organized as a monorepo; four directories work together:

```
┌─────────────────────────────────────────────────────────────────┐
│                     PetSite on AWS EKS                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
         ┌──────────────────▼──────────────────┐
         │  📦 infra/                          │
         │  CDK infra + modular ETL pipeline   │
         │  → builds Neptune knowledge graph   │
         └────┬──────────┬──────────┬──────────┘
              │ graph     │ alarm    │ graph
              │ queries   │ trigger  │ queries
              ▼           ▼           ▼
   ┌────────────┐  ┌────────────┐  ┌──────────────────────┐
   │ 🔍 rca/    │  │ 💥 chaos/  │  │ 📋 dr-plan-          │
   │  (this dir)│  │ AI-driven  │  │    generator/        │
   │  Multi-    │  │ chaos eng. │  │  Neptune-driven      │
   │  layer RCA │  │(FIS + CM)  │  │  DR plan generation  │
   │  + Probers │  │            │  │                      │
   │  + GraphRAG│  │            │  │                      │
   └─────┬──────┘  └──────┬─────┘  └──────────────────────┘
         │  writes         │ validates RCA
         │  incidents      │
         └────────────────┘
               closed loop
```

| Directory | Role |
|-----------|------|
| **[infra/](../infra/)** | Infrastructure layer — CDK stacks, Neptune ETL pipeline, DeepFlow + AWS topology ingestion |
| **rca/** | AIOps RCA engine — multi-layer root cause analysis, plugin-based AWS probers, Bedrock Graph RAG reports (this directory) |
| **[chaos/](../chaos/)** | AI-driven chaos engineering — hypothesis generation, 5-phase experiment runner, FIS + Chaos Mesh closed-loop learning |
| **[dr-plan-generator/](../dr-plan-generator/)** | Disaster recovery plan generator — Neptune graph-driven DR step generation and rollback plans |

**Data flow:** `infra/` ETL populates Neptune → CloudWatch Alarm triggers `rca/` for root cause analysis → `chaos/` injects faults to validate RCA accuracy → `dr-plan-generator/` uses the graph to generate DR plans → results feed back into Neptune.

---

## Architecture

### Core RCA Pipeline

```
CloudWatch Alarm
    │
    ▼ SNS
handler.py
    │
    ├─ [Aggregation path] ───→ Phase 4 Alert Aggregation (see section below)
    │   event_normalizer → alert_buffer → topology_correlator → decision_engine
    │
    ├─ fault_classifier.py ──────────────────────────────→ P0/P1/P2 severity
    │
    ├─ rca_engine.py ── Multi-layer RCA ─────────────────────────────────────┐
    │   Step 1:  DeepFlow L7 (HTTP 5xx call chain)                          │
    │   Step 1b: DeepFlow L4 (TCP RST/timeout/SYN retrans)                  │
    │   Step 2:  CloudTrail change events                                    │
    │   Step 3:  Neptune graph traversal (Service→Pod→EC2→AZ)               │
    │   Step 3b: Temporal validation (graph depth × timestamp)              │
    │   Step 3c: CW Logs sampling (ERROR/FATAL)                              │
    │   Step 3d: Layer2 probers (parallel)  ←── collectors/aws_probers.py   │
    │   Step 3e: Historical context         ←── neptune Q17/Q18             │
    │   Step 3f: Semantic incident search   ←── search/incident_vectordb.py │
    │   Step 4:  Confidence scoring (max 100)                               │
    │                                                                        │
    ├─ graph_rag_reporter.py ───────────────────────────────────────────────┘
    │   Bedrock Claude + Neptune subgraph → structured RCA report
    │
    ├─ actions/
    │   ├─ playbook_engine.py ──→ fault playbook matching
    │   ├─ semi_auto.py ────────→ P1/P2 semi-auto execution
    │   ├─ action_executor.py ──→ kubectl rollout/scale
    │   ├─ slack_notifier.py ───→ Slack notification + buttons
    │   └─ incident_writer.py ──→ Neptune Incident + S3 + Bedrock KB + S3 Vectors
    │
    └─ feedback_collector.py ───→ Slack feedback → Neptune write-back (Q19/Q20)
```

---

## Key Design: Multi-Layer Root Cause Detection

### Layer 1: Neptune Graph Traversal (preferred)

The graph already contains the full infrastructure chain built by ETL:

```
Microservice ─[RunsOn]→ Pod ─[RunsOn]→ EC2Instance ─[LocatedIn]→ AZ
```

- **Q10** queries all EC2 nodes with `state != 'running'` and reverse-traverses to find affected Pods and Services
- **Q11** expands blast radius: given faulty EC2 IDs, finds ALL impacted services (not just the alerting one)
- Works when ETL has run recently and Neptune has up-to-date `EC2Instance.state`

### Layer 2: Plugin-based AWS Service Probers (`collectors/aws_probers.py`)

A self-extensible probe framework that runs **in parallel (Step 3d)** alongside Neptune graph traversal to cover AWS managed service faults that Neptune cannot see.

#### Design

```
ProbeRegistry (auto-discovery via @register_probe decorator)
    │
    ├── SQSProbe            ← queue backlog + DLQ accumulation
    ├── DynamoDBProbe       ← ReadThrottle / WriteThrottle / SystemErrors
    ├── LambdaProbe         ← Errors / Throttles / Duration near timeout
    ├── ALBProbe            ← ELB_5XX / RejectedConnections / UnhealthyTargets / latency
    ├── StepFunctionsProbe  ← ExecutionsFailed / TimedOut / Throttled
    └── EC2ASGProbe         ← EKS node non-running (only when Neptune q10 found nothing)
```

Each probe implements a two-method contract:

```python
class BaseProbe:
    def is_relevant(self, signal: dict, affected_service: str) -> bool:
        """Should this probe run for this alarm/service?"""

    def probe(self, signal: dict, affected_service: str) -> Optional[ProbeResult]:
        """Execute probe; return ProbeResult or None if nothing found."""
```

All probes return a unified `ProbeResult`:

```python
@dataclass
class ProbeResult:
    service_name: str    # e.g. "SQS", "DynamoDB"
    healthy: bool        # False = anomaly detected
    score_delta: int     # RCA confidence score bonus (0~40)
    summary: str         # One-line finding
    evidence: list       # Bullet points injected into Slack + Graph RAG prompt
    details: dict        # Raw data for debugging
```

`run_all_probes()` runs all relevant probes concurrently via `ThreadPoolExecutor` (timeout=12s), then:
- Sums `score_delta` from all anomalous probes (capped at 40)
- Appends evidence to the `top_candidate` in the scoring pipeline
- Injects all probe findings into the Graph RAG prompt for Bedrock Claude

#### Coverage vs. fault type

| Fault Type | Neptune (L1) | AWS Probers (L2) |
|-----------|-------------|-----------------|
| EC2 node down / AZ outage | ✅ Q10 + Q11 | ✅ EC2ASGProbe (fallback) |
| Pod CrashLoop / OOM | ✅ Q6 + infra_collector | — |
| RDS connection exhausted | ✅ infra_collector | — |
| **SQS backlog / DLQ messages** | ❌ | ✅ SQSProbe |
| **DynamoDB throttling** | ❌ | ✅ DynamoDBProbe |
| **Lambda errors / throttles** | ❌ | ✅ LambdaProbe |
| **ALB ELB-side 5XX / rejected connections** | ❌ | ✅ ALBProbe |
| **Step Functions execution failure** | ❌ | ✅ StepFunctionsProbe |
| Application code deploy error | — | ✅ CloudTrail (step2) |

#### Adding a new probe

No changes to `rca_engine.py` or any other file needed. Just add a class to `collectors/aws_probers.py`:

```python
@register_probe                          # auto-registers on import
class MyServiceProbe(BaseProbe):

    def is_relevant(self, signal, affected_service):
        return affected_service in ('my-service', 'petsite')

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        # Query AWS API / CloudWatch
        # ...
        return ProbeResult(
            service_name='MyService',
            healthy=False,
            score_delta=20,
            summary='Anomaly found',
            evidence=['metric=value'],
        )
```

---

## Prerequisites

| Component | Description |
|-----------|-------------|
| **Neptune graph** | Built by [infra/](../infra/). Microservice, Pod, EC2Instance, AZ nodes; `Calls`, `DependsOn`, `RunsOn`, `LocatedIn` edges. ETL runs every 15 min. |
| **EKS cluster** | Target Kubernetes cluster. Lambda needs `eks:DescribeCluster`. |
| **DeepFlow / ClickHouse** | eBPF observability. `l7_flow_log` (HTTP 5xx) and `l4_flow_log` (TCP RST/timeout/SYN retrans) tables. |
| **Bedrock** | Claude Sonnet (`bedrock:InvokeModel`) + Knowledge Base (`bedrock-agent-runtime:Retrieve`). |
| **Slack** | Incoming Webhook URL stored in SSM Parameter Store. |
| **IAM Role** | Lambda execution role needs: `neptune-db:*`, `eks:DescribeCluster`, `cloudtrail:LookupEvents`, `cloudwatch:GetMetricData`, `logs:*`, `ssm:GetParameter*`, `bedrock:InvokeModel`, `bedrock-agent-runtime:Retrieve`, `ec2:DescribeInstances`, `rds:Describe*`, `autoscaling:DescribeAutoScalingGroups`, `sqs:ListQueues`, `sqs:GetQueueAttributes`, `dynamodb:ListTables`, `lambda:ListFunctions`, `lambda:GetFunctionConfiguration`, `states:ListStateMachines`, `elasticloadbalancing:Describe*`. |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REGION` | No | `ap-northeast-1` | AWS region |
| `NEPTUNE_ENDPOINT` | **Yes** | — | Neptune cluster endpoint hostname |
| `NEPTUNE_PORT` | No | `8182` | Neptune port |
| `EKS_CLUSTER_NAME` | **Yes** | — | EKS cluster name (used by infra_collector + EC2ASGProbe) |
| `CLICKHOUSE_HOST` | **Yes** | — | ClickHouse/DeepFlow host (internal IP) |
| `CLICKHOUSE_PORT` | No | `8123` | ClickHouse HTTP port |
| `BEDROCK_MODEL` | No | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID |
| `BEDROCK_KB_ID` | **Yes** | — | Bedrock Knowledge Base ID |
| `SLACK_WEBHOOK_URL` | No | — | Slack Incoming Webhook (injected by `deploy.sh` from SSM) |
| `SLACK_CHANNEL` | No | — | Slack channel ID for notifications |

---

## Confidence Scoring (step4)

```
Base scoring (max 100):
  +40  Earliest service to show anomaly (L7 5xx or L4 TCP errors)
  +30  Recent CloudTrail change event correlated to the service
  +20  Neptune graph confirms service is a call-chain origin (no upstream errors)
  +10  Bedrock KB finds similar past incidents

Infrastructure layer (additive):
  +40  Neptune q10 or EC2ASGProbe finds non-running EC2 nodes
       (single-AZ concentration noted in evidence)

Layer 2 AWS Service Probers (additive, capped at +40 total):
  +20~30  SQS / DynamoDB / Lambda / ALB / StepFunctions anomaly detected
  (probes run in parallel; score_delta values are summed then capped)

L4 TCP signal (when L7 has no data):
  +40  SYN retransmission detected (Pod completely unreachable)
  +15  TCP RST count > 10
  +10  TCP timeout count > 20

Cross-validation (when L7 has data):
  +10  L4 anomalies corroborate L7 findings

Temporal validation (step3b):
  +0~10  DeepFlow first_error timestamp aligns with Neptune graph path depth

Cap: min(score, 100)
```

---

## Neptune Queries Reference

| Query | Purpose |
|-------|---------|
| **Q1** `q1_blast_radius` | Downstream impact: service → 5-hop `Calls/DependsOn` + BusinessCapability |
| **Q2** `q2_tier0_status` | All Tier0 services: fault_boundary, AZ, replicas |
| **Q3** `q3_upstream_deps` | Upstream services calling the faulty service |
| **Q4** `q4_service_info` | Single service properties |
| **Q5** `q5_similar_incidents` | Historical resolved incidents for the service |
| **Q6** `q6_pod_status` | Pod status from Neptune (ETL-written) |
| **Q7** `q7_db_connections` | Database connections for the service |
| **Q8** `q8_log_source` | CloudWatch log group for the service |
| **Q9** `q9_service_infra_path` | **Service → Pod → EC2 → AZ** full infrastructure chain |
| **Q10** `q10_infra_root_cause` | **All non-running EC2** in cluster, reverse to find affected Pods/Services + AZ impact |
| **Q11** `q11_broader_impact` | Given faulty EC2 IDs, find ALL affected services (blast radius) |
| **Q17** `q17_incidents_by_resource` | Historical resolved incidents that mention the same resource (`MentionsResource` edge) |
| **Q18** `q18_chaos_history` | Chaos experiment history for a service (`TestedBy` edge) |

---

## Deployment

### 1. Configure

```bash
cp .env.example .env
# Fill in: ACCOUNT, FUNCTION_NAME, ROLE_ARN, SUBNET_IDS, SG_IDS,
#          NEPTUNE_ENDPOINT, EKS_CLUSTER, CLICKHOUSE_HOST, BEDROCK_KB_ID
```

### 2. Deploy

```bash
bash deploy.sh           # Full deploy
bash deploy.sh --dry-run # Preview only
```

`deploy.sh` performs:
1. `pip install requests` into build dir
2. Recursively copies source directories (`core/`, `neptune/`, `collectors/`, `actions/`, `data/`) + root `.py` files
3. `zip` into Lambda deployment package
4. `aws lambda update-function-code`
5. Configure environment variables (reads Slack webhook from SSM)
6. Create/verify SNS topic + Lambda subscription
7. Smoke test

### 3. Trigger

The Lambda is triggered by SNS when CloudWatch Alarm fires. Test manually:

```bash
# Via SNS payload (production format)
cat << 'EOF' > /tmp/test-payload.json
{
  "Records": [{
    "Sns": {
      "Message": "{\"AlarmName\":\"petsite-rca-alb-5xx-high\",\"NewStateValue\":\"ALARM\",\"NewStateReason\":\"test\",\"Trigger\":{\"Namespace\":\"AWS/ApplicationELB\",\"MetricName\":\"HTTPCode_Target_5XX_Count\",\"Dimensions\":[{\"name\":\"TargetGroup\",\"value\":\"targetgroup/Servic-PetSi-BGUX1XK3RN6D/8d4815db7d125b15\"},{\"name\":\"LoadBalancer\",\"value\":\"app/Servic-PetSi-by0kpyBtxswj/bbe5082588a126fc\"}]}}"
    }
  }]
}
EOF

aws lambda invoke \
  --function-name petsite-rca-engine \
  --payload fileb:///tmp/test-payload.json \
  --region ap-northeast-1 \
  /tmp/rca-output.json

cat /tmp/rca-output.json | python3 -m json.tool
```

---

## Module Reference

| Module | File | Role |
|--------|------|------|
| _(root)_ | `handler.py` | Lambda entry point; parses SNS/CW events, orchestrates all modules |
| _(root)_ | `config.py` | Service name mapping (★ now loaded from `profiles/petsite.yaml` with hardcoded fallback) |
| **core/** | `rca_engine.py` | Multi-layer RCA engine: DeepFlow L7/L4 + CloudTrail + Neptune graph + AWS Probers + scoring |
| **core/** | `fault_classifier.py` | Severity grading (P0/P1/P2); auto-execution gate |
| **core/** | `graph_rag_reporter.py` | Graph RAG: Neptune subgraph + all probe signals → Claude → structured report |
| **neptune/** | `neptune_client.py` | Neptune HTTP client with IAM SigV4 signing |
| **neptune/** | `neptune_queries.py` | Neptune openCypher queries Q1–Q18 (service + infrastructure + unstructured layers) |
| **neptune/** | `schema_prompt.py` | Graph schema as LLM prompt + few-shot examples (★ dynamically loaded from profile) |
| **neptune/** | `nl_query.py` | `NLQueryEngine`: natural language → openCypher → execute → summarise via Bedrock Claude |
| **neptune/** | `query_guard.py` | openCypher safety validation: blocks write ops, limits hop depth, enforces LIMIT |
| **collectors/** | `infra_collector.py` | Real-time Pod status (K8s API) + DB metrics (CloudWatch RDS) |
| **collectors/** | `aws_probers.py` | **Plugin-based AWS Service Probers** (SQS/DynamoDB/Lambda/ALB/EC2/StepFunctions) |
| **collectors/** | `eks_auth.py` | Shared EKS bearer token generation (SigV4 presigned STS URL) |
| **actions/** | `action_executor.py` | kubectl operations: rollout restart/undo, scale replicas |
| **actions/** | `playbook_engine.py` | Fault playbook matching (4 predefined patterns) |
| **actions/** | `semi_auto.py` | P1/P2 semi-automatic execution; Slack confirmation flow |
| **actions/** | `slack_notifier.py` | Slack message formatting + Incoming Webhook delivery |
| **actions/** | `incident_writer.py` | Neptune Incident node + entity extraction (`MentionsResource` edges) + S3 archive + Bedrock KB + S3 Vectors indexing |
| **search/** | `incident_vectordb.py` | S3 Vectors incident index: chunk + embed (Bedrock Titan v2) + semantic search |
| **data/** | `service-db-mapping.json` | Service → DB cluster mapping |
| **scripts/** | `scan-service-db-mapping.py` | Scans K8s Deployments to discover service→DB relationships |
| **scripts/** | `graph-ask.py` | CLI: ask graph questions in natural language, returns Cypher + results + summary |

---

## Testing

```bash
# Run tests with pytest (recommended)
cd <project-parent-dir>
python3 -m pytest rca_engine/tests/test_rca.py -v

# Or with unittest
python3 -m unittest rca_engine.tests.test_rca -v

# 17 tests: TestStep4Score(5) + TestFaultClassifier(5) + TestPlaybookMatch(7)
```

---

## Project Structure

```
rca_engine/
├── handler.py                  # Lambda entry point (must stay in root)
├── config.py                   # K8s deployment ↔ Neptune name mapping
├── __init__.py
├── core/                       # Core RCA logic
│   ├── rca_engine.py           # Multi-layer RCA engine
│   ├── fault_classifier.py     # P0/P1/P2 severity grading
│   └── graph_rag_reporter.py   # Bedrock Claude Graph RAG report
├── neptune/                    # Graph database layer
│   ├── neptune_client.py       # SigV4-signed HTTP client
│   ├── neptune_queries.py      # Q1-Q18 openCypher queries
│   ├── schema_prompt.py        # Graph schema prompt + few-shot examples (NL query)
│   ├── nl_query.py             # NLQueryEngine: NL→openCypher via Bedrock Claude
│   └── query_guard.py          # Safety: write-op blocking, hop limit, LIMIT enforcement
├── collectors/                 # Real-time data collection
│   ├── infra_collector.py      # K8s Pod status + RDS metrics
│   ├── aws_probers.py          # ★ Plugin-based AWS Service Probers (Layer 2)
│   └── eks_auth.py             # EKS bearer token generation
├── actions/                    # Execution & notification
│   ├── action_executor.py      # kubectl rollout/scale operations
│   ├── playbook_engine.py      # Fault playbook matching
│   ├── semi_auto.py            # Semi-automatic execution flow
│   ├── slack_notifier.py       # Slack webhook delivery
│   └── incident_writer.py      # Neptune + entity extraction + S3 + Bedrock KB + S3 Vectors
├── search/
│   └── incident_vectordb.py    # S3 Vectors incident semantic search
├── data/
│   └── service-db-mapping.json # Service → DB cluster mapping
├── scripts/
│   ├── scan-service-db-mapping.py
│   └── graph-ask.py            # CLI: natural language graph queries
├── tests/
│   └── test_rca.py             # 17 unit tests
├── docs/
│   ├── TDD-fault-recovery-rca.md
│   └── RCA-SYSTEM-DOC.md
├── deploy.sh                   # Lambda packaging + deployment
├── .env.example
└── README.md
```

---

---

## Phase A: Unstructured Data Integration

### Entity Extraction & MentionsResource Edges

`actions/incident_writer.py` now extracts entities (service names + EC2 instance IDs) from RCA report text and creates `Incident -[:MentionsResource]→ Resource` edges in Neptune. This enables Q17 to find historical incidents involving the same resource.

### Chaos Experiment Integration (Q18)

After each chaos experiment completes (Phase 5), `chaos/code/neptune_sync.py` writes a `ChaosExperiment` node and creates a `Microservice -[:TestedBy]→ ChaosExperiment` edge. Q18 queries this history for RCA context.

### Enhanced Graph RAG Context

`core/graph_rag_reporter.py` now enriches RCA reports with three additional context sections:

1. **Historical incidents** — Q17: incidents that mention the same resource
2. **Chaos experiment history** — Q18: past experiments on the affected service
3. **Semantically similar incidents** — S3 Vectors semantic search

---

## Phase B: Natural Language Graph Queries

### NL Query Engine

```python
from neptune.nl_query import NLQueryEngine

engine = NLQueryEngine()
result = engine.query("petsite 依赖哪些数据库？")
# result = { "question": ..., "cypher": ..., "results": [...], "summary": "..." }
```

### CLI Tool

```bash
cd rca
python3 scripts/graph-ask.py "petsite 的所有下游依赖有哪些？"
python3 scripts/graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
python3 scripts/graph-ask.py "AZ ap-northeast-1a 有多少个 Pod？"
python3 scripts/graph-ask.py "最近一周发生了几次 P0 故障？"
```

### Safety Guard

`query_guard.py` enforces three rules before executing any LLM-generated query:

| Rule | Detail |
|------|--------|
| Write-op blocking | Rejects queries containing `CREATE / DELETE / SET / MERGE / REMOVE / DROP / CALL` |
| Hop depth limit | Rejects variable-length traversals with depth > 6 |
| LIMIT enforcement | Appends `LIMIT 200` if no LIMIT clause present |

### Semantic Incident Search (S3 Vectors)

RCA reports are indexed at write-time and semantically searched at read-time:

```python
from search.incident_vectordb import index_incident, search_similar

# On incident write (automatic via incident_writer.py)
index_incident(incident_id, report_text, metadata)

# On next RCA (automatic via graph_rag_reporter.py)
results = search_similar("DynamoDB 限流导致服务超时", top_k=3)
```

---

## Design Documents

- [`docs/TDD-fault-recovery-rca.md`](./docs/TDD-fault-recovery-rca.md) — Technical Design Document
- [`docs/RCA-SYSTEM-DOC.md`](./docs/RCA-SYSTEM-DOC.md) — System document with resource inventory

---

## Known Limitations

1. **Neptune write permission**: RCA Lambda has read-only access to Neptune (openCypher write returns 403). `subgraph_pattern` and `causal_weight` writes are skipped gracefully.
2. **CloudTrail lag**: `StopInstances` events may not appear in `LookupEvents` within the first few minutes. EC2ASGProbe fallback compensates.
3. **ETL-ASG race condition**: When EC2 is stopped, ASG terminates it quickly. ETL may not capture the `stopped` state before the instance is gone. EC2ASGProbe handles this.
4. **Historical Pod accumulation**: Neptune retains Failed/Succeeded Pods from past deployments. The GC mechanism (`gc.py` in graph-dp-cdk) cleans some, but `Pod→EC2 RunsOn` edges for historical Pods may be stale.
5. **AWS Prober coverage**: Probers cover SQS/DynamoDB/Lambda/ALB/StepFunctions. Other AWS services (e.g., ElastiCache, Kinesis, API Gateway) are not yet covered. Add new probes via `@register_probe` in `collectors/aws_probers.py`.

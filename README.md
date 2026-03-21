# rca_engine — Neptune Graph + AIOps Root Cause Analysis Engine

An AWS Lambda-based AIOps engine that:
1. Receives CloudWatch/SNS alerts (e.g., HTTPCode_Target_5XX_Count > 5)
2. Classifies fault severity (P0/P1/P2)
3. Runs multi-layer RCA: DeepFlow L7/L4 → CloudTrail → **Neptune graph traversal** → EC2 API fallback
4. Generates a Graph RAG report via Bedrock Claude
5. Sends Slack notification with evidence and recommended actions
6. Writes incident to Neptune knowledge base

> **Prerequisite**: The Neptune dependency graph must be built first using the companion [graph-dp-cdk](../graph-dp-cdk/) project.

---

## Architecture

```
CloudWatch Alarm (HTTPCode_Target_5XX_Count > 5)
          │
          ▼
       SNS Topic (petsite-rca-alerts)
          │
          ▼
    handler.py                         ← Lambda entry point
          │
  ┌───────┼──────────────────────────────────────────────────────┐
  ▼       ▼                                                      ▼
core/     core/rca_engine.py                         core/graph_rag_reporter.py
fault_      Multi-layer RCA:                           Bedrock Claude
classifier  1.  DeepFlow L7 (HTTP 5xx call chain)      + Neptune subgraph
  │       1b. DeepFlow L4 (TCP RST/timeout/SYN重传)    + Service→Pod→EC2→AZ path
  │       2.  CloudTrail change events                 + CloudWatch metrics
  │       3.  Neptune graph candidates                 + collectors/infra_collector
  │           ├─ Service call chain (Calls/DependsOn)  + CW Logs sampling
  │           ├─ Infra: graph traversal (q10)          → structured RCA report
  │           └─ Infra: EC2 API fallback
  │       3b. Temporal validation (graph depth × time)
  │       3c. CW Logs sampling (ERROR/FATAL)
  │       4.  Confidence scoring (max 100)
  │             │
  │       neptune/neptune_queries.py  collectors/infra_collector.py
  │       Q1-Q8 (service layer)       real-time Pod status (K8s API)
  │       Q9-Q11 (infra layer)        real-time DB metrics (CloudWatch RDS)
  ▼
actions/playbook_engine.py → actions/semi_auto.py → actions/action_executor.py
(fault playbooks)            (semi-auto)             (kubectl rollout/scale via EKS token)
          │
          ▼
  actions/slack_notifier.py  ← Slack Incoming Webhook + confirmation buttons
  actions/incident_writer.py ← Neptune Incident node + S3 archive + Bedrock KB index
```

---

## Key Design: Two-Layer Root Cause Detection

### Layer 1: Neptune Graph Traversal (preferred)

The graph already contains the full infrastructure chain built by ETL:

```
Microservice ─[RunsOn]→ Pod ─[RunsOn]→ EC2Instance ─[LocatedIn]→ AZ
```

- **Q10** queries all EC2 nodes with `state != 'running'` and reverse-traverses to find affected Pods and Services
- **Q11** expands blast radius: given faulty EC2 IDs, finds ALL impacted services (not just the alerting one)
- Works when ETL has run recently and Neptune has up-to-date `EC2Instance.state`

### Layer 2: EC2 API Fallback (real-time, infra faults only)

Applies **only when the fault is infrastructure-related** (EC2 node failure, AZ outage) and Neptune graph traversal returns nothing due to ETL lag:

1. Queries `describe_instances` with `tag:eks:cluster-name` filter
2. Filters for `stopped/stopping/shutting-down/terminated` states
3. Uses `StateTransitionReason` timestamp to only include recent events (< 30 min)
4. Feeds results back into the same scoring pipeline

This handles the **ASG race condition**: EC2 stop → ASG terminates instance → Neptune loses the node (no `BelongsTo` edge) → EC2 API catches it.

> **Note**: For application-layer faults (DB connection errors, OOM, logic errors) that produce `HTTPCode_Target_5XX_Count` spikes, EC2 nodes remain `running`. In these cases, Layer 2 finds nothing and the RCA relies entirely on DeepFlow L7/L4 signals + CloudTrail + Neptune service call-chain (Layers applied in `rca_engine.py` steps 1–3).

---

## Prerequisites

| Component | Description |
|-----------|-------------|
| **Neptune graph** | Built by [graph-dp-cdk](../graph-dp-cdk/). Microservice, Pod, EC2Instance, AZ nodes; `Calls`, `DependsOn`, `RunsOn`, `LocatedIn` edges. ETL runs every 15 min. |
| **EKS cluster** | Target Kubernetes cluster. Lambda needs `eks:DescribeCluster`. |
| **DeepFlow / ClickHouse** | eBPF observability. `l7_flow_log` (HTTP 5xx) and `l4_flow_log` (TCP RST/timeout/SYN retrans) tables. |
| **Bedrock** | Claude Sonnet (`bedrock:InvokeModel`) + Knowledge Base (`bedrock-agent-runtime:Retrieve`). |
| **Slack** | Incoming Webhook URL stored in SSM Parameter Store. |
| **IAM Role** | Lambda execution role needs: `neptune-db:*`, `eks:DescribeCluster`, `cloudtrail:LookupEvents`, `cloudwatch:GetMetricData`, `logs:*`, `ssm:GetParameter*`, `bedrock:InvokeModel`, `bedrock-agent-runtime:Retrieve`, `ec2:DescribeInstances`, `rds:Describe*`, `autoscaling:DescribeAutoScalingGroups`. |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REGION` | No | `ap-northeast-1` | AWS region |
| `NEPTUNE_ENDPOINT` | **Yes** | — | Neptune cluster endpoint hostname |
| `NEPTUNE_PORT` | No | `8182` | Neptune port |
| `EKS_CLUSTER_NAME` | **Yes** | — | EKS cluster name (used by infra_collector + EC2 API fallback) |
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
  +40  Graph traversal or EC2 API finds non-running EC2 nodes
       (single-AZ concentration noted in evidence)

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
| _(root)_ | `config.py` | Canonical K8s deployment ↔ Neptune service name mapping |
| **core/** | `rca_engine.py` | Multi-layer RCA: DeepFlow L7/L4 + CloudTrail + Neptune graph + EC2 API fallback + scoring |
| **core/** | `fault_classifier.py` | Severity grading (P0/P1/P2); auto-execution gate |
| **core/** | `graph_rag_reporter.py` | Graph RAG: Neptune subgraph + infra path + all signals → Claude → structured report |
| **neptune/** | `neptune_client.py` | Neptune HTTP client with IAM SigV4 signing |
| **neptune/** | `neptune_queries.py` | Neptune openCypher queries Q1–Q11 (service + infrastructure layer) |
| **collectors/** | `infra_collector.py` | Real-time Pod status (K8s API) + DB metrics (CloudWatch RDS) |
| **collectors/** | `eks_auth.py` | Shared EKS bearer token generation (SigV4 presigned STS URL) |
| **actions/** | `action_executor.py` | kubectl operations: rollout restart/undo, scale replicas |
| **actions/** | `playbook_engine.py` | Fault playbook matching (4 predefined patterns) |
| **actions/** | `semi_auto.py` | P1/P2 semi-automatic execution; Slack confirmation flow |
| **actions/** | `slack_notifier.py` | Slack message formatting + Incoming Webhook delivery |
| **actions/** | `incident_writer.py` | Neptune Incident node + S3 archive + Bedrock KB indexing |
| **data/** | `service-db-mapping.json` | Service → DB cluster mapping |
| **scripts/** | `scan-service-db-mapping.py` | Scans K8s Deployments to discover service→DB relationships |

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
│   ├── rca_engine.py           # Multi-layer RCA engine (705 lines)
│   ├── fault_classifier.py     # P0/P1/P2 severity grading
│   └── graph_rag_reporter.py   # Bedrock Claude Graph RAG report
├── neptune/                    # Graph database layer
│   ├── neptune_client.py       # SigV4-signed HTTP client
│   └── neptune_queries.py      # Q1-Q11 openCypher queries
├── collectors/                 # Real-time data collection
│   ├── infra_collector.py      # K8s Pod status + RDS metrics
│   └── eks_auth.py             # EKS bearer token generation
├── actions/                    # Execution & notification
│   ├── action_executor.py      # kubectl rollout/scale operations
│   ├── playbook_engine.py      # Fault playbook matching
│   ├── semi_auto.py            # Semi-automatic execution flow
│   ├── slack_notifier.py       # Slack webhook delivery
│   └── incident_writer.py      # Neptune + S3 + Bedrock KB
├── data/
│   └── service-db-mapping.json # Service → DB cluster mapping
├── scripts/
│   └── scan-service-db-mapping.py
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

## Design Documents

- [`docs/TDD-fault-recovery-rca.md`](./docs/TDD-fault-recovery-rca.md) — Technical Design Document
- [`docs/RCA-SYSTEM-DOC.md`](./docs/RCA-SYSTEM-DOC.md) — System document with resource inventory

---

## Known Limitations

1. **Neptune write permission**: RCA Lambda has read-only access to Neptune (openCypher write returns 403). `subgraph_pattern` and `causal_weight` writes are skipped gracefully.
2. **CloudTrail lag**: `StopInstances` events may not appear in `LookupEvents` within the first few minutes. EC2 API fallback compensates.
3. **ETL-ASG race condition**: When EC2 is stopped, ASG terminates it quickly. ETL may not capture the `stopped` state before the instance is gone. EC2 API fallback handles this.
4. **Historical Pod accumulation**: Neptune retains Failed/Succeeded Pods from past deployments. The GC mechanism (`gc.py` in graph-dp-cdk) cleans some, but `Pod→EC2 RunsOn` edges for historical Pods may be stale.
 stale.

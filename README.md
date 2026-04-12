[中文文档](./README_CN.md) | English

# Graph Dependency Platform

A production-grade **Observability → Knowledge Graph → Intelligent RCA → Chaos Validation → DR Planning** closed-loop platform for microservices on AWS EKS.

Built around [PetSite](https://github.com/aws-samples/one-observability-demo) — a polyglot microservice application running on EKS (ARM64 Graviton3) — the platform continuously builds a Neptune knowledge graph from live traffic and infrastructure topology, performs AI-powered root cause analysis when alerts fire, validates system resilience through AI-driven chaos experiments, and generates graph-driven disaster recovery switchover plans.

---

## Platform Architecture

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                        PetSite on AWS EKS                                            │
│            petsite / petsearch / payforadoption / petfood / ...                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
      eBPF / AWS API / CFN / EventBridge                        CW Alarm → SNS
                    ↓                                                  ↓
┌─────────────────────────┐          ┌──────────────────────┐   ┌─────────────────────────┐
│  infra/ (CDK + ETL)     │          │                      │   │ rca/                    │
│  4 Lambda ETL pipelines │          │   Amazon Neptune     │   │ CW Alarm triggered      │
│  * DeepFlow    every 5m │ =write=> │   (openCypher)       │   │ Multi-layer RCA engine  │
│  * AWS APIs    every 15m│          │                      │=> │ DeepFlow + CloudTrail   │
│  * EventBridge realtime │          │   23 node types      │<= │ 6 Layer2 probers        │
│  * CloudFormation daily │          │   19 edge types      │   │ Graph RAG report→Slack  │
└─────────────────────────┘          │                      │   └─────────────────────────┘
                                     │                      │          │
                                     │                      │ RCA root cause events write back
                                     │                      │          ↓ (next hypothesis round)
                                     │                      │  ┌─────────────────────────┐
                                     │                      │  │ chaos/                  │
                                     │                      │  │ AI hypothesis (graph)   │
                                     │                      │=>│ 61 fault types × 2 back │
                                     │                      │<=│ LearningAgent loop      │
                                     │                      │  └─────────────────────────┘
                                     │                      │          │
                                     │                      │          │ Coverage writes back
                                     │                      │          ↓ (DR plans ref gaps)
                                     │                      │  ┌─────────────────────────┐
                                     │                      │  │ dr-plan-generator/      │
                                     │                      │  │ Dep tree + crit. path   │
                                     │                      │=>│ 7-phase ordered plan    │
                                     └──────────────────────┘  │ 3-level verify + policy │
                                                               └─────────────────────────┘

==========================================================================================
  profiles/ + shared/   Unified environment config & service registry (all modules)
==========================================================================================
```

<table style="border-collapse:collapse; width:100%; margin:1.5em 0; font-size:0.9em;">
  <tr>
    <td colspan="4" style="text-align:center; background:#f5f5f5; font-weight:bold; font-size:1.05em; padding:12px; border:1px solid #ddd;">
      Microservice Resilience Platform · Four-Layer Capability Overview
    </td>
  </tr>
  <tr>
    <td style="font-weight:bold; padding:10px 12px; border:1px solid #ddd; width:25%;">Continuous Awareness: Real-time Knowledge Graph</td>
    <td style="font-weight:bold; padding:10px 12px; border:1px solid #ddd; width:25%;">During Incident: Fast Root Cause Analysis</td>
    <td style="font-weight:bold; padding:10px 12px; border:1px solid #ddd; width:25%;">Post-Incident: Proactive Resilience Validation</td>
    <td style="font-weight:bold; padding:10px 12px; border:1px solid #ddd; width:25%;">Emergency: Ordered DR Execution</td>
  </tr>
  <tr>
    <td style="padding:8px 12px; border:1px solid #ddd;"><b>infra/</b><br>graph-dp-cdk</td>
    <td style="padding:8px 12px; border:1px solid #ddd;"><b>rca/</b><br>graph-rca-engine</td>
    <td style="padding:8px 12px; border:1px solid #ddd;"><b>chaos/</b><br>graph-driven-chaos</td>
    <td style="padding:8px 12px; border:1px solid #ddd;"><b>dr-plan-generator/</b></td>
  </tr>
  <tr>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Real-time dependency graph</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Automated root cause analysis</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Active fault injection</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Ordered switchover plan</td>
  </tr>
  <tr>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Blast radius analysis</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Intelligent alert aggregation</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ RCA accuracy validation</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ 3-level verification engine</td>
  </tr>
  <tr>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Change impact assessment</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Sub-minute MTTR</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ DR plan continuous validation</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Policy-driven customization</td>
  </tr>
  <tr>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Dependency drift detection</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Graph RAG reports</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Weak dependency discovery</td>
    <td style="padding:6px 12px; border:1px solid #ddd;">▶ Critical path RTO optimization</td>
  </tr>
</table>

### Data Flow

1. **infra/** — Modular ETL pipeline continuously ingests infrastructure topology into Neptune (171+ nodes, 19 edge types)
2. Real or injected faults trigger CloudWatch Alarms → **rca/** Lambda activates
3. **rca/** runs multi-layer analysis (DeepFlow L7/L4 + CloudTrail + Neptune graph traversal + Layer2 AWS Service Probers) → Graph RAG report via Bedrock Claude
4. **chaos/** HypothesisAgent generates hypotheses from Neptune graph → 5-Phase experiment engine injects faults → validates RCA accuracy → LearningAgent feeds results back
5. **dr-plan-generator/** queries Neptune graph to generate ordered, executable DR switchover plans with three-level verification (dry-run → step-by-step → full rehearsal), policy-driven customization, Phase 2.5 readiness gates, and rollback instructions
6. **profiles/** + **shared/** provide centralized environment configuration — all modules load service mappings, K8s namespaces, and resource identifiers from a single YAML profile instead of hardcoded values

---

## Repository Structure

```
graph-dependency-platform/
├── infra/          # Infrastructure & Data Layer (formerly graph-dp-cdk)
│   ├── bin/        #   CDK app entry point
│   ├── lib/        #   CDK stacks (NeptuneClusterStack + NeptuneEtlStack)
│   ├── lambda/     #   ETL Lambda functions (etl_aws / etl_deepflow / etl_cfn / etl_trigger)
│   └── _configs/   #   EKS & DeepFlow deployment configs
│
├── rca/            # Intelligent RCA Engine (formerly graph-rca-engine)
│   ├── core/       #   Multi-layer RCA engine + fault classifier + Graph RAG reporter
│   ├── neptune/    #   Neptune query library (Q1–Q18, openCypher) + NL query engine + schema prompt
│   ├── collectors/ #   Layer2 AWS Service Probers + infra collector + EKS auth
│   ├── actions/    #   Playbook engine + semi-auto remediation + Slack notifier
│   ├── search/     #   S3 Vectors incident semantic search
│   ├── scripts/    #   CLI tools (graph-ask.py — NL graph queries)
│   └── deploy.sh   #   Lambda deployment script
│
├── chaos/          # AI-Driven Chaos Engineering (formerly graph-driven-chaos)
│   └── code/
│       ├── agents/       # HypothesisAgent + LearningAgent
│       ├── runner/       # 5-Phase experiment engine + FIS/ChaosMesh backends
│       ├── experiments/  # Experiment YAML templates (tier0 / tier1 / fis)
│       ├── infra/        # FIS IAM + alarm setup
│       ├── neptune_sync.py  # Neptune sync: ChaosExperiment node + TestedBy edge (NEW)
│       └── fmea/         # FMEA failure mode analysis
│
├── dr-plan-generator/  # Graph-Driven DR Plan Generator
│   ├── graph/          #   Neptune queries (Q12–Q16) + dependency analyzer
│   ├── planner/        #   Plan generation (Phase -1 to 4 + Phase 2.5) + rollback
│   ├── registry/       #   ★ Policy system (YAML/Markdown/NL rules/CLI)
│   ├── assessment/     #   Impact analysis + RTO estimation + SPOF detection
│   ├── validation/     #   ★ 3-level verification engine (dry-run/step/rehearsal)
│   ├── output/         #   Markdown / JSON / LLM summary renderers
│   ├── examples/       #   Pre-generated example plans (AZ + Region)
│   └── AGENT.md        #   Universal AI agent instructions
│
├── profiles/       # ★ Environment profiles (multi-app support)
│   ├── profile_loader.py   # EnvironmentProfile loader
│   └── petsite.yaml        # PetSite application profile
│
├── shared/         # ★ Shared modules across all subsystems
│   └── service_registry.py # Centralized service name mapping
│
├── tests/          # Cross-module integration & regression tests
│
└── demo/           # Streamlit demo dashboard
```

---

## 1. infra/ — Infrastructure & Data Layer

**CDK full-stack deployment + modular Neptune ETL pipeline.**

Deploys all AWS resources (EKS, DeepFlow, Neptune, ALB, Lambda) via TypeScript CDK and runs a multi-source ETL pipeline that builds a live knowledge graph in Amazon Neptune.

### ETL Data Sources

| Source | Lambda | Schedule | What it captures |
|--------|--------|----------|-----------------|
| DeepFlow / ClickHouse | `neptune-etl-from-deepflow` | Every 5 min | L7 service call topology, latency, error rates |
| AWS APIs | `neptune-etl-from-aws` | Every 15 min | Static infra topology (EC2, EKS, ALB, RDS, Lambda, SQS, S3 …) |
| CloudFormation | `neptune-etl-from-cfn` | Daily + on deploy | Declared dependency edges (`DependsOn`, env var refs) |
| AWS Events | `neptune-etl-trigger` | Real-time (SQS) | Incremental sync on infra change events |

### Neptune Graph Schema

**23 vertex types** — Region, AZ, VPC, Subnet, EC2, EKS, K8s Service, Pod, ALB, TargetGroup, Lambda, StepFunction, DynamoDB, RDS, Neptune, S3, SQS, SNS, ECR, SecurityGroup, Microservice, BusinessCapability, **ChaosExperiment**

**19 edge types** — LocatedIn, BelongsTo, Contains, RunsOn, RoutesTo, ForwardsTo, HasRule, HasSG, Invokes, AccessesData, ConnectsTo, WritesTo, PublishesTo, TriggeredBy, Calls, DependsOn, Implements, **TestedBy**, **MentionsResource**

### Modular ETL Architecture

The original 2,124-line monolithic ETL was refactored into a plugin-based collector architecture:

```
lambda/etl_aws/
├── handler.py           # Entry point + orchestration
├── config.py            # Centralised configuration
├── neptune_client.py    # Neptune HTTP client (idempotent upsert)
├── business_layer.py    # Business topology construction
├── cloudwatch.py        # CloudWatch metrics collection
├── graph_gc.py          # Graph garbage collection (stale node cleanup)
└── collectors/          # Independent per-resource collectors
    ├── ec2.py / eks.py / alb.py / rds.py / data_stores.py / lambda_sfn.py
```

### Key Technical Highlights

- **ARM64 full-stack**: EKS + DeepFlow on Graviton3, ~40% cost reduction
- **eBPF zero-instrumentation**: DeepFlow Agent as DaemonSet, no application code changes
- **CDK Infrastructure as Code**: VPC → Neptune Cluster → Lambda, fully reproducible
- **Event-driven sync**: SQS-buffered EventBridge rules for near-real-time graph updates

📖 **Detailed docs**: [`infra/README.md`](infra/README.md)

---

## 2. rca/ — Intelligent Root Cause Analysis Engine

**CloudWatch Alarm → multi-layer RCA → Graph RAG report → Slack notification → semi-auto remediation.**

An AWS Lambda that receives CloudWatch/SNS alerts and runs a sophisticated multi-layer root cause analysis pipeline backed by Neptune graph queries and Bedrock Claude.

### Multi-Layer RCA Pipeline

```
CloudWatch Alarm (5XX > threshold)
    │
    ▼ SNS
handler.py → fault_classifier (P0/P1/P2)
    │
    ├─ Step 1:  DeepFlow L7 (HTTP 5xx call chain)
    ├─ Step 1b: DeepFlow L4 (TCP RST / timeout / SYN retrans)
    ├─ Step 2:  CloudTrail change events
    ├─ Step 3:  Neptune graph traversal (Q1–Q18)
    │           ├─ Service call chain (Calls / DependsOn)
    │           ├─ Infrastructure: Service → Pod → EC2 → AZ
    │           └─ Blast radius expansion
    ├─ Step 3b: Temporal validation
    ├─ Step 3c: CloudWatch Logs sampling (ERROR / FATAL)
    ├─ Step 3d: Layer2 AWS Service Probers (parallel)
    │           ├─ SQSProbe / DynamoDBProbe / LambdaProbe
    │           ├─ ALBProbe / StepFunctionsProbe
    │           └─ EC2ASGProbe (fallback)
    └─ Step 4:  Confidence scoring (max 100)
    │
    ▼
Graph RAG Report (Bedrock Claude + Neptune subgraph)
    │
    ▼
Slack notification + semi-auto remediation + incident archival
```

### Layer2 AWS Service Probers

A plugin-based probe framework that covers AWS managed service faults beyond Neptune's topology:

| Probe | Detects | Score Delta |
|-------|---------|-------------|
| **SQSProbe** | Queue backlog, DLQ accumulation | +20–30 |
| **DynamoDBProbe** | Read/Write throttling, system errors | +20–30 |
| **LambdaProbe** | Error rate, throttles, duration near timeout | +20–30 |
| **ALBProbe** | ELB 5XX, rejected connections, unhealthy targets | +20–30 |
| **StepFunctionsProbe** | Execution failures, timeouts, throttles | +20–30 |
| **EC2ASGProbe** | Non-running instances, ASG capacity anomaly | +20–30 |

Adding a new probe requires zero changes to the core engine — just implement `BaseProbe` with `@register_probe`:

```python
@register_probe
class MyServiceProbe(BaseProbe):
    def is_relevant(self, signal, affected_service): ...
    def probe(self, signal, affected_service) -> Optional[ProbeResult]: ...
```

### Severity Classification & Response

| Severity | Trigger | Response |
|----------|---------|----------|
| **P0** | Tier0 service + high error rate | Diagnose-first, human confirmation required |
| **P1** | Tier0/1 + moderate impact | Suggest mode + Slack button confirmation |
| **P2** | Tier1/2 + low impact | Low-risk actions auto-execute |

### Neptune Query Library (Q1–Q18)

| Query | Purpose | Layer |
|-------|---------|-------|
| Q1–Q8 | Blast radius, Tier0 status, upstream deps, service info, incidents, Pod status, DB connections, full dependency subgraph | Service |
| Q9–Q11 | Service → Pod → EC2 → AZ path, non-running EC2 detection, cross-service blast radius | Infrastructure |
| Q17 | Historical incidents that mention the same resource (`MentionsResource` edge) | Unstructured |
| Q18 | Chaos experiment history for a service (`TestedBy` edge) | Unstructured |

### Natural Language Graph Queries

Ask questions in natural language; the NL query engine translates them to openCypher via Bedrock Claude, executes against Neptune, and returns results with a Chinese-language summary.

```bash
cd rca
python3 scripts/graph-ask.py "petsite 的所有下游依赖有哪些？"
python3 scripts/graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
python3 scripts/graph-ask.py "最近发生了几次 P0 故障？"
```

- **`rca/neptune/schema_prompt.py`** — Hard-coded graph schema + 6 few-shot examples as LLM prompt
- **`rca/neptune/nl_query.py`** — `NLQueryEngine`: LLM → openCypher → execute → summarise
- **`rca/neptune/query_guard.py`** — Safety: blocks write keywords, limits hop depth, enforces `LIMIT`

### Semantic Incident Search (S3 Vectors)

RCA reports are chunked, embedded (Bedrock Titan v2), and stored in an S3 Vectors index. During the next RCA, semantically similar historical incidents are retrieved and injected into the Graph RAG context.

- Cost: **< $0.02/month** (vs OpenSearch Serverless ~$30+/month)
- Implementation: `rca/search/incident_vectordb.py`

📖 **Detailed docs**: [`rca/README.md`](rca/README.md)

---

## 3. chaos/ — AI-Driven Chaos Engineering Platform

**AI hypothesis generation → 5-Phase experiment engine → dual backend (Chaos Mesh + AWS FIS) → closed-loop learning.**

A chaos engineering platform that uses Neptune graph topology and Bedrock LLM to automatically generate, execute, and learn from chaos experiments.

### AI Agent System

#### HypothesisAgent
- **Input**: Neptune graph topology + TargetResolver live snapshot + existing experiment coverage
- **Engine**: Bedrock LLM (Claude) — auto-adapts parameters based on replica count, node distribution, resource types
- **Output**: `hypotheses.json` with priority scoring (business_impact × blast_radius × feasibility × learning_value)

#### LearningAgent
- **Input**: DynamoDB experiment history + Neptune graph
- **Analysis**: Per-service stats, failure pattern identification, coverage gap analysis, trend detection
- **Output**: `learning_report.md` + hypothesis library updates + Neptune graph property updates

#### Orchestrator
- Batch execution: sequential / parallel modes
- 4 strategies: `by_tier` / `by_priority` / `by_domain` / `full_suite`
- Tag filtering, cooldown, fail-fast support

### 5-Phase Experiment Engine

| Phase | Name | Action |
|-------|------|--------|
| 0 | Pre-flight | Target resolution, health check, residual experiment detection |
| 1 | Steady State Before | Baseline collection (success rate + P99 latency via DeepFlow) |
| 2 | Fault Injection | Chaos Mesh CRD or FIS experiment |
| 3 | Observation | 10s sampling interval, stop-condition guardrails, auto circuit-break |
| 4 | Recovery | Wait for recovery, poll status (300s timeout) |
| 5 | Report | Markdown report + DynamoDB + LLM analysis + CloudWatch Metrics |

### Dual Backend Architecture

| Backend | Coverage | Fault Types |
|---------|----------|-------------|
| **Chaos Mesh** | K8s layer | 24 verified types (Pod kill, network, HTTP, DNS, IO, CPU, memory, time, kernel) |
| **AWS FIS** | AWS managed services | 15 types (Lambda, RDS, EKS node, EBS, VPC network) |

### Industry Alignment

| Reference | Key Borrowing | Our Differentiation |
|-----------|--------------|-------------------|
| Fidelity Chaos Buffet | Safety guardrails, template library, maturity model | DeepFlow eBPF observability (deeper) |
| Capital One FMEA | RPN risk scoring `S×O×D` | Neptune knowledge graph (natural FMEA input) |

### CloudWatch Metrics

Each experiment publishes to the `ChaosEngineering` custom namespace:

- `ExperimentDuration`, `RecoveryTime`, `MinSuccessRate`, `MaxLatencyP99`, `DegradationRate`
- `ExperimentCount`, `ExperimentPassed` — dimensions: Service / FaultType / Status
- `PhaseDuration` per phase — dimensions: ExperimentId / Phase

📖 **Detailed docs**: [`chaos/code/README.md`](chaos/code/README.md)

---

## 4. dr-plan-generator/ — Graph-Driven DR Plan Generator

**Neptune graph → dependency analysis → ordered switchover plan → 3-level verification → policy customization → rollback → chaos export.**

Automatically generates phased, executable disaster recovery switchover plans with three-level verification, policy-driven customization, and Phase 2.5 readiness gate.

### Switchover Phases

| Phase | Name | Actions |
|-------|------|---------|
| -1 (opt) | Switchover Decision Trigger | CloudWatch alarms, 5XX rate, AWS Health events, human confirmation |
| 0 | Pre-flight Check | Target connectivity, replication lag, DNS TTL |
| 1 | Data Layer | RDS/Aurora failover, DynamoDB Global Table switch |
| 2 | Compute Layer | EKS workload scale-up (by Tier), Lambda, health checks |
| **2.5** | **Target Readiness Gate** | **5 checks: Tier0 replicas, ALB/NLB health, data connectivity, synthetic E2E, capacity — hard block** |
| 3 | Network Layer | ALB health, Route 53 DNS switch, CDN origin |
| 4 | Validation | End-to-end verification, performance baseline comparison |

### Three-Level Verification

| Level | Risk | What |
|-------|------|------|
| L1 Dry-Run | Zero | Variables, resources, IAM, state, network, freshness, contexts |
| L2 Step-by-Step | Low | Execute → validate → rollback (3 strategies) |
| L3 Full Rehearsal | Medium | End-to-end with Phase 2.5 hard-block and auto-rollback |

### Three-Tier Policy System

- **Layer 1**: `service_types.yaml` — resource type definitions
- **Layer 2**: `plan_policy.yaml` or Markdown policy — persistent customization + NL rules (LLM-parsed)
- **Layer 3**: CLI `--set` overrides — highest priority, one-time

### Key Capabilities

- **Topological Sort** (Kahn's algorithm): correct dependency order within each layer
- **Phase 2.5 Readiness Gate**: 5 hard-block checks before traffic cutover
- **Policy-Driven**: per-phase approval, parallelism, timeouts, resource overrides
- **NL Business Rules**: write rules in Chinese/English, LLM parses to structured rules
- **Every Step Has Rollback**: no step without a rollback command
- **Environment Profile**: all service mappings from `profiles/petsite.yaml`

📖 **Detailed docs**: [`dr-plan-generator/README.md`](dr-plan-generator/README.md)

---

## 5. profiles/ + shared/ — Environment Configuration

**Centralized, profile-driven configuration for multi-application support.**

All modules (rca, chaos, dr-plan-generator, infra ETL) load service name mappings, K8s namespaces, and resource identifiers from a single YAML profile instead of hardcoded values.

### profiles/petsite.yaml

Defines the complete application topology:
- Service catalog: Neptune names, K8s deployments/labels, DeepFlow app names, tiers, types
- Kubernetes config: cluster name, namespace, contexts (source/target)
- DR configuration: default scope, source/target regions, domain, health endpoints
- Infrastructure: alarm prefixes, SSM parameter paths, Neptune endpoints

### shared/service_registry.py

`ServiceRegistry` provides bidirectional lookups:
- Neptune name → K8s deployment / K8s label / DeepFlow app
- K8s deployment → Neptune name
- Service type, tier, and alias resolution

### Profile Migration Status

All modules now load from profile with hardcoded fallback:
- `rca/config.py` — service name mappings
- `chaos/code/runner/config.py` — K8s label and deployment mappings
- `rca/neptune/schema_prompt.py` — few-shot examples, service names
- `infra/lambda/etl_aws/collectors/eks.py` — K8S_SVC_ALIAS
- `dr-plan-generator/planner/` — all three planners

📖 **Detailed docs**: See `profiles/petsite.yaml` and `shared/service_registry.py`

---

## Key Metrics & Achievements

| Metric | Value |
|--------|-------|
| Neptune knowledge graph nodes | **171+** |
| Neptune node types | **23** |
| Neptune edge types | **19** |
| Neptune query library | **Q1–Q18 (18 queries)** |
| DR verification levels | **3** (dry-run → step → rehearsal) |
| DR plan phases | **7** (Phase -1 to 4 + Phase 2.5) |
| Policy system layers | **3** (YAML → Markdown/NL → CLI) |
| Chaos Mesh validated tools | **30** |
| AWS FIS fault types | **15** |
| Layer2 AWS Service Probers | **6** |
| RCA trigger latency | **< 1 min** |
| ETL sync cadence | 5min (DeepFlow) + 15min (AWS) + real-time (events) |
| Functional test pass rate | **47/47** |
| Cross-module integration tests | **47+** |
| Architecture version | **v18** |

---

## Core AWS Resources

| Resource | Identifier | Purpose |
|----------|-----------|---------|
| EKS Cluster | `petsite-cluster` (ARM64 Graviton3) | Microservice runtime |
| Neptune | `petsite-neptune` | Knowledge graph storage |
| Lambda | `petsite-rca-engine` | RCA main engine |
| Lambda | `petsite-rca-interaction` | Slack button callback |
| Lambda (×4) | `neptune-etl-from-*` | ETL pipeline |
| Bedrock | Claude Sonnet 4.6 | Graph RAG reports + chaos LLM analysis |
| Bedrock KB | `0RWLEK153U` | Historical incident knowledge base |
| S3 Vectors | `gp-incident-kb` | Semantic incident search index |
| DynamoDB | `chaos-experiments` | Chaos experiment history |
| S3 | `petsite-rca-incidents-*` | Incident archive |
| Region | `ap-northeast-1` (Tokyo) | Primary deployment |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| AWS Account | With VPC, EKS cluster, DeepFlow/ClickHouse deployed |
| Amazon Neptune | Created by `NeptuneClusterStack` in `infra/` |
| Node.js ≥ 18 | For CDK CLI |
| Python ≥ 3.12 | For Lambda runtime and chaos CLI |
| AWS CDK v2 | `npm install -g aws-cdk` |
| kubectl ≥ 1.28 | For Chaos Mesh CRD injection |
| Chaos Mesh ≥ 2.6 | Deployed on EKS cluster |

---

## Quick Start

### 1. Deploy Infrastructure (infra/)

```bash
cd infra
npm install

# Configure cdk.json with your VPC, Neptune, ClickHouse, EKS settings
# Deploy Neptune cluster + ETL pipeline
cdk deploy NeptuneClusterStack
cdk deploy NeptuneEtlStack
```

### 2. Deploy RCA Engine (rca/)

```bash
cd rca
cp .env.example .env
# Fill in Neptune endpoint, EKS cluster, ClickHouse host, Bedrock KB ID
bash deploy.sh
```

### 3. Run Chaos Experiments (chaos/)

```bash
cd chaos/code
pip install boto3 pyyaml requests structlog

# Setup FIS infrastructure
python3 infra/fis_setup.py
python3 main.py setup

# AI-generated hypotheses → experiments → learning
python3 main.py auto --max-hypotheses 5 --top 3 --dry-run
```

---

## Design Documents

| Document | Location |
|----------|----------|
| Architecture (v18) | [`infra/_docs/ARCHITECTURE-v18.md`](infra/_docs/ARCHITECTURE-v18.md) |
| Infrastructure TDD | [`infra/_docs/TDD.md`](infra/_docs/TDD.md) |
| RCA System Doc | [`rca/docs/RCA-SYSTEM-DOC.md`](rca/docs/RCA-SYSTEM-DOC.md) |
| Chaos PRD | [`chaos/docs/prd.md`](chaos/docs/prd.md) |
| Chaos TDD | [`chaos/docs/tdd.md`](chaos/docs/tdd.md) |
| Chaos Test Report | [`chaos/docs/test-report-2026-03-20.md`](chaos/docs/test-report-2026-03-20.md) |

---

## History

This monorepo was created by merging three independent projects (full Git history preserved):

| Original Repo | → Directory | Status |
|---------------|-------------|--------|
| [RadiumGu/ETL-Neptune](https://github.com/RadiumGu/ETL-Neptune) | `infra/` | Archived |
| [RadiumGu/graph-rca-engine](https://github.com/RadiumGu/graph-rca-engine) | `rca/` | Archived |
| [RadiumGu/graph-driven-chaos](https://github.com/RadiumGu/graph-driven-chaos) | `chaos/` | Archived |
| [RadiumGu/graph-dependency-managerment](https://github.com/RadiumGu/graph-dependency-managerment) | `infra/` (mirror) | Archived |

---

## License

MIT

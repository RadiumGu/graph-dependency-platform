[中文文档](./README_CN.md) | English

# DR Plan Generator

Graph-driven disaster recovery plan generator — automatically produces ordered, executable, rollback-ready switchover plans from the Neptune knowledge graph.

## Overview

When a Region or Availability Zone fails, switching over dozens of interdependent microservices, databases, queues, and load balancers **in the correct order** is critical. Get it wrong and you risk data inconsistency or cascading failures.

DR Plan Generator solves this by leveraging the Neptune dependency graph (built by the `infra/` ETL pipeline) to:

1. **Analyse** the blast radius of a failure (which services, databases, and resources are affected)
2. **Sort** resources by dependency order using topological sort (Kahn's algorithm)
3. **Generate** a phased switchover plan: Data → Compute → Network
4. **Attach** concrete AWS CLI / kubectl commands, validation checks, and rollback instructions to every step
5. **Detect** single points of failure (SPOF) and estimate RTO/RPO

## Architecture

```
Neptune Knowledge Graph (171+ nodes, 17 edge types)
        │
        ▼
┌─────────────────────────────────────────┐
│           dr-plan-generator              │
│                                          │
│  graph/       → Neptune queries (Q12-Q16)│
│  graph/       → Dependency analysis      │
│                 (topo sort, SPOF, layers) │
│  planner/     → Plan generation          │
│                 (Phase 0-4 + rollback)    │
│  assessment/  → Impact & RTO estimation  │
│  validation/  → Static checks + chaos    │
│  output/      → Markdown / JSON / LLM    │
└─────────────────────────────────────────┘
        │
        ▼
  Executable DR Plan (Markdown + JSON)
  + Rollback Plan
  + Chaos validation experiments
```

## Switchover Phases

Every generated plan follows a strict layered order:

| Phase | Name | Actions |
|-------|------|---------|
| 0 | Pre-flight Check | Target connectivity, replication lag verification, DNS TTL lowering |
| 1 | Data Layer | RDS/Aurora failover, DynamoDB Global Table switch, SQS/S3 endpoint update |
| 2 | Compute Layer | EKS workload scale-up (by Tier order), Lambda verification, health checks |
| 3 | Network Layer | ALB health confirmation, Route 53 DNS switch, CDN origin update |
| 4 | Validation | End-to-end verification, performance baseline comparison, monitoring check |

**Rollback** reverses the order: Network → Compute → Data, with all steps requiring approval.

## Quick Start

### Install

```bash
cd dr-plan-generator
pip install -r requirements.txt
export NEPTUNE_ENDPOINT=<your-neptune-endpoint>
export REGION=ap-northeast-1
```

### Generate a plan

```bash
# AZ-level switchover
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2,apne1-az4

# Region-level switchover
python3 main.py plan --scope region --source ap-northeast-1 --target us-west-2

# With service exclusion
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2 --exclude petfood

# JSON output
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2 --format json
```

### Impact assessment

```bash
python3 main.py assess --scope az --failure apne1-az1
```

### Validate an existing plan

```bash
python3 main.py validate --plan plans/dr-az-xxx.json
```

### Generate rollback plan

```bash
python3 main.py rollback --plan plans/dr-az-xxx.json
```

### Export as chaos validation experiments

```bash
python3 main.py export-chaos --plan plans/dr-az-xxx.json --output ../chaos/code/experiments/dr-validation/
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `plan` | Generate a DR switchover plan |
| `assess` | Run impact assessment for a failure scenario |
| `validate` | Validate an existing plan JSON (exit code 0=pass, 1=fail) |
| `rollback` | Generate rollback plan from an existing plan |
| `export-chaos` | Export plan assumptions as chaos experiment YAMLs |

### Common flags

| Flag | Commands | Description |
|------|----------|-------------|
| `--scope` | plan, assess | `region` / `az` / `service` |
| `--source` | plan | Failure source identifier |
| `--target` | plan | DR target (region/az) |
| `--failure` | assess | Failure source for assessment |
| `--exclude` | plan | Comma-separated services to exclude |
| `--format` | plan, assess, rollback | `markdown` (default) or `json` |
| `--output-dir` | plan, rollback | Output directory (default: `plans/`) |
| `--plan` | validate, rollback, export-chaos | Path to existing plan JSON |
| `--output` | export-chaos | Output directory for experiment YAMLs |

## Key Features

### Topological Sort (Kahn's Algorithm)

Within each layer, resources are sorted by dependency order. Resources with no dependencies on other same-layer resources are grouped for parallel execution.

### Single Point of Failure Detection

Automatically identifies resources deployed in a single AZ that multiple services depend on. These are flagged as ⚠️ warnings in the plan.

### Every Step Has a Rollback

No step is generated without a corresponding rollback command. The rollback plan reverses the phase order and marks all steps as requiring approval.

### Neptune Queries (Q12–Q16)

| Query | Purpose |
|-------|---------|
| Q12 | AZ/Region dependency tree — all resources and their dependency chains |
| Q13 | Data layer topology — all data stores and their dependent services |
| Q14 | Cross-region resources — Global Tables, cross-region replicas |
| Q15 | Critical path — longest Tier0 dependency chain (determines minimum RTO) |
| Q16 | Single point of failure — single-AZ resources with multiple dependents |

## AI Agent Integration

`AGENT.md` provides universal instructions for AI agents (OpenClaw, Claude Code, kiro-cli) to interactively guide users through DR plan generation. See [AGENT.md](./AGENT.md) for details.

## Examples

Pre-generated example plans using PetSite topology:

| Example | Scenario | Services | Steps | RTO |
|---------|----------|----------|-------|-----|
| [AZ switchover](examples/az-switchover-apne1-az1.md) | AZ1 → AZ2+AZ4 | 7 svc / 14 res | 19 + 15 rollback | ~34min |
| [AZ with exclusion](examples/az-switchover-exclude-petfood.md) | AZ1 → AZ2+AZ4 (no petfood) | 6 svc / 13 res | 18 + 14 rollback | ~32min |
| [Region switchover](examples/region-switchover-apne1-to-usw2.md) | Tokyo → US West | 7 svc / 22 res | 28 + 23 rollback | ~55min |

## Project Structure

```
dr-plan-generator/
├── main.py                  # CLI entry point
├── models.py                # Data models (DRPlan, DRPhase, DRStep, ImpactReport)
├── config.py                # Configuration (env vars)
├── AGENT.md                 # Universal AI agent instructions
├── graph/
│   ├── neptune_client.py    # Neptune openCypher + SigV4 client
│   ├── queries.py           # DR queries (Q12–Q16)
│   └── graph_analyzer.py    # Topo sort, layer classification, SPOF, cycle detection
├── planner/
│   ├── plan_generator.py    # Plan generation engine (Phase 0–4)
│   ├── step_builder.py      # Per-resource-type command generation
│   └── rollback_generator.py# Rollback plan generation
├── assessment/
│   ├── impact_analyzer.py   # Impact assessment
│   ├── rto_estimator.py     # RTO/RPO estimation
│   └── spof_detector.py     # Single point of failure detection
├── validation/
│   ├── plan_validator.py    # Static plan validation
│   └── chaos_exporter.py    # Export as chaos experiments
├── output/
│   ├── markdown_renderer.py # Markdown output
│   ├── json_renderer.py     # JSON output
│   └── summary_generator.py # LLM executive summary (Bedrock Claude)
├── examples/                # Pre-generated example plans
├── tests/                   # 70 unit tests
└── docs/
    ├── prd.md               # Product requirements
    └── tdd.md               # Technical design
```

## Testing

```bash
python3 -m pytest tests/ -v
# 70 tests: graph analyzer (32) + step builder (23) + plan validator (15)
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEPTUNE_ENDPOINT` | Yes* | — | Neptune cluster endpoint |
| `NEPTUNE_PORT` | No | `8182` | Neptune port |
| `REGION` | No | `ap-northeast-1` | AWS region |
| `BEDROCK_MODEL` | No | `global.anthropic.claude-sonnet-4-6` | LLM model for summaries |

*Not required when using mock data (examples, tests).

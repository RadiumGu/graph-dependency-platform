# Strands Agents POC & Migration

> **English** | [🌐 中文](./README.md)

> AWS Strands Agents feasibility validation + full-platform migration strategy for graph-dependency-platform.
>
> 📅 2026-04-18 | 🔍 Architecture Review Cat

---

## 📖 Reading Order

### 1. `report.md` — L0 Spike validation results ✅
Per-item verification of the 6 hard constraints for landing Strands in this project. **Conclusion: L1 POC GO (conditional)**.

### 2. `migration-strategy.md` — Official migration strategy
Strangler Fig one-way migration: direct code is frozen → deleted, final state is a single Strands implementation.
5 Phases / 21-27 weeks / 6 modules serialized, with freeze-date + delete-date CI enforcement.

---

## 🗂 Directory Contents

### Strategy documents
- `report.md` — L0 Spike report (required reading)
- `migration-strategy.md` — Migration strategy (read before starting L1)
- `TASK-L1-smart-query.md` — L1 POC task instructions for the programming cat
- `TASK-L2-prompt-caching.md` — L2 Prompt Caching integration task

### POC code (runnable, validates Strands feasibility)
- `spike.py` — Strands Agent POC (3 `@tool` + BedrockModel + guard)
- `baseline.py` — Direct NLQueryEngine parallel run on the same questions
- `golden_questions.yaml` — 3 test questions
- `run_spike.sh` / `run_baseline.sh` — One-liner launch scripts
- `requirements.txt` — Strands dependencies

### Run artifacts (produced by the L0 Spike)
- `spike_result.json` — Full Strands trace (tool-call / latency / summary)
- `baseline_result.json` — Direct-engine comparison data

### Customer-facing reference materials
- `CUSTOMER-REFERENCE-nl-to-graph-query.md` — Recommends Strands Agents as the default build path
- `CUSTOMER-REFERENCE-nl-to-graph-query-2.md` — Both paths compared (Phase 1 direct Bedrock + Phase 2 Strands), for customers who want the trade-off view

### Archive
- `archive/migration-strategy-v2-dual-track.md` — Early dual-track plan (4-option comparison), deprecated; kept for decision audit trail

---

## 🚀 How to Rerun the POC

```bash
cd experiments/strands-poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash run_spike.sh      # Run the Strands version
bash run_baseline.sh   # Run the direct version
```

Requirements:
- AWS credentials (Bedrock + Neptune SigV4)
- `NEPTUNE_ENDPOINT` environment variable (defaults to the petsite cluster)
- Bedrock access in the `ap-northeast-1` region

---

## ⚠️ Notes

1. **Production code is untouched** — This directory is self-contained; it does not pollute `rca/` / `profiles/` / `.env`
2. **Feasibility only** — Not a production implementation; productization starts in the L1 POC
3. **`.venv/` is 248MB** — Already in .gitignore (add it if missing), do not commit
4. **Only one authoritative strategy document** — `migration-strategy.md` is the single source of truth. The archived version is for audit only; **do not** base decisions on it

---

## 🔗 Related Documents

### In this repository
- `tests/golden/petsite.yaml` + `tests/test_golden_accuracy.py` — Smart Query Golden CI (directly reused by the L1 POC)
- `tests/golden/BASELINE-direct.md` / `BASELINE-strands.md` — Current accuracy baselines (both 20/20 = 100%)
- `rca/neptune/nl_query_direct.py` — Direct engine implementation
- `rca/neptune/nl_query_strands.py` — Strands engine implementation
- `rca/engines/base.py` + `factory.py` — Engine abstraction layer
- `profiles/petsite.yaml` — PetSite's configured schema + few-shot + guard rules

### Outside the repository (internal technical documents, located at `~/tech/blog/gp-improve/`)
- `smart-query-accuracy-improvement.md` — Smart Query Wave 1-5 source plan (landed)
- `harness-resilience-testing/improvements.md` — Resilience Score / Probe / GameDay incremental capability list

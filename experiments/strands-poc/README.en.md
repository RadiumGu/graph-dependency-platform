# Strands Agents POC & Migration

> **English** | [🌐 中文](./README.md)

> AWS Strands Agents feasibility validation + full-platform migration strategy for graph-dependency-platform.
>
> 📅 2026-04-18 | 🔍 Architecture Review Cat & Programming Cat

---

## 📖 Reading Order

### 1. `report.md` — End-to-end record of L0 Spike + L1 POC + L2 POC ✅
- §1-6: L0 Spike — all 6 hard constraints verified → *GO*
- §7: L1 POC complete (Direct 20/20 + Strands 20/20, 6 PRs merged to main)
- §8: *L2 Prompt Caching complete (direct 99.6% / strands 90.9% hit ratio, cost down 48% / 62%)*

### 2. `migration-strategy.md` — Official migration strategy
Strangler Fig one-way migration: direct code frozen → deleted, final state is a single Strands implementation.
5 Phases / 21-27 weeks / 6 modules serialized, with freeze-date + delete-date CI enforcement.

### 3. `TASK-L1-smart-query.md` / `TASK-L2-prompt-caching.md` — Task briefs
Detailed L1/L2 specs: hard constraints, acceptance gates, PR decomposition. *Both complete*.

---

## 🗂 Directory Contents

### Strategy documents
- `report.md` — L0/L1/L2 phase roll-up (required reading)
- `migration-strategy.md` — Migration strategy (read before starting a new phase)
- `TASK-L1-smart-query.md` — L1 POC brief (completed 2026-04-18)
- `TASK-L2-prompt-caching.md` — L2 Prompt Caching brief (completed 2026-04-18)

### POC code (runnable)
- `spike.py` — L0 Strands Agent POC (3 `@tool` + BedrockModel + guard)
- `baseline.py` — Direct NLQueryEngine parallel run on the same questions
- `golden_questions.yaml` — 3 smoke questions
- `run_spike.sh` / `run_baseline.sh` — One-liner launch scripts
- `requirements.txt` — Strands dependencies
- *`verify_cache_direct.py`* — L2 Direct-engine cache self-check (3 runs of the same question: write → read → read)
- *`verify_cache_strands.py`* — L2 Strands-engine cache self-check

### Run artifacts
- `spike_result.json` — L0 Strands full trace (tool-call / latency / summary)
- `baseline_result.json` — L0 direct engine comparison data

### Customer-facing reference materials
- `CUSTOMER-REFERENCE-nl-to-graph-query.md` — Recommends Strands Agents as the default starting point; §9.1 *adds Prompt Caching real-measured cost reduction (-48% / -62%)*

### Archive
- `archive/migration-strategy-v2-dual-track.md` — Early dual-track plan (4-option comparison), deprecated; kept for decision audit trail

---

## 🏁 Current Status (2026-04-18)

| Phase | Status | Key deliverables |
|-------|--------|------------------|
| L0 Spike | ✅ done | All 6 hard constraints passed |
| L1 POC | ✅ done | 6 PRs merged, Direct 20/20 + Strands 20/20 baselines |
| L2 Prompt Caching | ✅ done | 4 PRs merged, direct 99.6% / strands 90.9% hit ratio, monthly cost −48% / −62% |
| Phase 2 stability window | 🚧 observing | After 4 weeks, fill `freeze_date` and start Phase 3 |
| Phase 3 bulk migration | ⬜ not started | Hypothesis / Learning / Probers / Chaos / DR — 6 modules |

---

## 🚀 How to Rerun

### L0 Spike (isolated venv)
```bash
cd experiments/strands-poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash run_spike.sh      # Strands version
bash run_baseline.sh   # direct version
```

### L1 Golden CI (production code + host strands)
```bash
cd /home/ubuntu/tech/graph-dependency-platform
RUN_GOLDEN=1 NLQUERY_ENGINE=direct  pytest tests/test_golden_accuracy.py
RUN_GOLDEN=1 NLQUERY_ENGINE=strands pytest tests/test_golden_accuracy.py
```

### L2 cache self-check
```bash
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
  python3 experiments/strands-poc/verify_cache_direct.py
NEPTUNE_ENDPOINT=... python3 experiments/strands-poc/verify_cache_strands.py
```

Requirements: AWS credentials (Bedrock + Neptune SigV4), Bedrock access in `ap-northeast-1`.

---

## ⚠️ Notes

1. *Production code has been modified since L1*: `rca/engines/`, `rca/neptune/nl_query_*.py` are project-owned; this folder only keeps POC scripts.
2. *Strands installed on the host*: `/usr/bin/pip3 install --user --break-system-packages 'strands-agents>=1.36' 'strands-agents-tools>=0.5'`; pip freeze backup at `~/backups/pip-freeze-before-strands-20260418.txt`.
3. *Live Streamlit defaults to strands*: `/etc/systemd/system/streamlit-demo.service.d/nlquery.conf` pins `NLQUERY_ENGINE=strands`.
4. *`.venv/` is 248 MB*: already in .gitignore, do not commit.
5. *Single source of truth*: `migration-strategy.md` is authoritative; `archive/` is for audit only.

---

## 🔗 Related Documents

### In this repository
- `tests/golden/petsite.yaml` + `tests/test_golden_accuracy.py` — Smart Query Golden CI
- `tests/golden/BASELINE-direct.md` / `BASELINE-strands.md` — Latest accuracy + cache hit baselines
- `rca/neptune/nl_query_direct.py` — Direct engine (Wave 1-5 + L2 caching)
- `rca/neptune/nl_query_strands.py` — Strands engine (L1 + L2 caching)
- `rca/neptune/nl_query.py` — Back-compat shim (to be deleted in Phase 4)
- `rca/engines/base.py` / `factory.py` / `strands_common.py` — Engine abstraction layer (Phase 1 foundation)
- `rca/neptune/strands_tools.py` — 3 Strands `@tool` definitions
- `profiles/petsite.yaml` — PetSite configured schema + few-shot + guard rules
- `docs/migration/timeline.md` — 7-module migration timeline
- `scripts/check_migration_deadlines.py` + `.github/workflows/migration-checks.yml` — freeze/delete-date CI

### Outside the repository (internal technical documents, located at `~/tech/blog/gp-improve/`)
- `smart-query-accuracy-improvement.md` — Smart Query Wave 1-5 source plan (landed)
- `harness-resilience-testing/improvements.md` — Resilience Score / Probe / GameDay incremental capability list

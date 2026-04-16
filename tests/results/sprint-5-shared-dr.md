# Sprint 5 — Shared Modules & DR Plan Supplement Test Results

**Run date:** 2026-04-16  
**Files:** `tests/test_18_unit_shared.py`, `tests/test_19_unit_dr_supplement.py`  
**Result:** 10 passed, 0 failed, 0 skipped

---

## test_18_unit_shared.py — Profiles + Service Registry

| ID | Test | Result |
|----|------|--------|
| S5-01 | `profile_loader` — YAML loading and attribute access | PASS |
| S5-02 | `profile_loader` — missing field defaults handling | PASS |
| S5-03 | `service_registry` — service name mapping all directions | PASS |
| S5-04 | `service_registry` — alias resolution | PASS |
| S5-05 | `service_registry` — unknown service name doesn't crash | PASS |

**Coverage notes:**
- S5-01: Verified `name`, `domain`, `health_endpoint`, `k8s_namespace`, DNS TTLs, `alarm_prefix`, `health_check_command` substitution, dotted `get()`, and `get_deployment_name()` fallback against `petsite.yaml`.
- S5-02: Minimal YAML (profile name only) exercises every property default path without raising.
- S5-03: Neptune↔K8s bidirectional lookup, tier, DeepFlow app, CloudWatch config, `all_service_names()`.
- S5-04: Alias → Neptune standard name, K8s deployment → Neptune, tier lookup via alias.
- S5-05: `resolve`, `neptune_to_k8s`, `k8s_to_neptune`, `get_tier` (Tier2 fallback), `get_deepflow_app`, `get_cloudwatch_config` all return safe defaults for unknown names.

---

## test_19_unit_dr_supplement.py — DR Plan Modules

| ID | Test | Result |
|----|------|--------|
| S5-06 | `spof_detector` — SPOF detection from graph topology (mock Neptune) | PASS |
| S5-07 | `rto_estimator` — RTO/RPO estimation logic | PASS |
| S5-08 | `impact_analyzer` — business impact analysis | PASS |
| S5-09 | `plan_validator` — DR plan validation rules | PASS |
| S5-10 | `rollback_generator` — rollback steps generation | PASS |

**Coverage notes:**
- S5-06: `_detect_from_subgraph()` tested directly with mock `ServiceTypeRegistry`; verifies SPOF identification (≥2 dependents + az set + SPOF type), exclusion of single-dependent and no-az nodes.
- S5-07: `estimate_from_subgraph()` validates DEFAULT_TIMES per type (RDSCluster=300s, Microservice=120s, DynamoDBTable=60s) and minimum-1 floor; `estimate()` verifies serial + parallel-group-max + inter-phase gate (60s) accounting. Fixed: gate added per-phase, not between phases.
- S5-08: `ImpactAnalyzer.assess_impact()` with `SPOFDetector` patched at source module (`assessment.spof_detector.SPOFDetector`). Validates tier grouping, business capability extraction, RPO from node types (RDSCluster→5min), HIGH/LOW risk matrix logic.
- S5-09: Covers cycle detection (CRITICAL), ordering violation (CRITICAL), missing rollback (WARNING), empty validation (ERROR), completeness check (WARNING for uncovered resource), old-snapshot freshness (WARNING only, not blocking).
- S5-10: Phase reversal order (compute before data in rollback), `rollback-` prefix on all step IDs, `requires_approval=True`, command sourced from original `rollback_command`, preflight/validation phases excluded from reversal, terminal validation phase always appended.

---

## Issues encountered and fixes

| # | Issue | Fix |
|---|-------|-----|
| 1 | S5-07: `rto_two` asserted as 11 but actual is 10 | Gate added per phase (inside loop), not between phases; corrected arithmetic: (120+90+60)+(300+60)=630s→10min |
| 2 | S5-08: `patch("assessment.impact_analyzer.SPOFDetector")` raised `AttributeError` | SPOFDetector is imported inside the method body; patched source module instead: `assessment.spof_detector.SPOFDetector` |

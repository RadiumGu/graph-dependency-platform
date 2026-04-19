# BASELINE-direct.md — Chaos Runner Direct Engine

> Generated: 2026-04-19
> Engine: direct (ExperimentRunner wrapper)
> Model: N/A (no LLM, delegates to existing runner)

## L1 Golden Results (mock tools, dry_run=True)

| Case | Scenario | Expected | Actual | Time |
|------|----------|----------|--------|------|
| l1-001 | pod-delete happy path | PASSED/ABORTED* | PASSED/ABORTED* | ~54s |
| l1-002 | network-latency stop condition | ABORTED | ABORTED | ~7s |
| l1-003 | FIS aurora-failover | PASSED/ABORTED* | PASSED/ABORTED* | ~54s |
| l1-004 | PolicyGuard deny (petsite-prod) | ABORTED | ABORTED | ~6s |
| l1-005 | empty namespace | SKIPPED | SKIPPED | — |
| l1-006 | protected namespace (kube-system) | PrefightFailure | PrefightFailure | <1s |

*l1-001/l1-003: Direct engine delegates to real ExperimentRunner which checks K8s pods.
Result depends on cluster availability — PASSED if pods exist, ABORTED if not.

**Summary: 5 passed + 1 skipped**

## L2 Integration (petadoptions namespace, dry_run=True)

| Test | Target | Status | Latency |
|------|--------|--------|---------|
| l2-pod-delete | list-adoptions/petadoptions | PASSED | 54.1s |

- PolicyGuard: ALLOW (confidence 98%, rules R002+R009 matched)
- Target resolved: 2 pods found
- Phases completed: phase0 → phase5
- No LLM tokens (Direct engine uses existing runner logic)

## Token Usage

Direct engine does not use LLM calls. Token usage = 0 for all cases.
PolicyGuard calls within Direct engine use the direct PolicyGuard engine.

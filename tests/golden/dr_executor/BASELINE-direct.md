# BASELINE-direct.md — DR Executor Direct Engine

> Generated: 2026-04-20
> Engine: direct (DRPlanVerifier wrapper)

## L1 Golden Results (dry_run=True)

| Case | Scenario | Status | Latency |
|------|----------|--------|---------|
| l1-001 | AZ failover | DryRunReport | 5.3s |
| l1-002 | Region failover | DryRunReport | 3.0s |

**Summary: 2/2 passed ✅**

Direct engine delegates to DRPlanVerifier.dry_run() — no LLM, no token usage.

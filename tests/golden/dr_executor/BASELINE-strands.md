# BASELINE-strands.md — DR Executor Strands Engine

> Generated: 2026-04-20
> Engine: strands (Strands Agent with 8 tools)
> Model: global.anthropic.claude-sonnet-4-6 (Bedrock)
> System prompt: 1211 tokens (cache eligible ✅)

## L1 Golden Results (dry_run=True, mock tools)

| Case | Scenario | Status | Latency | Tool Calls | Key Protocol |
|------|----------|--------|---------|------------|-------------|
| l1-001 | AZ failover (4 steps) | SUCCESS | 52.7s | 12 | 4 phases, all gates, dependency tracking |
| l1-002 | Region failover (5 steps) | SUCCESS | 71.7s | 17 | cross-region health, 2 manual approvals, sequential mutations |

**Summary: 2/2 passed ✅**

## Agent Protocol Adherence

### l1-001 (AZ failover)
- 12 tool calls: execute_step(4) + validate_step(4) + check_gate_condition(4)
- Correct phase order: preflight → L1 → L2 → L3
- Gate types respected: 3× HARD_BLOCK + 1× INFO
- RTO tracked: 205s actual vs 900s estimated (3.4min / 15min)

### l1-002 (Region failover)
- 17 tool calls: check_cross_region_health(1) + execute_step(5) + validate_step(5) + check_gate_condition(4) + request_manual_approval(2)
- Cross-region health check BEFORE first mutation ✅
- Manual approval for Route53 failover + Aurora promote ✅
- Sequential mutations enforced (Aurora before S3) ✅
- RTO: 490s actual vs 1800s estimated (8.2min / 30min)

## Notes
- Token usage extraction returns 0 — Strands metrics.get_summary() path needs investigation
- dry_run=True means all mock tools return success → l1-002 gets SUCCESS not ROLLED_BACK
- Failure injection testing requires custom mock overrides or L2 integration

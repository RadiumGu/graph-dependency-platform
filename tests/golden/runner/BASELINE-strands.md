# BASELINE-strands.md — Chaos Runner Strands Engine

> Generated: 2026-04-19
> Engine: strands (Strands Agent with 7 tools)
> Model: global.anthropic.claude-sonnet-4-6 (Bedrock)
> System prompt: 1169 tokens (cache eligible ✅)

## L1 Golden Results (mock tools, dry_run=True)

| Case | Scenario | Expected | Actual | Latency | Tool Calls | cache_read | cache_write |
|------|----------|----------|--------|---------|------------|------------|-------------|
| l1-001 | pod-delete happy path | PASSED | PASSED | 79.3s | 19 | 95,988 | 7,110 |
| l1-002 | network-latency stop condition abort | ABORTED | ABORTED | 48.6s | 7 | 25,363 | 5,274 |
| l1-003 | FIS aurora-failover | PASSED | PASSED | 100.6s | 27 | 162,836 | 8,849 |
| l1-004 | PolicyGuard deny (petsite-prod) | ABORTED | ABORTED | ~15s | 1 | — | — |
| l1-005 | empty namespace | SKIPPED | SKIPPED | — | — | — | — |
| l1-006 | protected namespace (kube-system) | PrefightFailure | PrefightFailure | <1s | 0 | — | — |

**Summary: 5 passed + 1 skipped**

## L2 Integration (petadoptions namespace, dry_run=True)

| Test | Target | Status | Latency | Tool Calls | cache_read | cache_write |
|------|--------|--------|---------|------------|------------|-------------|
| l2-pod-delete | list-adoptions/petadoptions | PASSED | 72.3s | 13 | 59,565 | 6,462 |

- PolicyGuard: ALLOW (confidence 98%)
- Agent walked through all 6 phases (phase0-phase5)
- 3 observation rounds during 30s fault window
- Correct abort behavior when stop conditions breached (l1-002)

## Token Usage Summary

| Metric | L1 Average | L2 |
|--------|-----------|-----|
| Input tokens | ~20 | 15 |
| Output tokens | ~3,900 | 3,807 |
| Cache read | ~94,729 | 59,565 |
| Cache write | ~7,078 | 6,462 |

## Agent Behavior Notes

- l1-001: 6 observation rounds × (observe + check_stop) + policy + steady_state + inject + recover + collect = 19 tools
- l1-002: Agent correctly detected stop condition breach at Round 1 and immediately aborted + recovered
- l1-003: Agent adapted report format for FIS backend (10 observation rounds for 120s duration)
- l1-004: Agent recognized PolicyGuard DENY and stopped immediately without proceeding
- Cache is active: cache_read grows across sequential runs within Bedrock's 5-min TTL

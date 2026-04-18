# HypothesisAgent Golden Baseline — engine: strands

_Last run: 2026-04-18 15:29:41 UTC_

| Metric | Value |
|--------|-------|
| Total cases | 19 |
| Pass | 16/19 = 84.2% |
| Latency p50 | 88828 ms |
| Latency p99 | 168089 ms |
| Total tokens (approx) | 476517 |
| Cache read tokens | 263311 |
| Cache write tokens | 3607 |
| Avg Cache Hit Ratio | 69.0% |

## Failures

### S016: 不存在的 service（错误分支）
  - ❌ expected error, got 5 hypotheses

### S017: max_hypotheses=1（极小）
  - ❌ failure_domains {'network'} missing any of ['dependencies']

### S018: max_hypotheses=30（接近 Opus 阈值）
  - ❌ engine error: MaxTokensReachedException('Agent has reached an unrecoverable state due to max_tokens limit. For more information see: https://strandsagents.com/latest/user-guide/concepts/agents/agent-loop/#maxtokensreachedexception')

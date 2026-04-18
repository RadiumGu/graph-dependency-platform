# HypothesisAgent Golden Baseline — engine: direct

_Last run: 2026-04-18 13:23:41 UTC_

| Metric | Value |
|--------|-------|
| Total cases | 20 |
| Pass | 18/20 = 90.0% |
| Latency p50 | 26420 ms |
| Latency p99 | 377545 ms |
| Total tokens (approx) | 114588 |
| Cache read tokens | 51072 |
| Cache write tokens | 3192 |
| Avg Cache Hit Ratio | 66.3% |

## Failures

### S008: 无 filter 广度覆盖（系统级）
  - ❌ engine error: Read timeout on endpoint URL: "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/global.anthropic.claude-sonnet-4-6/invoke"
  - ❌ failure_domains set() missing any of ['compute', 'network', 'dependencies', 'resources']

### S018: max_hypotheses=30（接近 Opus 阈值）
  - ❌ engine error: Read timeout on endpoint URL: "https://bedrock-runtime.ap-northeast-1.amazonaws.com/model/global.anthropic.claude-sonnet-4-6/invoke"

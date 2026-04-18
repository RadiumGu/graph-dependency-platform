# Smart Query Golden Baseline — engine: strands

_Last run: 2026-04-18 06:00:41 UTC_

| Metric | Value |
|--------|-------|
| Total cases | 20 |
| Pass (all checks) | 19/20 = 95.0% |
| Feature match | 19/20 = 95.0% |
| Result correctness | 20/20 = 100.0% |
| Latency p50 | 10152 ms |
| Latency p99 | 17777 ms |
| Total tokens (approx) | 0 |

## Failures

### q014: 所有 P0 故障及其根因
- generated cypher: `MATCH (inc:Incident) RETURN inc.id AS incident, inc.severity AS severity, inc.root_cause AS root_cause, inc.affected_service AS service, inc.start_time AS start_time ORDER BY inc.start_time DESC LIMIT 20`
  - ❌ cypher missing substring: 'P0'

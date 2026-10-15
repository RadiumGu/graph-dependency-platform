---
name: dependency-graph-rca
description: >-
  Use this skill when investigating any incident, alarm, latency spike, 5xx /
  error-rate increase, or service degradation in the petsite environment
  (EKS services such as petsite, petsearch, payforadoption, petfood,
  petlistadoptions, pethistory, petstatusupdater; their Lambda, DynamoDB, RDS
  Aurora, S3, SQS, SSM and Step Functions dependencies; and the WaggleAI
  Bedrock AgentCore agents). It tells you to query the `graph-dependency` MCP
  server FIRST to get root-cause candidates (what the failing service depends
  on), blast radius (who depends on it), recent topology changes, and
  chaos-verified evidence for each dependency edge — before reading metrics or
  logs. Also use it for any question of the form "does A depend on B" or
  "what breaks if X fails".
---

# Dependency-graph-first RCA

This Agent Space has a persistent dependency graph (Amazon Neptune) exposed as
the MCP server **`graph-dependency`** (24 read-only, deterministic openCypher
queries — no LLM in the loop, same input gives the same output). Every
dependency edge carries a fault-injection verdict.

**Why query it first:** CloudWatch, X-Ray and CloudTrail tell you *what is
unhealthy*. They do not tell you *which unhealthy thing is the cause*. The graph
gives you the candidate list and the direction in one call, so you check 5–10
specific resources instead of scanning the account.

## Step 0 — Map the alarm to a graph node name

Graph node names are the **physical names** of resources:

| Alarm dimension looks like | Use as node name |
|---|---|
| EKS service / deployment (`petsearch`, `payforadoption`) | the service name as-is |
| DynamoDB table `ServicesEks2-ddbpetadoption7B7CFEC9-…` | the full table name |
| Lambda `ServicesEks2-statusupdaterservicelambdafn…` | the full function name |
| RDS cluster `serviceseks2-databaseb269d8bb-…` | the cluster identifier |

If a call returns an empty result, **do not conclude "no dependencies"** —
first confirm the name with `q4_service_info(service_name=…)`. An empty result
for a misspelled name looks identical to a real empty result.

## Step 1 — Root-cause candidates: what does the failing service depend on?

```
q3_upstream_deps(failed_service="<service>")        # NO kind — see below
```

Returns the nodes the service depends on (**outgoing** edges). If one of them
is failing, it can explain the incident. Each row carries `edge_type`,
`dependency_kind` and `verify_status`.

⚠️ **The name says "upstream" but it returns DEPENDENCIES (outgoing edges),
not callers.** Ignore the name; trust this sentence. Root cause is on outgoing
edges, blast radius on incoming edges. Mixing them up turns victims into
suspects.

⚠️ **Do not pass `kind="live"` here, even though the tool description
suggests it.** Most service → DynamoDB / RDS / S3 / SQS edges are *declared*
(`static`), not runtime-observed — SDK connections and VPC endpoints do not
produce the traffic the observers see. Measured on this graph (2026-10-15):

| Service | `live` data-layer candidates | without `kind` |
|---|---|---|
| petsearch | 0 | 2 |
| payforadoption | 1 | 6 |
| petsite | 2 | 6 |
| petlistadoptions | 1 | 3 |

`live` would hide most data-layer root causes — and the data layer is where
most real root causes are. Read `dependency_kind` on each row instead:
`dynamic` = seen at runtime, `static` = declared.

Then check **only those candidates** in CloudWatch — start with the ones whose
`verify_status` is `confirmed` (proven by fault injection to propagate
failure).

## Step 2 — What changed? (often the fastest answer)

```
q19_topology_changes(service_name="<service>", since_seconds=86400)
```

Dependencies that **appeared or disappeared**. CloudTrail cannot see these —
a dependency disappearing is an absence of traffic, not an API call.
A new dependency right before the incident is a strong lead.

## Step 3 — Blast radius: who else is affected?

```
q1_blast_radius(failed_node="<root-cause candidate>")   # NO kind
```

Returns who depends on that node (**incoming** edges). Use it to:

- **corroborate** a candidate: if the other alarming services are in its blast
  radius, the candidate explains all of them;
- **report impact**: list affected services and business capabilities.

Same warning as Step 1, worse for data stores. Measured 2026-10-15:
the RDS cluster `serviceseks2-databaseb269d8bb-…` has blast radius **0** with
`kind="live"` and **11** without it; `ddbpetadoption` has 4 vs 21.
An RDS incident investigated with `live` would report "nothing affected".

## Step 4 — Weigh each edge by its evidence

For every dependency you use in your conclusion, state its `verify_status`:

| verify_status | Meaning | How to use it |
|---|---|---|
| `confirmed` | Fault injected at the target, the source degraded (`verify_degradation` = strength) | Strong evidence |
| `inconclusive` | Not enough evidence | Mention, mark as unsettled |
| `untested` | Never fault-tested — **most edges, normal** | Usable, but say "not yet verified" |
| `refuted` | Fault injection showed it does not propagate | **Do not use as evidence** |

`refuted` may be 0 — by design an edge seen by any independent observation
source is never refuted. See `references/verification-semantics.md`.

## Step 5 — Infrastructure path when the cause is below the service

```
q9_service_infra_path(service_name="<service>")   # Service → Pod → EC2 → AZ
q10_infra_root_cause(affected_service="<service>") # non-running EC2 → affected services
q7_db_connections(service_name="<service>")
```

## Hard rules

1. **Never invent numbers.** The graph returns dependency facts and verdicts,
   not metric values. Latency, error rate, CPU and connection counts come from
   CloudWatch. If you did not retrieve a number, say "not retrieved".
2. **Cite the query.** Every response carries `_provenance` (query name,
   params, `queried_at`, `graph_contract_version`). When a conclusion rests on
   the graph, name the query.
3. **Zero degradation is not "no dependency".** It is `inconclusive` — the
   fault may not have landed. Zero traffic and healthy look the same.
4. **Edges seen by a single observation source are weak.** Check with
   `q21_observation_source_coverage`; single-source edges historically caused
   85% of false-negative verdicts.
5. **A FIS experiment template existing is not verification.** A template is
   intent; only `q22_edge_verification_verdicts` reports results.

## References

- `references/verification-semantics.md` — verdicts, observation vs.
  intervention layers, which edge types are dependencies
- `references/tool-quick-reference.md` — all 24 tools with parameters and
  when to use each

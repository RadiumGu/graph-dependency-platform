# graph-dependency tool quick reference

All tools are read-only and deterministic (fixed openCypher, no LLM).
Parameters marked `*` are required. Every response carries `_provenance`;
dependency-bearing tools also carry `_verification`.

## Incident RCA — use in this order

| Tool | Params | Answers |
|---|---|---|
| `q3_upstream_deps` | `failed_service*`, `kind` | **Root-cause candidates**: what the failing service depends on (outgoing). Do not pass `kind` — see SKILL.md Step 1 |
| `q19_topology_changes` | `service_name`, `since_seconds` (default 86400), `limit` | Dependencies that appeared / disappeared recently |
| `q1_blast_radius` | `failed_node*`, `kind` | **Blast radius**: who depends on this node (incoming). Returns `{services, capabilities}`. Do not pass `kind` |
| `q22_edge_verification_verdicts` | `service_name`, `status`, `limit` | Fault-injection verdict per edge |
| `q4_service_info` | `service_name*` | Service attributes (tier, fault boundary, AZ, replicas). Also use it to confirm a node name exists |
| `q9_service_infra_path` | `service_name*` | Service → Pod → EC2 → AZ |
| `q6_pod_status` | `service_name*` | Pod status and restart counts as last recorded by ETL — confirm live state in EKS |
| `q7_db_connections` | `service_name*` | Database nodes the service connects to |
| `q10_infra_root_cause` | `affected_service*` | Non-running EC2 → affected services and AZ |
| `q11_broader_impact` | `ec2_ids*` | Given failed EC2 IDs, every affected service |
| `q17_incidents_by_resource` | `resource_name*`, `limit` | Past incidents mentioning the same resource |
| `q5_similar_incidents` | `service_name*`, `limit` | Past **resolved** incidents for the service |
| `q8_log_source` | `service_name*` | Where the service's logs are |
| `q18_chaos_history` | `service_name*`, `limit` | Chaos experiments run against the service |

## Evidence quality

| Tool | Params | Answers |
|---|---|---|
| `q23_verification_coverage` | — | Overall verified ratio; read this before drilling down |
| `q20_dependency_verification` | `service_name`, `stale_after_seconds`, `only_problematic`, `limit` | **Observation** layer: declared-but-not-observed, single-source, stale |
| `q21_observation_source_coverage` | `service_name`, `coverage`, `limit` | Which observers (X-Ray / DeepFlow) saw each edge. Single-source edges are weak evidence |

## Architecture / DR (less common during an incident)

| Tool | Params | Answers |
|---|---|---|
| `q2_tier0_status` | — | All Tier0 services: fault boundary, AZ, replicas |
| `q12_service_dependency_tree` | `service_name*` | Full dependency tree of a service |
| `q12_az_dependency_tree` | `az_name*` | What runs in an AZ — use for AZ-impairment incidents |
| `q13_data_layer_topology` | — | Every data store and the services depending on it |
| `q15_critical_path` | — | Critical path |
| `q16_single_point_of_failure` | — | SPOFs computed by graph algorithm — topology conclusion can be trusted |
| `q14_cross_region_resources` | — | Cross-region replicated resources |

## Common mistakes

| Mistake | Consequence | Instead |
|---|---|---|
| Reading `q3_upstream_deps` as "callers" | Victims reported as root cause | It returns dependencies (outgoing edges) |
| Passing `kind="live"` to q1 / q3 | Most data-layer candidates silently dropped | Omit `kind`; read `dependency_kind` per row |
| Empty result → "no dependencies" | A typo in the node name looks the same | Confirm the name with `q4_service_info` |
| Quoting a latency / error number from memory | Fabricated evidence | Numbers come from CloudWatch, not the graph |
| "There is a FIS template, so it is verified" | Intent mistaken for result | Only `q22` reports results |

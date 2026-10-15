# Verification semantics

## Two different "verification" layers — do not mix them

| Tool | Layer | Question it answers | Key fields |
|---|---|---|---|
| `q20_dependency_verification` | **Observation** | Has this edge been *seen* recently? | `drift_status`, `runtime_verified`, `verified_by` |
| `q22_edge_verification_verdicts` | **Intervention** | When we *broke* the target, did the source degrade? | `verify_status`, `verify_degradation`, `verify_experiment` |

An edge can be "observed every day" and "inconclusive under fault injection"
at the same time. **That contradiction is a valuable signal — report it.**

Start with `q23_verification_coverage` for the overall picture before drilling
into individual edges. A low `verified_ratio` is normal: most edges are
`untested`, and most dependency graphs cannot compute this number at all.

## The four verdicts

| verify_status | Meaning | Use in reasoning |
|---|---|---|
| `confirmed` | Fault injected at the dependency target; the source degraded. `verify_degradation` is the strength | Yes — strong evidence |
| `refuted` | Fault injected; the source did **not** degrade, and no independent source has ever observed the edge | **No** — never use as evidence |
| `inconclusive` | Degradation 5–20 %, insufficient observation traffic, or unknown whether the fault landed | Mention, mark as unsettled |
| `untested` | Never fault-tested. The state of most edges | Usable — but say "this dependency has not been verified" |

### Why `refuted` may be 0

Independent-evidence gate: if an edge has been seen by **any** independent
observation source (e.g. a non-zero DeepFlow call count), it can never be
judged `refuted`. A zero count of refuted edges is the result of that
conservative design, not evidence that verification is not running.

### Zero degradation is not "no dependency"

Zero degradation after injection has two explanations that metrics cannot
tell apart:

- the dependency truly does not propagate failure, or
- the fault never landed.

Both are `inconclusive`. Check `injection_confirmed`; if it is missing, say the
two cannot be distinguished. Likewise, **zero traffic and healthy look
identical in metrics** — also `inconclusive`.

## Which edges are dependencies

Dependency edge types (from the graph contract, version 1):

`AccessesData`, `Calls`, `Delegates`, `DependsOn`, `Invokes`, `InvokesTool`,
`PublishesTo`, `Retrieves`, `RoutesToRuntime`, `RoutesVia`

Edges such as `RunsOn`, `LocatedIn`, `TestedBy`, `OwnedBy` are **placement or
bookkeeping, not dependencies**. They carry no `verify_status`; do not use them
to answer "does A depend on B".

The verdict property is **`verify_status`** (not `verification_status`, which
does not exist).

## Direction

Dependency edges always point **from the dependent to the dependency**:
`petsearch -[AccessesData]-> ddbpetadoption` means petsearch depends on the
table.

- Root-cause candidates = **outgoing** edges → `q3_upstream_deps`
- Blast radius = **incoming** edges → `q1_blast_radius`

`q3_upstream_deps`'s name says "upstream" but it returns outgoing edges
(dependencies). Decide direction by the edge, never by the tool name.

## `kind` parameter

| kind | Edges included | When |
|---|---|---|
| `live` | runtime-observed **and** currently active | Default for incidents — what exists now |
| `dynamic` | runtime-observed, including stale | History |
| `static` | declared in code / CloudFormation | Design intent |
| (omitted) | all | Widest net |

`live` and `static` disagreeing (declared but never observed, or observed but
never declared) is itself a finding worth reporting.

## Data-layer dependencies are often `static`

Many service → DynamoDB / RDS / S3 edges are declared (`static`) rather than
runtime-observed, because SDK connections and VPC endpoints do not generate the
traffic signals the observers rely on. If `kind="live"` returns few data-layer
candidates, **repeat without `kind`** before concluding the data layer is not
involved.

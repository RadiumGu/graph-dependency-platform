#!/usr/bin/env python3
"""M4 端到端验证辅助：掺入平台设施与污染节点后建快照，再检视产出。

刻意只用 argv 传路径，不读任何环境变量。
用法：
  build <fixture.json> <out_snapshot.json>
  inspect <plan.json>
"""

import json
import sys

_PLATFORM_AND_POLLUTION = [
    {"name": "petsite-neptune", "type": "NeptuneCluster", "tier": "Tier1", "state": "available"},
    {"name": "etl_aws", "type": "LambdaFunction", "tier": "Tier2", "state": "Active"},
    {"name": "etl_deepflow", "type": "LambdaFunction", "tier": "Tier2", "state": "Active"},
    {"name": "gateway-service", "type": "Microservice", "namespace": "awesomeshop", "state": "running"},
    {"name": "order-service", "type": "Microservice", "namespace": "awesomeshop", "state": "running"},
    {"name": "artillery", "type": "Microservice", "namespace": "awesomeshop", "state": "running"},
]

_EXTRA_EDGES = [
    {"from": "gateway-service", "to": "order-service", "type": "Calls"},
    {"from": "etl_aws", "to": "petsite-neptune", "type": "WritesTo"},
]

_SUSPECT = (
    "petsite-neptune", "etl_aws", "etl_deepflow",
    "gateway-service", "order-service", "artillery",
)


def build(fixture: str, out_path: str) -> None:
    """把平台设施与污染节点掺进 fixture，模拟一份整区快照。"""
    from graph.snapshot import build_snapshot

    sub = json.load(open(fixture, encoding="utf-8"))
    nodes = sub["nodes"] + _PLATFORM_AND_POLLUTION
    edges = sub["edges"] + _EXTRA_EDGES
    snap = build_snapshot("region", "ap-northeast-1", nodes, edges)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, ensure_ascii=False)
    print(f"  掺入后快照: {snap['node_count']} 节点 / {snap['edge_count']} 边")


def inspect(plan_path: str) -> int:
    """检视计划：进入范围的资源、排除项、以及是否有误入。"""
    plan = json.load(open(plan_path, encoding="utf-8"))
    in_scope = set(plan.get("affected_resources", []))

    print(f"  ── 进入范围 ({len(in_scope)}) ──")
    print("     " + ", ".join(sorted(in_scope)))
    print()
    print("  ── 排除项 ──")
    for exc in plan.get("scope_exclusions", []):
        print("     %-18s %-16s rule=%-22s %s" % (
            exc.get("name", ""), exc.get("type", ""),
            exc.get("rule", ""), exc.get("reason", "")[:40],
        ))
    print()
    leaked = [n for n in _SUSPECT if n in in_scope]
    if leaked:
        print(f"  ✗ 平台/污染节点误入范围: {leaked}")
        return 1
    print("  ✓ 平台设施与污染节点全部出局")
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2], sys.argv[3])
        sys.exit(0)
    sys.exit(inspect(sys.argv[2]))

#!/usr/bin/env python3
"""
refresh_fixtures.py — 从活图谱抓取真实数据，生成离线快照。

目的：让没有 AWS 凭证的访客也能看到**真实数据的快照**，而不是报错页，
也不是编造的假数据。每份快照都带 captured_at 时间戳，界面会明确标注
「离线快照」而非伪装成实时。

用法（需要能访问 Neptune 的环境）：
    cd demo && python3 fixtures/refresh_fixtures.py

只读：本脚本只执行 MATCH 查询，不写图。
"""
import json
import os
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

_FIX_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.dirname(_FIX_DIR)
_ROOT = os.path.dirname(_DEMO_DIR)

# _DEMO_DIR 也要进 sys.path —— 证据链定义在 demo/_common.py，
# 本脚本与 RCA 页面共用它（各抄一份必然漂移，见下方 rca_evidence 段的注释）。
for _p in (_ROOT, os.path.join(_ROOT, "rca"), _DEMO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("REGION", "ap-northeast-1")
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["REGION"])

if not os.environ.get("NEPTUNE_ENDPOINT"):
    sys.exit(
        "NEPTUNE_ENDPOINT 未设置。\n"
        "本脚本刻意不内嵌集群端点，请显式传入：\n"
        "  NEPTUNE_ENDPOINT=<cluster-endpoint> python3 fixtures/refresh_fixtures.py"
    )

# 依赖边类型来自契约，不硬编码
DEP_LABELS = ["Calls", "AccessesData", "DependsOn", "Delegates", "InvokesTool", "Retrieves"]
AGENT_LABELS = [
    "AgentRuntime", "AgentTool", "AgentGateway",
    "AgentMemory", "KnowledgeBase", "Guardrail",
]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(name, payload):
    payload["captured_at"] = _now()
    path = os.path.join(_FIX_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
    print(f"  wrote {name}  ({os.path.getsize(path)} bytes)")


def main():
    from neptune import neptune_client as nc

    def q(cypher, **params):
        res = nc.query(cypher, **params) if params else nc.query(cypher)
        return res.get("results", []) if isinstance(res, dict) else res

    print("→ graph_stats")
    nodes = q("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC")
    edges = q("MATCH ()-[e]->() RETURN type(e) AS t, count(*) AS c ORDER BY c DESC")
    _write("graph_stats.json", {
        "nodes_by_label": nodes,
        "edges_by_type": edges,
        "node_total": sum(r["c"] for r in nodes),
        "edge_total": sum(r["c"] for r in edges),
        "node_label_count": len(nodes),
        "edge_type_count": len(edges),
    })

    print("→ verification")
    dep_list = ", ".join(f"'{x}'" for x in DEP_LABELS)
    by_status = q(
        f"MATCH ()-[e]->() WHERE type(e) IN [{dep_list}] "
        "RETURN type(e) AS edge_type, coalesce(e.verify_status,'untested') AS status, "
        "count(*) AS c ORDER BY edge_type, status"
    )
    # 已判定的边取明细（confirmed / refuted / inconclusive）
    detail = q(
        f"MATCH (a)-[e]->(b) WHERE type(e) IN [{dep_list}] "
        "AND e.verify_status IS NOT NULL AND e.verify_status <> 'untested' "
        "RETURN coalesce(a.name, a.arn, 'unknown') AS source, "
        "type(e) AS edge_type, coalesce(b.name, b.arn, 'unknown') AS target, "
        "e.verify_status AS status, e.verify_confidence AS confidence, "
        "e.verify_degradation AS degradation, e.verify_experiment AS experiment, "
        "e.verify_last AS verified_at, e.verify_reason AS reason, "
        "e.verify_evidence_channel AS evidence_channel, e.source AS edge_source "
        "ORDER BY e.verify_status, type(e) LIMIT 200"
    )
    counts = {}
    for row in by_status:
        counts[row["status"]] = counts.get(row["status"], 0) + row["c"]
    total = sum(counts.values())
    decided = counts.get("confirmed", 0) + counts.get("refuted", 0)
    _write("verification.json", {
        "by_edge_type_status": by_status,
        "totals_by_status": counts,
        "dependency_edge_total": total,
        "verified_ratio": round(decided / total, 4) if total else 0.0,
        "decided_edges": detail,
    })

    print("→ agent_graph")
    ag_list = ", ".join(f"'{x}'" for x in AGENT_LABELS)
    agent_nodes = q(
        f"MATCH (n) WHERE labels(n)[0] IN [{ag_list}] "
        "RETURN labels(n)[0] AS label, coalesce(n.name, n.tool_key, n.arn) AS name, "
        "n.arn AS arn ORDER BY label, name LIMIT 100"
    )
    agent_edges = q(
        f"MATCH (a)-[e]->(b) WHERE labels(a)[0] IN [{ag_list}] OR labels(b)[0] IN [{ag_list}] "
        "RETURN labels(a)[0] AS source_label, coalesce(a.name,a.tool_key,a.arn) AS source, "
        "type(e) AS edge_type, labels(b)[0] AS target_label, "
        "coalesce(b.name,b.tool_key,b.arn) AS target, "
        "coalesce(e.verify_status,'untested') AS verify_status, e.source AS edge_source "
        "ORDER BY edge_type LIMIT 200"
    )
    _write("agent_graph.json", {
        "nodes": agent_nodes,
        "edges": agent_edges,
        "node_total": len(agent_nodes),
        "edge_total": len(agent_edges),
    })

    print("→ sample_topology (Graph Explorer 离线用)")
    topo_nodes = q(
        "MATCH (n) WHERE labels(n)[0] IN "
        "['Microservice','LoadBalancer','TargetGroup','DynamoDBTable','RDSCluster',"
        "'SQSQueue','SNSTopic','S3Bucket','LambdaFunction','BusinessCapability',"
        "'AWSServiceEndpoint','AgentRuntime','AgentTool','EKSCluster','StepFunction'] "
        "RETURN labels(n)[0] AS label, coalesce(n.name,n.tool_key,n.arn) AS name "
        "ORDER BY label, name LIMIT 120"
    )
    topo_edges = q(
        "MATCH (a)-[e]->(b) WHERE labels(a)[0] IN "
        "['Microservice','LoadBalancer','TargetGroup','DynamoDBTable','RDSCluster',"
        "'SQSQueue','SNSTopic','S3Bucket','LambdaFunction','BusinessCapability',"
        "'AWSServiceEndpoint','AgentRuntime','AgentTool','EKSCluster','StepFunction'] "
        "AND labels(b)[0] IN "
        "['Microservice','LoadBalancer','TargetGroup','DynamoDBTable','RDSCluster',"
        "'SQSQueue','SNSTopic','S3Bucket','LambdaFunction','BusinessCapability',"
        "'AWSServiceEndpoint','AgentRuntime','AgentTool','EKSCluster','StepFunction'] "
        "RETURN coalesce(a.name,a.tool_key,a.arn) AS source, labels(a)[0] AS source_label, "
        "type(e) AS edge_type, coalesce(b.name,b.tool_key,b.arn) AS target, "
        "labels(b)[0] AS target_label, coalesce(e.verify_status,'') AS verify_status "
        "LIMIT 250"
    )
    _write("sample_topology.json", {
        "nodes": topo_nodes,
        "edges": topo_edges,
    })

    print("→ observation_source_coverage")
    try:
        src = q(
            "MATCH ()-[e]->() WHERE e.source IS NOT NULL "
            "RETURN e.source AS source, type(e) AS edge_type, count(*) AS c "
            "ORDER BY c DESC LIMIT 80"
        )
        _write("edge_sources.json", {"by_source": src})
    except Exception as exc:  # noqa: BLE001
        print(f"  skipped edge_sources: {exc}")

    print("→ rca_evidence（RCA 页离线用：真实证据链）")
    # RCA 页的证据采集全是**纯图查询**（不需要 Bedrock），所以离线时
    # 可以展示真实证据，只有最后那段 LLM 叙述缺席。
    # 刻意不预置 AI 生成的报告文本——那是给 LLM 输出做录像。
    #
    # 证据链从 _common.RCA_EVIDENCE_CHAIN 取，不在这里抄第二份：
    # 原先这里手写了一份 9 条的清单，与页面各自维护。页面加查询而这里没加，
    # 离线模式下对应小节就是空的 —— 而这种缺失没有任何报错，只会让访客以为
    # 「那部分没数据」。方向标注（q1/q3 谁是根因谁是影响面）也曾在两边都是旧的。
    try:
        from neptune.query_catalog import run_query  # type: ignore
        import _common as C  # type: ignore

        out: dict = {}
        for svc in ("petsite", "petsearch", "payforadoption"):
            per_svc: dict = {}
            for qname, why, pmap, stage in C.RCA_EVIDENCE_CHAIN:
                kw = {k: (svc if v is None else v) for k, v in pmap.items()}
                try:
                    r = run_query(qname, **kw)
                    rows = r.get("results", r) if isinstance(r, dict) else r
                    if isinstance(rows, list):
                        rows = rows[:40]
                    per_svc[qname] = {"why": why, "params": kw,
                                      "stage": stage, "data": rows}
                except Exception as exc:  # noqa: BLE001
                    per_svc[qname] = {"why": why, "params": kw,
                                      "stage": stage,
                                      "error": str(exc)[:150]}
            out[svc] = per_svc
            print(f"  {svc}: {len(per_svc)} 条证据查询")
        _write("rca_evidence.json", {"by_service": out})
    except Exception as exc:  # noqa: BLE001
        print(f"  skipped rca_evidence: {exc}")

    print("→ services（服务清单，从图谱现取）")
    try:
        svcs = q(
            "MATCH (n:Microservice) RETURN n.name AS name, n.tier AS tier, "
            "n.az AS az, n.fault_boundary AS fault_boundary, "
            "n.recovery_priority AS recovery_priority ORDER BY n.name"
        )
        _write("services.json", {"services": svcs})
    except Exception as exc:  # noqa: BLE001
        print(f"  skipped services: {exc}")

    print("\n完成。快照已写入 demo/fixtures/")


if __name__ == "__main__":
    main()

"""
provenance.py — 给每个工具响应附上出处，并把证据纪律交给 agent。

为什么这个模块存在（不是可选的装饰）：

## 承重证据：12 次真实调用（2026-09-07，可复现）

同一个问题问 AWS DevOps Agent 12 次，图谱已作 MCP server 关联在 agent space
`petsite-devops` 上、24 条只读查询全部可用：

    不提图谱时会去查图谱的比例    2/8   = 25%
    问题里点名要求查图谱          4/4   = 100%

**工具可用 ≠ 工具会被用。** 没查的那些调用一样成功返回、排版精美、语气自信，
依据是「FIS 实验模板存在」—— 而模板是**意图**，不是**结果**。
查了的那些引用真实实验 ID 与退化幅度，并在证据不足时主动拒绝下结论。

逐字全文、每次的 executionId、耗时与判别信号在
`demo/fixtures/agent_unaided_answer.json`，可用
`aws devops-agent list-pending-messages` 按 executionId 逐条取回核对。
门禁 `tests/test_57_rca_agent_tab.py`。

## 一条未能核实的旧记录（保留，但不作为论据）

此前本文件与 `README.md` 都写着：2026-09-01 在 SAP 系统上做盲发现验证时，
AWS DevOps Agent 从 CloudTrail 挖出了 FIS 实验与发起者（这部分很好），
但同时编造了 CWAgent 的指标值、用一个虚构的 iowait 数字排除了存储瓶颈；
为此在 AGENTS.md v2 里加了最高优先级的 Evidence integrity 规则。

**2026-09-07 复核：这条记录在本仓库无法核实。** 它 2026-09-05 随 `59f41b5`
以散文形式进入仓库，没有随附对话记录、executionId、指标名或那个 iowait 数值；
它引用的 `AGENTS.md v2` 既不在本仓库，`petsite-devops` 里也没有 `agents_md`
资产；`todo/` 下最早的记录是 09-04，没有 09-01 的任何痕迹。它发生在另一个
系统、另一个 agent space。

它可能是真的 —— 但按本项目自己的判据，**一条无法被第三方核对的论断不能放在
承重位置**，那正是本模块要求 agent 不要做的事。所以它降级为标注过的轶事：
说明这套设计的动机从哪来，不用来证明任何结论。

（对照：agent space 里的 `components/neptune-graph-platform` memory 带着
`claim / evidence / source` 结构和 `devopsagent:execution` ID —— 这个标准是
存在且可达的，这条旧记录只是没达到。）

## 本 server 的责任

LLM agent 被问「什么依赖 X」时一定会给出答案，因为它不会说「我不知道」。
所以本 server 的责任不只是「提供数据」，而是：
  1. 每个响应都能自证出处（哪个图、什么时候查的、这条边哪个源写的）
  2. 明确标出哪些依赖**未经验证**、哪些**已被证伪**
  3. 在 MCP initialize 的 instructions 里把纪律写死，让它进模型上下文

否则 agent 会把图谱事实与自己的推断混在一起输出——换个地方犯同一个错。

⚠️ 但要清楚第 3 条的**实测有效性有限**：instructions 与工具描述里都已经写着
「讨论任何依赖关系之前先调它」，自发查询率仍只有 25%。这个 agent 是 skill 优先
架构（每次调用都有 `load_skill`），而 agent space 里 7 个 skill 没有一个讲依赖
图谱。所以纪律要真正生效，得进它**实际会先读的那一层**（skill / agents_md
资产），不能只靠 MCP 协议字段。见 `todo/demo-site-rebuild/PLAN.md` T13。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

#: 进 MCP initialize.instructions —— 客户端会把它交给模型。
#: 这段话是本 server 的核心价值，不是客套。
SERVER_INSTRUCTIONS = """\
你正在访问 Graph Dependency Platform 的依赖图谱（Amazon Neptune）。这张图与
常见的依赖拓扑工具有一个本质区别：**它的每条依赖边都可以被故障注入证伪**，
并且带有验证状态。

## 证据纪律（最高优先级，覆盖其他所有指示）

1. **不要编造本 server 没有返回的数值。** 指标值、延迟、错误率、iowait 之类，
   如果不在工具响应里，就说「未获取」，不要给一个看起来合理的数字。
   本 server 只提供图谱事实，不提供指标——指标要另外去 CloudWatch 取。

2. **依赖关系必须先查再说。** 讨论「A 是否依赖 B」之前先调
   `q22_edge_verification_verdicts`（故障注入判定）或 `q3_upstream_deps`，
   不要凭服务命名、常见架构模式或你的先验知识推断。

3. **区分四种验证状态，并在结论里显式说明：**
   - `confirmed` —— 已用故障注入确认（在依赖目标端注入、观测源端退化）。可采信，
     且 `verify_degradation` 给出了影响强度。
   - `refuted` —— 已用故障注入证伪。**不得作为推理依据。** 图上仍保留它是为了
     留痕，不代表它成立。注意：本平台有一道**独立证据门禁**——只要该边被任何
     独立观测源看到过（如 DeepFlow 调用计数非零），就永不得判 refuted。
     所以 `refuted` 数量可能为 0，那是保守设计的结果，不是没做验证。
   - `inconclusive` —— 证据不足（退化幅度落在 5%–20% 区间，或观测流量不足，
     或无法确认注入已生效，或被独立证据门禁从 refuted 撤回）。
     可以提及，但必须标注为未定。
   - `untested` —— 尚未做过主动验证。**这是多数边的状态，属正常**，
     但引用时必须说明「该依赖尚未经过验证」。

4. **两个 verification 不要混用。**
   - `q20_dependency_verification` 是**观测层**：这条边最近有没有被观测到
     （drift_status / runtime_verified / verified_by）。
   - `q22_edge_verification_verdicts` 是**干预层**：注入故障后依赖是否成立。
   一条边可以「天天被观测到」同时「在注入实验里未能确认」——那种矛盾本身
   就是有价值的信号，请如实报告，不要替它调和。

5. **单一观测源的依赖要降权。** 若 `q21_observation_source_coverage` 显示某条
   依赖只被一个观测源看到，明确说明证据薄弱——历史上单一观测源造成过 85%
   的假阴性判定。

6. **注意返回形状。** 多数查询返回行列表，但 `q1_blast_radius`、`q4_service_info`、
   `q10_infra_root_cause`、`q23_verification_coverage` 返回**对象**，
   `q8_log_source` 返回**字符串**。每个响应的 `result_shape` 字段会告诉你是哪种。

7. **响应里的 `_provenance` 与 `_verification` 字段是给你读的**，请在结论中
   引用具体出处（实验 ID、数据源、查询时间），而不是笼统地说「根据图谱」。

## 建议的调查顺序

1. `q23_verification_coverage` —— 先看整体有多少依赖经过验证
2. `q22_edge_verification_verdicts` —— 弄清具体哪些依赖可信
3. `q3_upstream_deps` —— 找**故障服务依赖的**节点（根因候选，出边方向）
4. `q1_blast_radius` —— 算**依赖故障服务的**节点（影响面，入边方向）
5. `q9_service_infra_path` / `q10_infra_root_cause` —— 落到基础设施层
6. `q16_single_point_of_failure` —— 图算法给出的单点故障
7. `q17_incidents_by_resource` / `q18_chaos_history` / `q5_similar_incidents` —— 历史
8. `q19_topology_changes` —— 最近拓扑是否变过
9. `q20_dependency_verification` / `q21_observation_source_coverage` —— 观测侧对账

全部查询都是确定性的固定 openCypher，只读，同参数同结果。
"""

_VERIFY_KEYS = (
    "verify_status", "verify_confidence", "verify_degradation",
    "verify_experiment", "verify_last", "verify_reason",
)

STATUS_GUIDANCE = {
    "confirmed": "已用故障注入确认，可采信",
    "refuted": "已用故障注入证伪，不得作为推理依据",
    "inconclusive": "证据不足，引用时必须标注为未定",
    "untested": "尚未验证，引用时必须说明",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_provenance(query_name: str, params: dict, contract_version: Any) -> dict:
    ep = os.environ.get("NEPTUNE_ENDPOINT", "")
    # 只暴露集群短名，不回显完整端点（它是内网地址，没必要进 agent 上下文）
    cluster = ep.split(".")[0] if ep else "unknown"
    return {
        "source": "Amazon Neptune openCypher",
        "graph_cluster": cluster,
        "region": os.environ.get("REGION", "unknown"),
        "graph_contract_version": contract_version,
        "query": query_name,
        "params": params,
        "queried_at": _now(),
        "determinism": "固定 openCypher，无 LLM 参与，同参数同结果",
        "caveat": "本 server 只提供图谱事实，不提供指标数值。指标请另查 CloudWatch。",
    }


def summarize_verification(rows: list) -> dict | None:
    """
    从结果行里抽取验证状态分布。
    行里没有 verify_* 字段就返回 None（说明这个查询不涉及依赖边验证）。
    """
    if not isinstance(rows, list) or not rows:
        return None

    found = False
    counts: dict[str, int] = {}
    refuted_samples: list[dict] = []

    for r in rows:
        if not isinstance(r, dict):
            continue
        # 兼容不同查询对同一属性的不同列名
        status = None
        for k in ("verify_status", "verifyStatus", "status_verify"):
            if k in r:
                status = r[k]
                found = True
                break
        if status is None:
            continue
        s = str(status or "untested")
        counts[s] = counts.get(s, 0) + 1
        if s == "refuted" and len(refuted_samples) < 10:
            refuted_samples.append({
                k: r.get(k) for k in ("source", "target", "edge_type", *_VERIFY_KEYS)
                if k in r
            })

    if not found:
        return None

    total = sum(counts.values())
    decided = counts.get("confirmed", 0) + counts.get("refuted", 0)
    out: dict[str, Any] = {
        "by_status": counts,
        "total_with_status": total,
        "verified_ratio": round(decided / total, 4) if total else 0.0,
        "status_guidance": {k: v for k, v in STATUS_GUIDANCE.items() if k in counts},
    }
    if refuted_samples:
        out["refuted_edges"] = refuted_samples
        out["warning"] = (
            f"结果中有 {counts.get('refuted', 0)} 条**已被证伪**的依赖边，"
            "不得作为推理依据。"
        )
    if counts.get("untested"):
        out["note"] = (
            f"结果中有 {counts['untested']} 条依赖边尚未经过主动验证，"
            "引用时必须说明。"
        )
    return out


def wrap_result(
    query_name: str,
    params: dict,
    rows: Any,
    contract_version: Any,
    dependency_bearing: bool = False,
) -> dict:
    """把查询结果包成带出处的响应。"""
    # 22+ 条查询的返回形状实测有三类，不能一律当行列表处理：
    #   list  行列表（多数）
    #   dict  单个对象或多个命名集合（q1_blast_radius / q4_service_info /
    #         q10_infra_root_cause / q23_verification_coverage）
    #   str   单值（q8_log_source）
    # 明确告诉 agent 拿到的是哪种，免得它按行去遍历一个 dict。
    if isinstance(rows, list):
        shape, count = "list", len(rows)
    elif isinstance(rows, dict):
        shape, count = "object", None
    else:
        shape, count = type(rows).__name__, None

    payload: dict[str, Any] = {
        "query": query_name,
        "result_shape": shape,
        "row_count": count,
        "results": rows,
        "_provenance": base_provenance(query_name, params, contract_version),
    }
    if shape == "object" and isinstance(rows, dict):
        payload["result_keys"] = sorted(rows.keys())

    ver = summarize_verification(rows if isinstance(rows, list) else [])
    if ver:
        payload["_verification"] = ver
    elif dependency_bearing:
        payload["_verification"] = {
            "note": (
                "本查询结果未携带逐边验证状态。若结论依赖具体某条边是否成立，"
                "请另外调用 q22_edge_verification_verdicts 取该边的故障注入判定"
                "（不是 q20_dependency_verification —— 那是观测层的漂移状态）。"
            )
        }

    if isinstance(rows, list) and not rows:
        payload["empty_result_guidance"] = (
            "返回 0 行。空结果不等于错误——例如 q14_cross_region_resources 在"
            "单区域部署下本就应为空。不要把空结果解释成「依赖不存在」，"
            "它只说明这个查询在当前图谱状态下没有匹配。"
        )
    return payload


def error_result(query_name: str, params: dict, exc: BaseException) -> dict:
    """错误也要带出处，且明确禁止拿失败当「不存在」。"""
    return {
        "query": query_name,
        "error": f"{type(exc).__name__}: {exc}",
        "params": params,
        "queried_at": _now(),
        "guidance": (
            "查询失败。**不要把失败解释成「该依赖不存在」或据此推断拓扑**——"
            "这是取数失败，不是事实。请报告查询失败本身。"
        ),
    }

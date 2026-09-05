"""
_common.py — demo 各页面共用的基础设施。

存在的理由：改造前 5 个页面各自重复 `sys.path` 注入 + `os.environ.setdefault`
三件套，节点类型清单散落在 3 个地方，计数硬编码在 4 个地方。本模块把它们
收敛到一处，并给出统一的「实时 / 离线快照」降级路径。

三条原则：
1. **数字一律现算**：节点/边/查询数量从 profiles/graph_contract.yaml 与
   rca/neptune/query_catalog.py 派生，代码里不出现计数字面量。
2. **无凭证也能看**：Neptune 不可达时回退到 fixtures/ 里的真实数据快照，
   并在界面上明确标注是快照（不伪装成实时）。
3. **不内嵌集群端点**：NEPTUNE_ENDPOINT 从环境变量取，缺失时进离线模式。
"""
from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from typing import Any

import streamlit as st
import yaml

# ── 路径 ──────────────────────────────────────────────────────────────────────
DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(DEMO_DIR, ".."))
FIXTURE_DIR = os.path.join(DEMO_DIR, "fixtures")
CONTRACT_PATH = os.path.join(PROJECT_ROOT, "profiles", "graph_contract.yaml")
FAULT_CATALOG_PATH = os.path.join(
    PROJECT_ROOT, "chaos", "code", "runner", "fault_catalog.yaml"
)

for _p in (
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "rca"),
    os.path.join(PROJECT_ROOT, "chaos", "code"),
    os.path.join(PROJECT_ROOT, "dr-plan-generator"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 环境 ──────────────────────────────────────────────────────────────────────
os.environ.setdefault("REGION", "ap-northeast-1")
os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["REGION"])

REGION = os.environ["REGION"]
NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")

#: 真实注入按钮的闸门。线上刻意不设该变量，演示页不应能打生产。
INJECTION_ENABLED = os.environ.get("DEMO_ALLOW_INJECTION") == "1"

# 依赖边类型由契约派生（见 dependency_edge_labels），此处仅作为离线兜底顺序
_STATUS_ORDER = ["confirmed", "refuted", "inconclusive", "untested"]

STATUS_META = {
    "confirmed": ("✅", "已确认", "#2E7D32"),
    "refuted":   ("❌", "已证伪", "#C62828"),
    "inconclusive": ("⚠️", "未定", "#EF6C00"),
    "untested":  ("⬜", "未测", "#757575"),
}


# ── 契约（唯一真相源）────────────────────────────────────────────────────────
#: 契约加载失败原因。非空表示界面上所有「现算」数字都不可用——必须显式告警，
#: 绝不能静默降级成 0：显示「0 种节点类型」比报错更糟，因为那是在撒谎。
CONTRACT_ERROR = ""


@lru_cache(maxsize=1)
def contract() -> dict:
    """
    加载 graph_contract.yaml —— ETL 写入门禁实际读取的那一份。

    读不到时返回空 dict 而不是抛异常：`sidebar()` 每页都会调用它，
    一个缺失文件不应该让整站白屏。调用方通过 `CONTRACT_ERROR` 判断可信度。
    """
    global CONTRACT_ERROR
    try:
        with open(CONTRACT_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not data.get("node_types"):
            CONTRACT_ERROR = f"{CONTRACT_PATH} 存在但缺少 node_types 段"
            return {}
        CONTRACT_ERROR = ""
        return data
    except FileNotFoundError:
        CONTRACT_ERROR = f"契约文件不存在：{CONTRACT_PATH}"
    except Exception as exc:  # noqa: BLE001
        CONTRACT_ERROR = f"契约文件无法解析：{type(exc).__name__}: {exc}"
    return {}


def contract_ok() -> bool:
    """契约是否可用。调用会触发一次加载，因此可安全用于渲染前的门禁判断。"""
    contract()
    return not CONTRACT_ERROR


@lru_cache(maxsize=1)
def schema_counts() -> dict:
    """节点/边类型数量与来源数量，全部现算。"""
    gc = contract()
    node_types = gc.get("node_types", {}) or {}
    edge_types = gc.get("edge_types", {}) or {}
    srcs = gc.get("sources", {}) or {}
    return {
        "node_types": len(node_types),
        "edge_types": len(edge_types),
        "sources": len(srcs),
        "dependency_edge_types": len(dependency_edge_labels()),
        "contract_version": gc.get("version", "?"),
    }


@lru_cache(maxsize=1)
def dependency_edge_labels() -> tuple:
    """契约里标了 dependency: true 的边类型——「A 依赖 B」的那批。"""
    gc = contract()
    return tuple(
        name
        for name, spec in (gc.get("edge_types", {}) or {}).items()
        if isinstance(spec, dict) and spec.get("dependency")
    )


@lru_cache(maxsize=1)
def verification_rubric() -> dict:
    """证据权重与阈值，用于在界面上解释判定规则。"""
    return contract().get("edge_verification", {}) or {}


@lru_cache(maxsize=1)
def node_types_by_writer() -> dict:
    """
    按契约的 `writer` 字段给节点类型分组——即「这类节点由哪个 ETL 负责写」。
    比人为分层更有价值：它同时回答了溯源问题。
    """
    gc = contract()
    groups: dict[str, list] = {}
    for name, spec in (gc.get("node_types", {}) or {}).items():
        w = "未标注 writer"
        if isinstance(spec, dict) and spec.get("writer"):
            w = str(spec["writer"])
        groups.setdefault(w, []).append(name)
    for v in groups.values():
        v.sort()
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


@lru_cache(maxsize=1)
def node_type_table() -> list:
    """节点类型明细表：身份键、是否不可变、过期策略、writer。"""
    gc = contract()
    rows = []
    for name, spec in (gc.get("node_types", {}) or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        exp = spec.get("expires_seconds")
        rows.append({
            "节点类型": name,
            "身份键": spec.get("identity", "—"),
            "身份键不可变": "是" if spec.get("immutable") else "否",
            "过期": "永不过期" if exp in (None, 0) else f"{int(exp)}s",
            "writer": spec.get("writer", "—"),
            "说明": (spec.get("note") or spec.get("expiry_note") or "")[:60],
        })
    rows.sort(key=lambda r: r["节点类型"])
    return rows


@lru_cache(maxsize=1)
def edge_type_table() -> list:
    """边类型明细表：是否依赖边、允许的端点组合数、过期策略。"""
    gc = contract()
    rows = []
    for name, spec in (gc.get("edge_types", {}) or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        exp = spec.get("expires_seconds")
        rows.append({
            "边类型": name,
            "依赖边": "✅ 是" if spec.get("dependency") else "—",
            "源端点": len(spec.get("src", []) or []),
            "目标端点": len(spec.get("dst", []) or []),
            "合法组合": len(spec.get("pairs", []) or []),
            "过期": "永不过期" if exp in (None, 0) else f"{int(exp)}s",
            "说明": (spec.get("note") or "")[:70],
        })
    rows.sort(key=lambda r: (r["依赖边"] == "—", r["边类型"]))
    return rows


@lru_cache(maxsize=1)
def source_vocabulary() -> list:
    """契约允许的数据源白名单——写入门禁实际校验的那个清单。"""
    gc = contract()
    srcs = gc.get("sources", []) or []
    if isinstance(srcs, dict):
        return sorted(srcs.keys())
    return list(srcs)


@lru_cache(maxsize=1)
def few_shot_examples() -> list:
    """
    契约里的 NL → Cypher 示例对（`{q, cypher}`）。

    这批示例是 NL 查询引擎真正用的 few-shot 语料，也就是「AI 应该生成什么」
    的标准答案。页面用它当示例问题，好处是问题一定有对应的正确 Cypher，
    不像硬编码的问题清单可能问出图里根本没有的东西。
    """
    try:
        from profiles.profile_loader import EnvironmentProfile  # type: ignore

        ex = EnvironmentProfile().neptune_few_shot_examples or []
        return [e for e in ex if isinstance(e, dict) and e.get("q")]
    except Exception:  # noqa: BLE001
        return []


@st.cache_resource(show_spinner=False)
def nlquery_engine():
    """
    NL 查询引擎单例。

    加 cache_resource 的原因：改造前每次交互都重建引擎（Strands agent、
    Bedrock 客户端、tool schema 全部重来），既慢又刷日志。
    """
    from engines.factory import make_nlquery_engine  # type: ignore

    return make_nlquery_engine()


def active_engine_name() -> str:
    """
    当前**实际生效**的引擎名。

    不能用 `os.environ.get("NLQUERY_ENGINE") or "direct"` 猜——
    `rca/engines/factory.py` 的默认值是 `strands`，而且 strands 不可用时
    会静默回落 direct。改造前页面就是这么猜的，于是在不设环境变量时
    显示 direct、实际跑 strands，还据此选错了「工作原理」的说明文字。
    """
    try:
        eng = nlquery_engine()
        name = getattr(eng, "ENGINE_NAME", None) or getattr(eng, "engine_name", None)
        if name:
            return str(name)
        return type(eng).__name__
    except Exception:  # noqa: BLE001
        return "unavailable"


def build_engine(engine: str):
    """
    按名字构造指定引擎（用于 direct / strands 并排对比）。

    工厂在**调用时**读环境变量，所以临时改 env 再构造是可行的；
    构造完立刻恢复，避免影响后续调用。
    """
    import os as _os

    from engines.factory import make_nlquery_engine  # type: ignore

    prev = _os.environ.get("NLQUERY_ENGINE")
    _os.environ["NLQUERY_ENGINE"] = engine
    try:
        return make_nlquery_engine()
    finally:
        if prev is None:
            _os.environ.pop("NLQUERY_ENGINE", None)
        else:
            _os.environ["NLQUERY_ENGINE"] = prev


def services() -> tuple[list, str]:
    """
    Microservice 清单，从图谱现取。返回 (services, mode)。

    改造前各页面硬编码 7 个服务名，实测图谱里有 20 个 Microservice 节点
    （去重后 15 个名字），且清单里的 `petadoptionshistory` 与图谱/混沌目录用的
    `pethistory` 不是同一个名字——这类命名空间不一致正是本项目反复查出的
    「身份不唯一」缺陷，不该在展示层再复制一遍。

    同名多条是正常的：同一服务在多个 AZ 各有一个节点。
    """
    if neptune_online():
        res = gquery(
            "MATCH (n:Microservice) RETURN n.name AS name, n.tier AS tier, "
            "n.az AS az, n.fault_boundary AS fault_boundary, "
            "n.recovery_priority AS recovery_priority ORDER BY n.name"
        )
        if "error" not in res:
            return res["results"], "live"
    snap = fixture("services")
    return (snap.get("services", []), "snapshot" if snap else "none")


def service_names() -> list:
    """去重后的服务名列表（同名跨 AZ 折叠成一个）。"""
    rows, _ = services()
    seen: list = []
    for r in rows:
        n = r.get("name")
        if n and n not in seen:
            seen.append(n)
    return seen


@lru_cache(maxsize=1)
def query_catalog_info() -> dict:
    """查询库条目——从 QUERY_CATALOG 现算，不采信任何 'Q1-Q18' 说法。"""
    try:
        from neptune.query_catalog import QUERY_CATALOG  # type: ignore

        names = sorted(QUERY_CATALOG.keys())
        return {"available": True, "count": len(names), "names": names}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "count": 0, "names": [], "error": str(exc)}


@lru_cache(maxsize=1)
def fault_catalog_counts() -> dict:
    """故障目录条数，按后端拆分——从 YAML body 现算。"""
    try:
        with open(FAULT_CATALOG_PATH, encoding="utf-8") as fh:
            fc = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc), "total": 0}
    out = {
        "available": True,
        "chaosmesh": len(fc.get("chaosmesh", []) or []),
        "fis": len(fc.get("fis", []) or []),
        "fis_scenarios": len(fc.get("fis_scenarios", []) or []),
        "raw": fc,
    }
    out["total"] = out["chaosmesh"] + out["fis"] + out["fis_scenarios"]
    return out


# ── 离线快照 ──────────────────────────────────────────────────────────────────
@lru_cache(maxsize=16)
def fixture(name: str) -> dict:
    """读取 fixtures/<name>.json；缺失时返回空 dict 而不是抛异常。"""
    path = os.path.join(FIXTURE_DIR, f"{name}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


# ── Neptune：可达性探测 + 统一降级 ────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _neptune_module():
    """惰性导入 Neptune 客户端；进程内只导一次。"""
    if not NEPTUNE_ENDPOINT:
        return None
    try:
        from neptune import neptune_client as nc  # type: ignore

        return nc
    except Exception:  # noqa: BLE001
        return None


@st.cache_data(ttl=120, show_spinner=False)
def neptune_online() -> bool:
    """一次轻量查询判断图谱是否可达。结果缓存 2 分钟，避免每次交互都探。"""
    nc = _neptune_module()
    if nc is None:
        return False
    try:
        nc.query("MATCH (n) RETURN count(n) AS c LIMIT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


@st.cache_data(ttl=60, show_spinner=False)
def gquery(cypher: str) -> dict:
    """
    执行 openCypher。返回 {"results": [...]} 或 {"error": "..."}。
    永不抛异常——调用方只需判断 'error' 键。
    """
    nc = _neptune_module()
    if nc is None:
        return {"error": "Neptune 未配置（NEPTUNE_ENDPOINT 未设置）"}
    try:
        res = nc.query(cypher)
        if isinstance(res, dict):
            return {"results": res.get("results", [])}
        return {"results": res or []}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# ── 数据：实时优先，回退快照 ──────────────────────────────────────────────────
def graph_stats() -> tuple[dict, str]:
    """
    图谱规模统计。返回 (data, mode)，mode ∈ {"live", "snapshot", "none"}。
    """
    if neptune_online():
        nodes = gquery("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC")
        edges = gquery("MATCH ()-[e]->() RETURN type(e) AS t, count(*) AS c ORDER BY c DESC")
        if "error" not in nodes and "error" not in edges:
            nl, el = nodes["results"], edges["results"]
            return (
                {
                    "nodes_by_label": nl,
                    "edges_by_type": el,
                    "node_total": sum(r["c"] for r in nl),
                    "edge_total": sum(r["c"] for r in el),
                    "node_label_count": len(nl),
                    "edge_type_count": len(el),
                },
                "live",
            )
    snap = fixture("graph_stats")
    return (snap, "snapshot" if snap else "none")


def verification_data() -> tuple[dict, str]:
    """
    依赖边验证状态。返回 (data, mode)。这是本项目的核心产出。
    """
    labels = dependency_edge_labels()
    if neptune_online() and labels:
        lst = ", ".join(f"'{x}'" for x in labels)
        by = gquery(
            f"MATCH ()-[e]->() WHERE type(e) IN [{lst}] "
            "RETURN type(e) AS edge_type, coalesce(e.verify_status,'untested') AS status, "
            "count(*) AS c ORDER BY edge_type, status"
        )
        det = gquery(
            f"MATCH (a)-[e]->(b) WHERE type(e) IN [{lst}] "
            "AND e.verify_status IS NOT NULL AND e.verify_status <> 'untested' "
            "RETURN coalesce(a.name, a.arn, 'unknown') AS source, "
            "type(e) AS edge_type, coalesce(b.name, b.arn, 'unknown') AS target, "
            "e.verify_status AS status, e.verify_confidence AS confidence, "
            "e.verify_degradation AS degradation, e.verify_experiment AS experiment, "
            "e.verify_last AS verified_at, e.verify_reason AS reason, "
            "e.verify_evidence_channel AS evidence_channel, e.source AS edge_source "
            "ORDER BY e.verify_status, type(e) LIMIT 200"
        )
        if "error" not in by and "error" not in det:
            counts: dict[str, int] = {}
            for row in by["results"]:
                counts[row["status"]] = counts.get(row["status"], 0) + row["c"]
            total = sum(counts.values())
            decided = counts.get("confirmed", 0) + counts.get("refuted", 0)
            return (
                {
                    "by_edge_type_status": by["results"],
                    "totals_by_status": counts,
                    "dependency_edge_total": total,
                    "verified_ratio": round(decided / total, 4) if total else 0.0,
                    "decided_edges": det["results"],
                },
                "live",
            )
    snap = fixture("verification")
    return (snap, "snapshot" if snap else "none")


# ── UI 组件 ───────────────────────────────────────────────────────────────────
NAV = [
    ("app.py", "🏠 首页 · 这张图是真的吗"),
    ("pages/1_Edge_Verification.py", "🎯 边验证 · 核心"),
    ("pages/2_Query_Catalog.py", "📚 查询库 · 免 AI"),
    ("pages/3_Graph_Explorer.py", "🕸️ 图谱 · 分层总览"),
    ("pages/9_Interactive_Explorer.py", "🧭 图谱 · 交互探索"),
    ("pages/4_Smart_Query.py", "💬 自然语言查询"),
    ("pages/5_Agent_Dependencies.py", "🤖 Agent 依赖"),
    ("pages/6_Root_Cause_Analysis.py", "🔍 根因分析"),
    ("pages/7_Chaos_Engineering.py", "💥 混沌工程"),
    ("pages/8_DR_Plan.py", "🛡️ DR 计划"),
]


def page_setup(title: str, icon: str = "🕸️", layout: str = "wide") -> None:
    """统一的 set_page_config —— 必须在任何其他 st.* 调用之前。"""
    st.set_page_config(
        page_title=f"{title} · Graph Dependency Platform",
        page_icon=icon,
        layout=layout,
        initial_sidebar_state="expanded",
    )


def mode_badge(mode: str, what: str = "数据") -> None:
    """
    明确告诉观众看到的是实时还是快照。
    刻意不把快照伪装成实时——那会毁掉整个项目关于「数据可信」的主张。
    """
    if mode == "live":
        st.success(f"🟢 **实时** —— {what}直接来自 Neptune 活图谱", icon="🟢")
    elif mode == "snapshot":
        snap_time = ""
        for key in ("verification", "graph_stats", "agent_graph"):
            ts = fixture(key).get("captured_at")
            if ts:
                snap_time = ts
                break
        st.info(
            f"🔵 **离线快照** —— 当前环境无法访问 Neptune，展示的是活图谱的真实快照"
            + (f"（抓取于 {snap_time}）" if snap_time else "")
            + "。数字真实，只是不是此刻的。"
        )
    else:
        st.warning("⚪ 无数据 —— Neptune 不可达且未找到离线快照")


def embed_html(html: str, height: int) -> None:
    """
    在沙箱 iframe 里渲染一段自带 JS 的 HTML（pyvis / vis-network 的产物）。

    不能用 `st.html`：它走 DOMPurify 且默认 `unsafe_allow_javascript=False`，
    会把 vis-network 的脚本清掉。

    版本兼容：`streamlit.components.v1.html` 自 1.56 起标记弃用，替代品是
    `st.iframe`（能自动识别 HTML 字符串）；但 1.50 等旧版没有 `st.iframe`，
    所以按可用性择一，两个版本都能跑。
    """
    if hasattr(st, "iframe"):
        st.iframe(html, height=height)
        return
    import streamlit.components.v1 as _components  # 旧版回退
    _components.html(html, height=height, scrolling=False)


def page_link(path: str, label: str, width: str = "content") -> None:
    """
    st.page_link 的容错包装。

    st.page_link 依赖多页应用上下文；页面被**单独执行**时
    （`streamlit run demo/pages/X.py`，或测试用 AppTest 直接跑单个页面文件）
    它会抛 `KeyError: 'url_pathname'` 把整页打挂。
    这里退化为纯文本，保证单页也能正常渲染。
    """
    try:
        st.page_link(path, label=label, width=width)
    except Exception:  # noqa: BLE001
        # 退化：单独执行时没有多页路由，给出文件名即可
        st.caption(f"{label}　`{path}`")


def sidebar(active: str = "") -> None:
    """统一侧栏：导航 + 契约摘要 + 连接状态。"""
    with st.sidebar:
        st.markdown("### 导航")
        for path, label in NAV:
            page_link(path, label)

        st.markdown("---")
        st.markdown("### 图谱契约")
        if not contract_ok():
            st.error(
                "契约文件读不到，本页所有「现算」数字均不可用。\n\n"
                f"`{CONTRACT_ERROR}`\n\n"
                "部署遗漏了 `profiles/graph_contract.yaml`。"
            )
        else:
            c = schema_counts()
            qc = query_catalog_info()
            st.caption(
                f"版本 `{c['contract_version']}` · 单一真相源\n\n"
                f"- 节点类型 **{c['node_types']}**\n"
                f"- 边类型 **{c['edge_types']}**（其中依赖边 **{c['dependency_edge_types']}**）\n"
                f"- 合法数据源 **{c['sources']}**\n"
                f"- 预置查询 **{qc['count']}**"
            )

        st.markdown("---")
        online = neptune_online()
        if online:
            st.success("Neptune 已连接")
        elif NEPTUNE_ENDPOINT:
            st.warning("Neptune 不可达 · 离线快照模式")
        else:
            st.info("未配置 Neptune · 离线快照模式")
        st.caption(f"Region `{REGION}`")


def status_chips(counts: dict, total: int) -> None:
    """四种验证状态的指标行。"""
    cols = st.columns(len(_STATUS_ORDER) + 1)
    for i, key in enumerate(_STATUS_ORDER):
        icon, label, _ = STATUS_META[key]
        n = counts.get(key, 0)
        pct = f"{n / total * 100:.1f}%" if total else "—"
        cols[i].metric(f"{icon} {label}", n, pct, delta_color="off")
    decided = counts.get("confirmed", 0) + counts.get("refuted", 0)
    cols[-1].metric(
        "🎯 已验证覆盖率",
        f"{decided / total * 100:.2f}%" if total else "—",
        f"{decided}/{total}",
        delta_color="off",
    )


def df(rows: list[dict[str, Any]]):
    """list[dict] → DataFrame，空列表也安全。"""
    import pandas as pd

    return pd.DataFrame(rows or [])

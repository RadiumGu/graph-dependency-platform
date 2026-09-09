"""合规依赖报告导出层 —— 查询口径的单一来源。

## 这个模块回答什么

监管对依赖关系的要求分三类（见 `todo/decks/合规依赖报告-能力评估与补齐路线.md`）：

    A 穿透式依赖映射   DORA Art. 8(4) / BCBS POR 原则四 / SYSC 15A.4.1R / 关基条例第九条
    B 合同型登记册     DORA Art. 28/29/31 —— 本平台结构上做不到，也不该做
    C 容忍度阈值       SYSC 15A.2.5R —— 承载能力有，阈值数据待业务方填

本模块只做 **A 类**，产出四份互相咬合的表：

    1. 功能映射表     每个业务能力的依赖全集（DORA 8(1)/8(4)、SYSC 15A.4.1R）
    2. 三栏分列统计   证据等级 / 声明-观测 / 第三方范围，**刻意不合并成单一覆盖率**
    3. 技术集中度     多个业务功能共同依赖的对象（SYSC 15A.2.7G(10)）
    4. 承载层        LocatedIn/RunsOn 等，单列且注明性质（不是服务消费关系）

## 三条不可协商的纪律

**一、依赖边清单必须来自契约。** 本仓库曾因四处各抄一份依赖边清单而产生分歧
（契约 / RCA 兜底 / drift 对账 / dr-plan ordering），其中两处漂移到实际错误。
所以这里只允许 `graph_contract.dependency_edge_labels()`，不得内联字面量。
由 `tests/test_54_compliance_export.py::m01` 静态锁定。

**二、快照时刻必须唯一且贯穿全表。** 活图谱正被 ETL 持续改写 —— 实测数分钟内
`LocatedIn` 从 1060 变 1054、`EC2Instance` 从 13 变 10。DORA 的登记册用 12/31
作为统一基准日，正是为了让所有模板可交叉校验；ESAs 点名的失败模式里就有
「跨模板基准日不一致」。所以 `Snapshot` 在一次导出里只取一次，写进每份产出的页首。

**三、`verify_status` 与 `dependency_kind` 是强制列。** 前者是「凭什么说这条依赖
成立」（SYSC 15A.5.3R 场景测试的证据），后者是「声明的还是观测到的」。
少任何一列，这份报告就退化成填表工具的产出。

## Neptune openCypher 的两个坑（实测）

- **不支持 `any()` / `all()` 列表谓词**（报 `'any' predicate function is not supported.`）。
  标签多选要写成源侧 `(s:A OR s:B)`、目标侧 `labels(t)[0] IN [...]`。
- `BusinessCapability` **零出边**，方向是 `Microservice -[Implements]-> BusinessCapability`。
  遍历必须先反向跳一步。契约 55374a2 已注明该出边刻意不生成，别去「修」它。
"""
from __future__ import annotations

import datetime
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path[:0] = os.environ.get("PYTHONPATH", "").split(":")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "rca") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "rca"))
_SHARED = os.path.join(_ROOT, "infra", "lambda", "shared", "python")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from graph_contract import dependency_edge_labels  # noqa: E402

#: 承载/放置类边 —— **刻意不算依赖**。
#:
#: 理由不是它们不重要（Region 挂了什么都挂），而是它们**普遍为真**、对几乎每个
#: 资源都成立，因而不携带判别信息。算进依赖会让 Region 成为所有东西的咽喉点，
#: 淹没真正可操作的发现。
#:
#: 代价必须在报告里说明：EKS 集群与负载均衡器因此不出现在依赖表中，而它们在
#: DORA 视角下确实是关键 ICT 服务 —— 所以单列一栏（见 `fetch_hosting_layer`）。
HOSTING_EDGE_LABELS = ("LocatedIn", "RunsOn", "BelongsTo", "Manages", "Routes")

#: 业务能力 → 实现它的服务，方向是**入边**（见模块 docstring 第二个坑）。
IMPLEMENTS_LABEL = "Implements"


def _dep_labels() -> List[str]:
    """依赖边标签，**唯一来源是契约**。不要在本模块任何地方内联字面量。"""
    labels = sorted(dependency_edge_labels())
    if not labels:
        raise RuntimeError(
            "契约返回了空的依赖边集合。这不可能是正常状态 —— "
            "宁可让导出失败，也不要产出一份缺边的合规报告。"
        )
    return labels


def _label_list(labels) -> str:
    """渲染成 openCypher 的 `[...]` 字面量（双引号，Neptune 兼容）。"""
    return ", ".join('"%s"' % l for l in labels)


def _rel_alternation(labels) -> str:
    """渲染成 `-[r:A|B|C]->` 的关系类型选择列表。"""
    return "|".join(labels)


# ─── 快照 ────────────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    """一次导出的全部数据与其基准时刻。

    `taken_at` 在构造时取一次，之后所有表共用 —— 这是跨表可交叉校验的前提。
    """

    taken_at: str
    endpoint: str
    dependency_edge_labels: List[str]
    function_mapping: List[Dict[str, Any]] = field(default_factory=list)
    hosting_layer: List[Dict[str, Any]] = field(default_factory=list)
    concentration: List[Dict[str, Any]] = field(default_factory=list)
    capability_meta: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def capability_count(self) -> int:
        return len({r["capability"] for r in self.function_mapping})

    @property
    def dependency_count(self) -> int:
        return len(self.function_mapping)


# ─── 四份产出的查询 ──────────────────────────────────────────────────────────


def fetch_function_mapping(nc, dep_labels: List[str]) -> List[Dict[str, Any]]:
    """功能映射表 —— 每个业务能力的一跳依赖全集。

    对应 DORA Art. 8(1)（business functions ← 支撑资产 ← 其 dependencies）与
    Art. 8(4)（map the links and interdependencies），以及 SYSC 15A.4.1R 的
    technology 维度。

    ## 为什么是一跳而不是多跳

    多跳（`*1..6`）能给出「可达对象」总数，适合回答「这项业务功能一共牵连多少
    资产」；但一跳才是**可归责的直接依赖**，每条边有明确的源、目标、观测来源与
    证据等级。报告主体用一跳，多跳作为补充统计（`fetch_reachability`）。

    ## `verify_status` 与 `dependency_kind` 是强制列

    两者都可能为 null（未验证 / 尚未打标），此时导出 `None` 而不是省略该列 ——
    「未验证」与「没有这一列」在审计上是完全不同的两件事。
    """
    rel = _rel_alternation(dep_labels)
    rows = nc.results(
        f"MATCH (i:BusinessCapability)<-[:{IMPLEMENTS_LABEL}]-(s) "
        f"MATCH (s)-[d:{rel}]->(t) "
        "RETURN i.name AS capability, "
        "       i.recovery_priority AS tier, "
        "       s.name AS service, "
        "       labels(s)[0] AS service_label, "
        "       type(d) AS edge_type, "
        "       coalesce(t.name, t.tool_key, t.arn) AS target, "
        "       labels(t)[0] AS target_label, "
        "       t.scope AS target_scope, "
        "       d.dependency_kind AS dependency_kind, "
        "       d.verify_status AS verify_status, "
        "       d.confidence AS confidence, "
        "       d.source AS source, "
        "       d.drift_status AS drift_status, "
        "       d.last_seen AS last_seen, "
        "       d.verify_experiment AS verify_experiment "
        "ORDER BY capability, service, edge_type, target"
    )
    return rows


def fetch_reachability(nc, dep_labels: List[str], max_hops: int = 6) -> List[Dict[str, Any]]:
    """每个业务能力的多跳可达对象数 —— 「一共牵连多少资产」。

    这是 DORA Art. 8(4) 「links and interdependencies」的穿透视角，也是清单
    做不到的那一部分。刻意与一跳分开呈现，避免把两种口径混成一个数字。
    """
    rel = _rel_alternation(dep_labels)
    return nc.results(
        f"MATCH (i:BusinessCapability)<-[:{IMPLEMENTS_LABEL}]-(s) "
        f"MATCH p=(s)-[:{rel}*1..{max_hops}]->(d) "
        "RETURN i.name AS capability, "
        "       count(DISTINCT d) AS reachable_objects, "
        "       count(p) AS paths "
        "ORDER BY capability"
    )


def fetch_hosting_layer(nc) -> List[Dict[str, Any]]:
    """承载层 —— 单列一栏，注明性质。

    这些边**不是**服务消费关系，而是放置/承载关系。报告里必须单列并注明，
    否则读者会以为 EKS 集群与负载均衡器被遗漏了（它们在 DORA 视角下确实是
    关键 ICT 服务，只是不该混进依赖表 —— 见 HOSTING_EDGE_LABELS 的注释）。
    """
    out = []
    for label in HOSTING_EDGE_LABELS:
        rows = nc.results(f"MATCH ()-[r:{label}]->() RETURN count(r) AS n")
        out.append({
            "edge_type": label,
            "count": rows[0]["n"] if rows else 0,
            "nature": "承载/放置关系，非服务消费关系",
        })
    return out


def fetch_concentration(nc, dep_labels: List[str]) -> List[Dict[str, Any]]:
    """技术集中度 —— 被多个业务功能共同依赖的对象。

    对应 SYSC 15A.2.7G(10) 的原文要求：评估「multiple important business services
    rely on **common operational resources** as identified by the firm's mapping
    exercise」，也是 DORA Art. 29/31（分包链集中度、关键第三方指定）的技术输入。

    注意这里给的是**技术**集中度。DORA Art. 29/31 要的是供应商层面的集中度，
    需要 `Vendor` 节点才能回答，本平台当前没有 —— 报告必须说明这个边界。
    """
    rel = _rel_alternation(dep_labels)
    return nc.results(
        f"MATCH (i:BusinessCapability)<-[:{IMPLEMENTS_LABEL}]-(s) "
        f"MATCH (s)-[d:{rel}]->(t) "
        "WITH coalesce(t.name, t.tool_key, t.arn) AS target, "
        "     labels(t)[0] AS target_label, "
        "     t.scope AS target_scope, "
        "     count(DISTINCT i.name) AS capability_count, "
        "     count(d) AS edge_count "
        "WHERE capability_count > 1 "
        "RETURN target, target_label, target_scope, capability_count, edge_count "
        "ORDER BY capability_count DESC, edge_count DESC, target"
    )


def fetch_capability_meta(nc) -> List[Dict[str, Any]]:
    """业务能力自身的属性 —— 含 impact tolerance（当前全为 null）。

    `impact_tolerance_seconds` 为 null 时报告**不得**出现任何「未越界」表述：
    SYSC 15A.2.5R 要求 firm *must* set an impact tolerance，没设就是没设，
    不能用「没检测到越界」掩盖「压根没有阈值」。
    """
    return nc.results(
        "MATCH (i:BusinessCapability) "
        "RETURN i.name AS capability, "
        "       i.recovery_priority AS tier, "
        "       i.impact_tolerance_seconds AS impact_tolerance_seconds, "
        "       i.rto_target_seconds AS rto_target_seconds "
        "ORDER BY capability"
    )


def take_snapshot(nc=None, max_hops: int = 6) -> Snapshot:
    """取一次完整快照。**所有表共用同一个 `taken_at`。**

    Args:
        nc: Neptune 客户端（需有 `results(cypher) -> list`）。None 时用
            `rca.neptune.neptune_client`，便于测试注入替身。
    """
    if nc is None:
        from neptune import neptune_client as _nc  # type: ignore
        nc = _nc

    taken_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dep = _dep_labels()

    snap = Snapshot(
        taken_at=taken_at,
        endpoint=os.environ.get("NEPTUNE_ENDPOINT", "(unset)"),
        dependency_edge_labels=dep,
    )
    snap.function_mapping = fetch_function_mapping(nc, dep)
    snap.hosting_layer = fetch_hosting_layer(nc)
    snap.concentration = fetch_concentration(nc, dep)
    snap.capability_meta = fetch_capability_meta(nc)
    snap.reachability = fetch_reachability(nc, dep, max_hops)  # type: ignore[attr-defined]
    return snap

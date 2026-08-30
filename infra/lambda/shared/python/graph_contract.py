"""图谱契约门禁 —— 写入 Neptune 之前校验类型与身份键。

## 为什么存在

引入本模块之前，四个写入 ETL（etl_aws / etl_deepflow / etl_xray / etl_cfn）
**无一 import profiles**，运行时对节点与边类型零校验：`upsert_vertex` /
`upsert_edge` 拿到什么 label 就往 Gremlin 里直拼。后果是任何拼错的或新造的
标签都会被**静默**写进 Neptune，而所谓「权威 schema」
（profiles/petsite.yaml 的 graph_schema_text）只在测试期被比对一次。
参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 1。

业界的对照做法是 New Relic 的 entity-definitions：身份定义是声明式 YAML，
配 PR + 自动校验 + owner 双评审，而不是散在代码里的字符串字面量。

## 三种模式

由环境变量 `GRAPH_CONTRACT_MODE` 控制，默认 `enforce`：

- `enforce` —— 违约抛 `GraphContractError`，那一次写入失败。
                这是默认值：静默写入未声明类型正是要消除的缺陷。
- `warn`    —— 只 log warning 并放行。用于**灰度**：新接一个 ETL 或大改类型
                声明时先跑一轮 warn，把真实违约摸清再切 enforce，
                避免一上线就把整轮采集打断。
- `off`     —— 完全跳过。只应在离线回放/单测夹具里用。

## 刻意不做的事

- **不校验属性集**。schema 文本里的属性列表是文档性的、且各源写入的属性子集
  本来就不同（xray 只补度量、cfn 只写 declared_in）。强制属性集会把
  「这个源没有这项数据」误判成违约。
- **端点约束默认只 warn**。src/dst 白名单来自 schema 文本的
  `(:A)-[:E]->(:B)` 行，那份声明本身就不完整（实测 WritesTo 的 dst 写的是
  S3/SNS/SQS 这种非类型名）。先观察一段再决定是否升级为 enforce，
  由 `GRAPH_CONTRACT_ENDPOINTS` 单独控制。
"""
from __future__ import annotations

import logging
import os

from graph_contract_data import (  # noqa: F401 (re-export)
    CONTRACT_VERSION,
    EDGE_TYPES,
    EDGE_WRITE_ONCE_ATTRS,
    NODE_ATTR_AUTHORITY,
    NODE_TYPES,
    SOURCES,
    TIMESTAMP_FIELD,
    TIMESTAMP_LEGACY_ALIASES,
)

logger = logging.getLogger()

MODE_ENFORCE = 'enforce'
MODE_WARN = 'warn'
MODE_OFF = 'off'
_VALID_MODES = (MODE_ENFORCE, MODE_WARN, MODE_OFF)


class GraphContractError(ValueError):
    """写入违反图谱契约。"""


def _mode() -> str:
    """每次调用都读环境变量 —— 便于单测用 monkeypatch 切换模式。"""
    m = (os.environ.get('GRAPH_CONTRACT_MODE') or MODE_ENFORCE).strip().lower()
    return m if m in _VALID_MODES else MODE_ENFORCE


def _endpoints_mode() -> str:
    """端点约束单独控制，默认 warn（声明本身不完整，见模块 docstring）。"""
    m = (os.environ.get('GRAPH_CONTRACT_ENDPOINTS') or MODE_WARN).strip().lower()
    return m if m in _VALID_MODES else MODE_WARN


def _violate(mode: str, msg: str) -> None:
    if mode == MODE_OFF:
        return
    if mode == MODE_ENFORCE:
        raise GraphContractError(msg)
    logger.warning("graph-contract: %s", msg)


# ── 类型门禁 ──────────────────────────────────────────────────────────────

def assert_node_type(label: str) -> None:
    """节点类型必须已在契约里声明。"""
    mode = _mode()
    if mode == MODE_OFF or label in NODE_TYPES:
        return
    _violate(mode, f"未声明的节点类型 {label!r}（契约 v{CONTRACT_VERSION} 共 "
                   f"{len(NODE_TYPES)} 种）。新增类型请改 profiles/graph_contract.yaml "
                   f"并跑 scripts/gen_graph_contract.py --write")


def assert_edge_type(label: str, src_label: str = None, dst_label: str = None) -> None:
    """边类型必须已声明；端点若给出则按声明的白名单核对（默认只 warn）。"""
    mode = _mode()
    if mode == MODE_OFF:
        return
    spec = EDGE_TYPES.get(label)
    if spec is None:
        _violate(mode, f"未声明的边类型 {label!r}（契约 v{CONTRACT_VERSION} 共 "
                       f"{len(EDGE_TYPES)} 种）")
        return
    emode = _endpoints_mode()
    if emode == MODE_OFF:
        return
    if src_label and src_label not in spec['src']:
        _violate(emode, f"边 {label} 的源类型 {src_label!r} 不在声明的白名单 {spec['src']}")
    if dst_label and dst_label not in spec['dst']:
        _violate(emode, f"边 {label} 的目标类型 {dst_label!r} 不在声明的白名单 {spec['dst']}")


# ── 身份键 ────────────────────────────────────────────────────────────────

def identity_prop_for(label: str) -> str | None:
    """返回该节点类型声明的身份属性名。

    返回 'name' 时调用方无需特殊处理（那就是历史默认行为）；
    返回其它属性名时调用方应把它作为 mergeV 的匹配键，把 name 降级为普通属性。
    未声明的类型返回 None，让调用方保持原行为而不是崩掉。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('identity') if spec else None


def identity_is_immutable(label: str) -> bool:
    """声明的身份键是否不可变。

    'lifetime' 视为不可变 —— K8s Pod 那类对象重建后换名是预期语义，
    不是「同一实体被改名」。
    """
    spec = NODE_TYPES.get(label)
    if not spec:
        return False
    return spec.get('immutable') in (True, 'lifetime')


# ── 生命周期 ──────────────────────────────────────────────────────────────

def expires_seconds_for(label: str):
    """该边类型的软删除阈值（秒）。

    None 表示结构边，生命周期跟随两端节点、不独立过期
    （对应 Dynatrace 的 static edge 继承 node lifetime 语义）。
    """
    spec = EDGE_TYPES.get(label)
    return spec.get('expires_seconds') if spec else None


def retention_seconds_for(label: str):
    """硬删除边界（秒）。None 表示不硬删，只软删除。"""
    spec = EDGE_TYPES.get(label)
    return spec.get('retention_seconds') if spec else None


def is_dependency_edge(label: str) -> bool:
    """是否是「A 依赖 B」语义的边（需要 dependency_kind）。

    取代此前散在三个 ETL 里各自复制的 DEPENDENCY_EDGE_LABELS 常量 ——
    那份复制在 etl_aws/neptune_client.py 的注释里已被作者标注为待收敛项。
    """
    spec = EDGE_TYPES.get(label)
    return bool(spec and spec.get('dependency'))


def dependency_edge_labels() -> frozenset:
    return frozenset(lb for lb, s in EDGE_TYPES.items() if s.get('dependency'))


# ── 多源权威 ──────────────────────────────────────────────────────────────

def may_write_node_attr(label: str, attr: str, source: str) -> bool:
    """该来源是否有权写这个节点属性。

    未在 NODE_ATTR_AUTHORITY 里声明的 (类型, 属性) 一律放行 ——
    权威表是**例外清单**而非白名单，只登记已实测出冲突的属性。
    这样引入门禁不会把大量正常写入判成违约。
    """
    authority = NODE_ATTR_AUTHORITY.get(label, {}).get(attr)
    return True if authority is None else source in authority


def filter_node_props(label: str, props: dict, source: str) -> tuple[dict, list]:
    """按权威表过滤节点属性，返回 (放行的属性, 被拒的属性名)。

    被拒的属性**明示返回**而不是静默丢弃 —— 对应 ServiceNow IRE 的
    maskedAttributes：调用方应把它 log 出来，否则「谁赢」永远查不清。
    """
    kept, masked = {}, []
    for k, v in (props or {}).items():
        if may_write_node_attr(label, k, source):
            kept[k] = v
        else:
            masked.append(k)
    return kept, masked

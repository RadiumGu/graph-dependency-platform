"""tests/test_77_expiry_jurisdiction_is_visible.py

## 这条门禁在防什么

**失效机制的「管辖之外」必须可见。**

`deactivate_stale_dynamic_edges` 返回 `per_label[X]['stale']`。
那个数字有两种截然不同的 0：

    · 该类型的动态边都新鲜        → 一切正常
    · 该类型的动态边一条都没被看见 → 统计本身失效，而运维读到的还是 0

2026-09-18 实测后者真实存在：8 条 `deepflow-etl` 的 `DependsOn` 边没有
`last_seen`（它们写的是契约声明的 legacy 别名 `last_updated`，实测 2.3h 前、
边是**新鲜的**）。时间戳谓词 `has(last_seen, lt(cutoff))` 匹配不上它们，
于是 `DependsOn` 的 stale 统计**恒为 0**，而那个 0 的含义是「看不见」。

节点侧早就有 `_node_unjudgeable_query` 与 `unjudgeable` 字段，注释写着
「『不可判定』必须与『已判定为新鲜』分开上报」。**边侧一直没有。**

## 我在这件事上先判错了一次，判据教训记在这

我最初认定「18 条超期 78 倍的 deepflow-dns 边逃过了过期机制」，
并准备把 `has('active', true)` 的谓词当成根因。**两处都错**：

    ① deepflow-dns 是契约里唯一的 sparse_observation_sources 成员，
       被 `_not_sparse_clause()` **刻意**排除 —— 它们走 `_mark_query`，
       只标 observed_then_silent 而不碰 active。实测这条路径工作正常
       （18 条里 13 条已标 observed_then_silent，最近检查 0.0h 前）。
    ② 唯一「非稀疏 + 缺 active」的 8 条边，缺的不止 active，
       连 last_seen 都没有 —— 时间戳谓词先排除了它们。

**教训：看到一个异常数字，先查这个形状是不是已经被刻意处理过。**
本仓库把「哪些源是弱信号」写进了契约词表，那就是设计意图的所在，
而我在读词表之前就已经动手改判据了。

`_live_clause()` 仍然保留（对齐节点侧、消除两侧分叉），但它当前影响 **0 条**，
其 docstring 已如实写明这一点 —— 不拿它当收益。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CLEANUP = _ROOT / "infra" / "lambda" / "shared" / "python" / "graph_cleanup.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_gcl77", _CLEANUP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _code_only(src: str) -> str:
    """剥掉注释与 docstring —— 本仓库已有至少 5 次「断言匹配到自己写的注释」。"""
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"'''.*?'''", "", src, flags=re.S)
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


def test_t77_01_edge_side_reports_unjudgeable_like_node_side():
    """边侧必须有「管辖之外」的计数查询，与节点侧对称。"""
    src = _code_only(_CLEANUP.read_text(encoding="utf-8"))
    assert "def _edge_unjudgeable_query" in src, (
        "边侧缺「不可判定」计数查询。节点侧有 _node_unjudgeable_query，"
        "注释写着「『不可判定』必须与『已判定为新鲜』分开上报」—— 边侧同样需要，"
        "否则 stale=0 会被读成「都好」，而真相可能是「一条都没看见」。")

    m = _mod()
    q = m._edge_unjudgeable_query("DependsOn")
    assert f".not(__.has('{m.TIMESTAMP_FIELD}'))" in q, (
        f"不可判定查询没有按「缺 {m.TIMESTAMP_FIELD}」筛选: {q}")
    # 管辖口径必须与失效查询一致，否则两个数字对不上。
    assert m._not_sparse_clause() in q, "不可判定查询漏了稀疏源排除，口径与失效查询不一致"
    assert "has('dependency_kind','dynamic')" in q, "不可判定查询漏了 dynamic 限定"


def test_t77_02_unjudgeable_is_not_folded_into_stale():
    """行为验证：不可判定的边不得被算进 stale，两者必须分开上报。

    这是最要紧的一条 —— 把它们并进 stale 会让失效路径去处置看不见时间戳的边。
    """
    m = _mod()

    def fake_query(q: str):
        # 「缺时间戳」的计数 → 8；普通 stale 计数 → 0。
        if ".not(__.has('last_seen'))" in q:
            return {"result": {"data": {"@value": [{"@value": 8}]}}}
        if q.rstrip().endswith(".count()"):
            return {"result": {"data": {"@value": [{"@value": 0}]}}}
        return {"result": {"data": {"@value": []}}}

    old = os.environ.get("GRAPH_EDGE_EXPIRY_ENABLED")
    os.environ["GRAPH_EDGE_EXPIRY_ENABLED"] = "true"
    try:
        r = m.deactivate_stale_dynamic_edges(
            fake_query, round_ts=1_000_000, only_labels=["DependsOn"])
    finally:
        if old is None:
            os.environ.pop("GRAPH_EDGE_EXPIRY_ENABLED", None)
        else:
            os.environ["GRAPH_EDGE_EXPIRY_ENABLED"] = old

    e = (r.get("per_label") or {}).get("DependsOn")
    assert e, f"DependsOn 未被处理: {r}"
    assert e["unjudgeable"] == 8, f"不可判定数没上报: {e}"
    assert e["stale"] == 0, (
        f"不可判定的边被并进了 stale: {e}。"
        "那会让失效路径去处置看不见时间戳的边 —— 保守方向就丢了。")
    assert e["deactivated"] == 0, f"stale=0 却置了失效: {e}"
    assert r["unjudgeable_total"] == 8, f"合计没累加: {r}"


def test_t77_03_liveness_predicate_is_shared_between_edge_and_node():
    """两侧共用一个存活谓词 —— 分叉过一次就会再分叉一次。"""
    src = _code_only(_CLEANUP.read_text(encoding="utf-8"))
    assert "def _live_clause" in src, "没有共用的存活谓词函数"

    inline = re.findall(r"has\('active',\s*true\)", src)
    assert not inline, (
        f"仍有 {len(inline)} 处内联 has('active', true) —— 那是「存在性 + 值」的"
        "双重要求，从未被写过 active 的边匹配不上。应统一调 _live_clause()。")

    m = _mod()
    for fn in ("_count_query", "_deactivate_query", "_edge_unjudgeable_query"):
        q = getattr(m, fn)("AccessesData", 1_000_000) if fn != "_edge_unjudgeable_query" \
            else m._edge_unjudgeable_query("AccessesData")
        assert "not(__.has('active', false))" in q, f"{fn} 没用共用存活谓词: {q}"


def test_t77_04_sparse_sources_are_still_excluded_from_deactivation():
    """稀疏源仍必须走 _mark_query，不得被顺手纳入失效管辖。

    这条纪律比本次补的可见性更重要：`active=false` 断言「这条依赖不存在」，
    而稀疏源上我们能证明的只是「窗口内没观测到」。
    实测 deepflow-dns 是契约里唯一的稀疏源，18 条边全部由 _mark_query 正确处置。
    """
    m = _mod()
    assert m._sparse_sources(), (
        "稀疏源词表为空 —— 那会让 deepflow-dns 边回落到 dynamic 处置、"
        "被误置 active=false。应跑 scripts/gen_graph_contract.py --write。")
    for fn_name in ("_count_query", "_deactivate_query"):
        q = getattr(m, fn_name)("AccessesData", 1_000_000)
        assert m._not_sparse_clause() in q, (
            f"{fn_name} 丢了稀疏源排除 —— 稀疏源会被错误地判定为「依赖不存在」。")
    mark = m._mark_query("Retrieves", 1_000_000, 1_000_001)
    assert "property('active'" not in mark, (
        "_mark_query 碰了 active —— 它的全部要义就是**不**碰 active。")

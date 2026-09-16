"""契约声明了、但**必须保持零实例**的对偶（pair）—— 反向登记门禁。

## 为什么需要这个，`tests/test_11` 的 PENDING_FIRST_EDGE 不够吗

不够，因为**粒度不同**。那个门禁判的是「边类型」：

    schema_edges - actual_edges - PENDING_FIRST_EDGE

它能抓「某个边类型声明了却零实例」。但契约里还有一层是**对偶**
（`edge_types[X].pairs`，形如 `[['BusinessCapability', 'RDSCluster'], ...]`），
而对偶的缺席对上面那个门禁**完全不可见**：

    DependsOn 有 18 条实例  ⇒  边类型层面「非空」，门禁满足
    但其中 BusinessCapability 打头的三组对偶是 0 条

## 这个不可见通道有过实际代价

2026-09-07（提交 `93e3120`）查出 BusinessCapability 的 6 条出边全是假边。
根因是 `business_layer.py` 的 `depends_on_types` 按**类型**展开：
「依赖 RDSCluster」被展开成「依赖账号里每一个 RDSCluster」，把 Grafana 自己的
aurora-mysql 也接上了；另 4 条方向是反的（业务能力「依赖」自己的告警 topic
与 DLQ，实际是它们观测/承接该能力）。

代价已经落地过：那两条 Grafana 假边让 `grafana-aurora-mysql` 成了
**priority=1 的故障注入靶点**，并生成过两个 `fis_rds_failover` 实验指向它。

## 现有防护为什么不足以合上这个通道

三道都是**人的纪律**，没有一道是自动的：

  1. `business_layer.py` 的 `SKIP_BC_INFRA_LABELS` —— 有效，但只要有人
     删掉那几行就没了，删掉时不会有任何测试变红。
  2. 该文件的注释（写着「防止有人把跳过去掉」）—— 注释不执行。
  3. `graph_contract.yaml` 的 `DependsOn.note`（2026-09-09 补）—— 同样不执行。

**这个门禁是第四道，而且是唯一会变红的一道。**

## 为什么保留 pairs 而不是从契约里删掉

因为**形状本身合法**。93e3120 的结论是：

    类型这个粒度本身不够，要正确表达必须在 business_config.json 里
    **按资源名**声明。

也就是说 `BusinessCapability -[DependsOn]-> RDSCluster` 将来完全可能是一条
正确的边 —— 只要它是按资源名派生出来的。删掉 pairs 会禁掉一个正当的建模关系。
所以正确的做法是「声明合法 + 锁定当前为零」，而不是「声明为非法」。

## 销账纪律（与 PENDING_FIRST_EDGE 相反的方向）

PENDING_FIRST_EDGE：**期待它出现**，出现了就从名单移除。
本名单：**期待它不出现**，出现了说明有人重新打开了类型展开 —— 门禁变红。

若将来真的按资源名正确实现了 BusinessCapability 的基础设施依赖，
那时**要连带删掉本名单里对应的项**，并在提交说明里写清新的派生方式为什么
不会重犯类型展开的错。**不要**因为「测试挡路」就删名单。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RCA = _ROOT / "rca"
for _p in (str(_ROOT), str(_RCA)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_CONTRACT = _ROOT / "profiles" / "graph_contract.yaml"

# ── 必须保持零实例的对偶 ──────────────────────────────────────────────────────
#
# 键是边类型，值是「必须为零」的 (src_label, dst_label) 集合。
#
# ⚠️ 加项之前必须有**实测反例**说明为什么这个形状当前产不出正确的边，
#    并在下面的 _WHY 里写明依据提交。只凭「现在是 0 条」不构成理由 ——
#    大量对偶只是暂时没有实例，锁定它们会把正常的图谱演进判成回归。
MUST_STAY_EMPTY: dict[str, set[tuple[str, str]]] = {
    "DependsOn": {
        ("BusinessCapability", "RDSCluster"),
        ("BusinessCapability", "SNSTopic"),
        ("BusinessCapability", "SQSQueue"),
    },
}

_WHY = {
    ("DependsOn", "BusinessCapability", "RDSCluster"): "93e3120 类型展开把 Grafana 的 aurora-mysql 接上了",
    ("DependsOn", "BusinessCapability", "SNSTopic"): "93e3120 方向反了：告警 topic 观测该能力，不是它的依赖",
    ("DependsOn", "BusinessCapability", "SQSQueue"): "93e3120 方向反了：DLQ 承接该能力，不是它的依赖",
}


def _load_contract() -> dict:
    with _CONTRACT.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── t67_01：名单里的每一项都必须在契约里真实声明 ──────────────────────────────
#
# 防的是名单本身腐烂：契约改了对偶、名单没跟上，于是门禁在守一个
# 已经不存在的形状 —— 永远绿，且掩盖了「这个决定已经失去载体」。
def test_t67_01_listed_pairs_are_declared_in_contract():
    contract = _load_contract()
    edge_types = contract.get("edge_types") or {}

    problems: list[str] = []
    for etype, pairs in MUST_STAY_EMPTY.items():
        spec = edge_types.get(etype)
        if spec is None:
            problems.append(f"{etype}: 契约里没有这个边类型")
            continue
        declared = {tuple(p) for p in (spec.get("pairs") or []) if len(p) == 2}
        for pair in sorted(pairs):
            if pair not in declared:
                problems.append(
                    f"{etype} {pair[0]}->{pair[1]}: 契约的 pairs 里没有这一组。"
                    f" 若是刻意从契约删除的，请连带从 MUST_STAY_EMPTY 移除")

    assert not problems, "MUST_STAY_EMPTY 与契约脱节:\n  " + "\n  ".join(problems)


# ── t67_02：每一项都必须有说明依据 ────────────────────────────────────────────
def test_t67_02_every_entry_has_a_documented_reason():
    missing = [
        f"{et} {s}->{d}"
        for et, pairs in MUST_STAY_EMPTY.items()
        for (s, d) in sorted(pairs)
        if not (_WHY.get((et, s, d)) or "").strip()
    ]
    assert not missing, (
        "这些项没有说明为什么必须为零（_WHY 里缺条目）:\n  " + "\n  ".join(missing)
        + "\n只凭『现在是 0 条』不构成锁定理由。")


# ── t67_03：活图谱里这些对偶必须真的是零条 ────────────────────────────────────
@pytest.mark.neptune
def test_t67_03_pairs_are_actually_empty_in_graph():
    if os.environ.get("GDP_OFFLINE"):
        pytest.skip("GDP_OFFLINE：本用例需要真实 Neptune")

    from neptune import neptune_client as neptune_rca

    violations: list[str] = []
    for etype, pairs in MUST_STAY_EMPTY.items():
        for src, dst in sorted(pairs):
            rows = neptune_rca.results(
                f"MATCH (s:{src})-[r:{etype}]->(t:{dst}) "
                "RETURN count(r) AS c, collect(DISTINCT t.name)[0..4] AS samples")
            row = (rows or [{}])[0]
            count = int(row.get("c") or 0)
            if count:
                why = _WHY.get((etype, src, dst), "")
                violations.append(
                    f"{src} -[{etype}]-> {dst}: 出现了 {count} 条"
                    f"（样例 {row.get('samples')}）。"
                    f" 这个形状被锁定为零的原因：{why}")

    assert not violations, (
        "\n\n有形状被重新打开了 —— 极可能是 business_layer.py 的\n"
        "SKIP_BC_INFRA_LABELS 被删或绕过（按类型展开会造假边）：\n\n  "
        + "\n  ".join(violations)
        + "\n\n处置：先读 infra/lambda/etl_aws/business_layer.py 的注释与提交 93e3120。"
        "\n若确认是按**资源名**正确派生出来的新实现，则连带从 MUST_STAY_EMPTY 移除，"
        "\n并在提交说明里写清为什么不会重犯类型展开的错。")


# ── t67_04：ETL 侧的守卫必须还在 ──────────────────────────────────────────────
#
# t67_03 只能在假边**已经写进图谱之后**变红。这一条更早：守卫被删的那一刻就红。
def test_t67_04_etl_guard_still_present():
    path = _ROOT / "infra" / "lambda" / "etl_aws" / "business_layer.py"
    assert path.is_file(), f"找不到 {path}"
    src = path.read_text(encoding="utf-8")

    assert "SKIP_BC_INFRA_LABELS" in src, (
        "business_layer.py 里的 SKIP_BC_INFRA_LABELS 不见了。"
        " 它是防 BusinessCapability 按类型展开产生假边的守卫，"
        " 删掉会让 6 条假边（含把 Grafana aurora-mysql 接上业务能力）重新出现。"
        " 见提交 93e3120。")

    # 守卫必须覆盖被锁定的那些 dst 标签，否则等于只剩个名字。
    need = {dst for pairs in MUST_STAY_EMPTY.values() for (_s, dst) in pairs}
    # 只看赋值那一行，避免匹配到注释里复述标签名。
    guard_line = next(
        (ln for ln in src.splitlines() if "SKIP_BC_INFRA_LABELS" in ln and "=" in ln
         and not ln.lstrip().startswith("#")), "")
    missing = sorted(lbl for lbl in need if lbl not in guard_line)
    assert not missing, (
        f"SKIP_BC_INFRA_LABELS 没有覆盖这些被锁定的目标标签: {missing}\n"
        f"守卫那一行: {guard_line.strip()[:160]}")

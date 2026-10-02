"""test_122_pod_queries_must_filter_active.py — 查 Pod 的 cypher 必须过滤失活。

## 守什么

`expire_stale_nodes`（2026-09-04 引入）会把超期节点标记 `active=false`，实测
1,466 个陈旧 `Pod` **全部**被正确标记、一个不漏。但在 2026-10-02 之前，
**没有一个消费方读这个标记** —— RCA 5 处、DR 2 处共 7 个查询都没有过滤。

机制写了没人读，于是标记等于不存在。实测代价（2026-10-02，petsite）：

    MATCH (svc {name:'petsite'})-[:RunsOn]->(pod:Pod)
      过滤前   855 个 Pod
      过滤后     2 个          ← 427 倍

而 `q6_pod_status` 的查询带 `ORDER BY pod.restarts DESC`：

      活跃 Pod 的 max(restarts)  = 0     ← 当前两个副本都健康
      含历史的 max(restarts)     = 2     ← 排首位的是早已消失的 Pod

所以 RCA 诊断 petsite 时会把一个 `restarts=2` 的**死** Pod 排在最前面，
报告「有 Pod 在反复重启」—— 在故障中把工程师引向完全错误的结论。
AZ 维度更糟：`count(pod)` 是「受影响比例」的分母，1,548/74 ≈ 21 倍高估
会让 AZ 故障的影响面看起来比实际小一个数量级。

## 为什么必须是 coalesce(pod.active, true)，不能是 pod.active = true

活跃节点的 `active` 是 **NULL**，不是 `true` —— 过期机制只给失活的写
`false`，不给新鲜的写 `true`。实测（1,548 个 Pod）：

    coalesce(p.active, true)              →   74   ✓
    p.active = true                       →    0   ← 把查询变成空结果
    p.active <> false OR p.active IS NULL →   74   ✓ 等效但啰嗦

`= true` 是这个修复最容易被「顺手改简洁」改坏的地方：它不报错、不告警，
只是让每个 Pod 查询静默返回空。所以本门禁同时禁掉它。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 扫描范围：这些文件里的 cypher 会查到 Pod。新增查 Pod 的模块要加进来。
SCANNED = (
    "rca/neptune/neptune_queries.py",
    "dr-plan-generator/graph/queries.py",
)

#: 认可的过滤写法。`coalesce` 是推荐形式，第二种是等效的显式写法。
_OK_FILTERS = (
    re.compile(r"coalesce\s*\(\s*\w+\.active\s*,\s*true\s*\)", re.I),
    re.compile(r"\w+\.active\s*<>\s*false", re.I),
)

#: 禁止的写法 —— 活跃**节点**的 active 是 NULL，`= true` 恒为空集。
#: ⚠️ 只适用于节点。**边**的 active 确实写 true（实测
#: `payforadoption -[Calls]-> petsearch` 的 active=true），所以
#: `rca/neptune/neptune_queries.py` 里针对边的 `r.active = true` 是正确的，
#: 不在本规则范围内。两者语义不对称：
#:     Pod 节点   true=0      false=1466   NULL=79   ← 只给失活的写 false
#:     依赖边     有 true     有 false     有 NULL
#: 所以本规则按**变量名**限定在该 cypher 块声明的 Pod 变量上。
_BAD_FILTER_TMPL = r"{var}\.active\s*=\s*true"

#: 三引号 cypher 块
_BLOCK = re.compile(r'"""(.*?)"""', re.S)

#: 从 cypher 块里抓出绑定到 Pod 的变量名，如 `(pod:Pod)` → pod
_POD_VAR = re.compile(r"\(\s*(\w+)\s*:\s*Pod\b")


def _cypher_blocks_touching_pod() -> list[tuple[str, int, str]]:
    """返回 [(相对路径, 起始行号, 块内容)]，只含真正 MATCH 到 Pod 的块。"""
    found: list[tuple[str, int, str]] = []
    for rel in SCANNED:
        path = ROOT / rel
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        for m in _BLOCK.finditer(src):
            blk = m.group(1)
            # 只看真的在图模式里引用 Pod 的块；docstring 里提到 Pod 不算。
            if not re.search(r"\(\s*\w+\s*:\s*Pod\b", blk):
                continue
            if "MATCH" not in blk.upper():
                continue
            line = src[: m.start()].count("\n") + 1
            found.append((rel, line, blk))
    return found


def test_t122_01_scanned_files_exist():
    """t122-01: 扫描范围里的文件必须存在 —— 文件改名后门禁不能静默空跑。

    这道断言是前提：若 SCANNED 里的路径失效，下面两条会扫到 0 个块而
    「全部通过」，门禁就变成了装饰。
    """
    missing = [rel for rel in SCANNED if not (ROOT / rel).exists()]
    assert not missing, (
        f"这些被扫描的文件不存在：{missing}。文件被移动过就更新 SCANNED，"
        "否则本门禁会扫到 0 个 cypher 块并假装通过。"
    )


def test_t122_02_found_pod_queries_is_nonempty():
    """t122-02: 必须至少扫到若干个查 Pod 的 cypher 块。

    同上理由：扫不到就等于没守。2026-10-02 的实际数量是 7 个
    （RCA 5 + DR 2），这里只要求 >= 5，留出重构余量。
    """
    blocks = _cypher_blocks_touching_pod()
    assert len(blocks) >= 5, (
        f"只扫到 {len(blocks)} 个查 Pod 的 cypher 块，预期至少 5 个。"
        "要么正则失效，要么查询被挪到了 SCANNED 之外的文件。"
    )


def test_t122_03_every_pod_query_filters_active():
    """t122-03: 每个 MATCH 到 Pod 的 cypher 都必须过滤失活节点。

    不过滤的后果不是数字难看，而是**错误陈述**：实测 petsite 的 Pod 查询
    过滤前 855 个、过滤后 2 个，且排首位的死 Pod 带着 restarts=2。
    """
    offenders = []
    for rel, line, blk in _cypher_blocks_touching_pod():
        if not any(p.search(blk) for p in _OK_FILTERS):
            first_match = next(
                (ln.strip() for ln in blk.splitlines() if "MATCH" in ln.upper()), ""
            )
            offenders.append(f"{rel}:{line}  {first_match[:90]}")

    assert not offenders, (
        "这些 cypher 查到了 Pod 但没有过滤 active：\n  "
        + "\n  ".join(offenders)
        + "\n\n加 `WHERE coalesce(pod.active, true)`。"
        "不加的后果见本文件 docstring —— 1,466 个失活 Pod 会混进结果，"
        "而 ORDER BY restarts DESC 会把早已消失的 Pod 排在最前面。"
    )


def test_t122_04_no_active_equals_true_on_pod_vars():
    """t122-04: 禁止对 Pod 变量写 `active = true` —— 它恒为空集。

    活跃**节点**的 active 是 NULL（过期机制只给失活的写 false）。实测
    1,548 个 Pod 里 `p.active = true` 匹配 **0** 个。这个写法不报错、
    不告警，只是让查询静默返回空 —— 比不过滤更危险。

    ⚠️ 本规则只管 Pod 变量。**边**的 active 确实写 true，所以同一文件里
    针对边的 `r.active = true` 是正确的，不在此列 —— 这个不对称是本门禁
    第一版的 bug，写它的时候我自己踩了一次。
    """
    offenders = []
    for rel, line, blk in _cypher_blocks_touching_pod():
        for var in set(_POD_VAR.findall(blk)):
            bad = re.compile(_BAD_FILTER_TMPL.format(var=re.escape(var)), re.I)
            if bad.search(blk):
                offenders.append(f"{rel}:{line}  变量 {var}")

    assert not offenders, (
        "这些地方对 Pod 变量用了 `active = true`，它在本图谱上恒为空集：\n  "
        + "\n  ".join(offenders)
        + "\n\n活跃节点的 active 是 NULL，不是 true。"
        "改用 `coalesce(x.active, true)`。"
    )


@pytest.mark.parametrize("bad_cypher", [
    "MATCH (svc)-[:RunsOn]->(pod:Pod) RETURN pod.name",
    "MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:LocatedIn]->(z) RETURN count(p)",
])
def test_t122_05_detector_catches_unfiltered(bad_cypher):
    """t122-05: 反向验证 —— 检测逻辑必须能抓住没过滤的写法。

    门禁自己要能挂靶，否则它可能因为正则写错而永远通过。
    """
    assert re.search(r"\(\s*\w+\s*:\s*Pod\b", bad_cypher), "样本应被识别为查 Pod"
    assert not any(p.search(bad_cypher) for p in _OK_FILTERS), (
        "样本没有 active 过滤，检测逻辑却认为它有 —— 正则写反了"
    )


def test_t122_06_detector_accepts_filtered():
    """t122-06: 反向验证 —— 正确写法必须被放过，不能误报。"""
    good = (
        "MATCH (svc)-[:RunsOn]->(pod:Pod)\n"
        "WHERE coalesce(pod.active, true)\n"
        "RETURN pod.name"
    )
    assert any(p.search(good) for p in _OK_FILTERS), "coalesce 写法被误判为未过滤"
    bad = re.compile(_BAD_FILTER_TMPL.format(var="pod"), re.I)
    assert not bad.search(good), "coalesce 写法被误判为 `= true`"


def test_t122_07_edge_active_true_is_not_flagged():
    """t122-07: 针对**边**的 `active = true` 不得被判违规。

    这是本门禁第一版的 bug：它禁掉了所有 `x.active = true`，于是把
    `rca/neptune/neptune_queries.py` 里正确的边过滤
    （`r.dependency_kind = 'dynamic' AND r.active = true`）也抓了。

    边与节点的 active 语义不对称 —— 边上确实写 true（实测
    `payforadoption -[Calls]-> petsearch` 的 active=true），而 Pod 节点
    从不写 true。规则必须按变量区分，不能按属性名一刀切。
    """
    blk = (
        "MATCH (a)-[r:Calls]->(b)\n"
        "WHERE r.dependency_kind = 'dynamic' AND r.active = true\n"
        "RETURN a.name"
    )
    # 这个块没有 Pod，本就不该进扫描范围
    assert not _POD_VAR.findall(blk), "样本不含 Pod 变量"

    # 即使同一个块里既有 Pod 又有边过滤，也只该检查 Pod 变量
    mixed = (
        "MATCH (svc)-[:RunsOn]->(pod:Pod)\n"
        "WHERE coalesce(pod.active, true)\n"
        "OPTIONAL MATCH (svc)-[r:Calls]->(dst)\n"
        "  WHERE r.active = true\n"
        "RETURN pod.name, dst.name"
    )
    pod_vars = set(_POD_VAR.findall(mixed))
    assert pod_vars == {"pod"}
    for var in pod_vars:
        bad = re.compile(_BAD_FILTER_TMPL.format(var=re.escape(var)), re.I)
        assert not bad.search(mixed), (
            "混合块里边的 r.active = true 被错当成 Pod 变量的违规"
        )

"""test_66_observation_not_in_intervention_slot.py — 观测不得占用干预槽。

## 缺陷（2026-09-09 修）

3 条 agent 域的边带着 `verify_status=confirmed`、`verify_degradation=100.0`、
`verify_confirm_count=1`、`verify_by=chaos-runner`，而它们的
`verify_experiment` 自述的是**观测**：

    agent-invoke:orchestrator log adoption x19 in one request
    agent-invoke:bedrock KB Invocations=7 during probe window

「日志里数到 19 次调用」「探测窗口内 Invocations=7」都是被动计数。
**没有任何东西被打断，也就没有任何退化被测量。**

## 两处后果

**① 置信度虚高。** 契约里 `intervention_confirmed=4.0` 对
`observed_per_source=0.5` —— 对数几率上差 8 倍。实测 0.9890，
按纯观测重算是 0.6225。

**② `verify_degradation=100.0` 是编造的数字。** 一个 agent 读
`q22_edge_verification_verdicts` 会看到「退化 100%」，于是报告
「故障注入以 100% 退化确认了这条依赖」。那是假的。
`mcp/provenance.py` 的第一条规则正是「不要编造本 server 没有返回的数值」，
而这里是图**自己**在提供编造的数值 —— 项目要抓的错误出现在它自己的数据里。

## 判据为什么对准 degradation 而不是 experiment 文本

`verify_experiment` 的取值是自由文本，措辞会变（`agent-invoke:` 这个前缀是
写入方当时的习惯，不是契约）。而**「有 confirmed / refuted 判定就必须有
可测量的退化幅度」是语义必然**：判定来自干预，干预必然产生一个测量值。

所以门禁钉的是这条不变量，而不是某个字符串前缀。本会话在「判据对准字符串
而不是语法/语义位置」上栽过 11 次，这一条刻意反过来做。

## 来源

跨会话笔记 `todo/CROSS-SESSION-NOTE_20260905-0630.md` 里另一个会话独立发现
同一处，并写明「这是语义判断，留给你定」。修正记录在
`scripts/unfile_observation_from_intervention.py`。
"""
import os
import sys
from pathlib import Path

import pytest

from paths import PROJECT_ROOT

ONLINE = bool(os.environ.get('NEPTUNE_ENDPOINT'))
SCRIPT = (Path(PROJECT_ROOT) / 'scripts'
          / 'unfile_observation_from_intervention.py')


def _nc():
    for p in (str(PROJECT_ROOT), str(Path(PROJECT_ROOT) / 'rca')):
        if p not in sys.path:
            sys.path.insert(0, p)
    from neptune import neptune_client as nc      # type: ignore
    return nc


def _dep_labels() -> str:
    for p in (str(PROJECT_ROOT), str(Path(PROJECT_ROOT) / 'rca')):
        if p not in sys.path:
            sys.path.insert(0, p)
    from neptune.neptune_queries import _dependency_edge_labels  # type: ignore
    return ', '.join(f"'{x}'" for x in _dependency_edge_labels())


def test_m01_修正脚本必须在库里():
    """判定被撤销这件事要留痕，否则下一个人看到 untested 会以为从没测过。"""
    assert SCRIPT.exists(), (
        f'缺少 {SCRIPT}。三条边的 confirmed 判定是被**撤销**的，'
        '不是从来没有过 —— 撤销的理由与前后值必须留在库里。')
    src = SCRIPT.read_text(encoding='utf-8')
    assert '--dry-run' in src, '修正脚本必须支持 dry-run —— 它直接写生产图谱'
    assert 'id(r) = $eid' in src or 'id(r)=$eid' in src, (
        '修正脚本必须按边 id 定位。用 name 匹配会误伤同名节点。')


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m02_有判定就必须有可测量的退化幅度():
    """confirmed / refuted 只能来自干预，而干预必然产生一个测量值。

    这是语义不变量，不是书写惯例 —— 所以判据钉它，而不是钉
    `verify_experiment` 的文本前缀（那是自由文本，措辞会变）。
    """
    nc = _nc()
    rows = nc.results(
        f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_dep_labels()}] "
        "AND r.verify_status IN ['confirmed','refuted'] "
        "AND r.verify_degradation IS NULL "
        "RETURN s.name AS sn, t.name AS tn, type(r) AS et, "
        "r.verify_status AS vs, r.verify_experiment AS exp")
    bad = [f"{r['sn']} -[{r['et']}]-> {r['tn']}  {r['vs']}  exp={r['exp']}"
           for r in rows]
    assert not bad, (
        '这些边有 confirmed/refuted 判定却没有退化幅度：\n  '
        + '\n  '.join(bad) + '\n\n'
        '判定只能来自干预，而干预必然测到一个退化值。没有退化值说明'
        '这条判定不是干预产生的 —— 它应该是 untested，'
        '而它的观测证据归 q20_dependency_verification（观测层）。')


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m03_退化幅度不得出现在无判定的边上():
    """反向：没有判定却带着退化幅度，说明有人写了一个没有结论支撑的数字。"""
    nc = _nc()
    rows = nc.results(
        f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_dep_labels()}] "
        "AND coalesce(r.verify_status,'untested')='untested' "
        "AND r.verify_degradation IS NOT NULL "
        "RETURN s.name AS sn, t.name AS tn, "
        "r.verify_degradation AS deg")
    bad = [f"{r['sn']} → {r['tn']}  退化={r['deg']}" for r in rows]
    assert not bad, (
        '这些边是 untested 却带着退化幅度：\n  ' + '\n  '.join(bad) + '\n\n'
        '一个没有结论支撑的测量值会被下游当成证据 —— '
        'agent 读到「退化 100%」就会报告注入确认过这条依赖。')


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m04_置信度不得越界():
    """另一个会话修过 11 条 ±4.0 越界（直接写了证据权重而非归一化值）。

    这里一并守住：那道 `authority=chaos-runner` 门禁只校验「谁在写」，
    不校验「写的值是否合法」。
    """
    nc = _nc()
    rows = nc.results(
        f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_dep_labels()}] "
        "AND r.verify_confidence IS NOT NULL "
        "AND (r.verify_confidence < 0 OR r.verify_confidence > 1) "
        "RETURN s.name AS sn, t.name AS tn, r.verify_confidence AS c")
    bad = [f"{r['sn']} → {r['tn']}  conf={r['c']}" for r in rows]
    assert not bad, (
        f'这些边的 verify_confidence 越界（契约值域 [0,1]）：\n  '
        + '\n  '.join(bad) + '\n\n'
        '成因通常是直接写了 evidence_weights 里的**证据权重**（±4.0 等）'
        '而不是归一化后的置信度。')

"""
test_135_agentcore_tool_chaos_names_must_exist.py

守「agent 工具级故障实验里的工具名必须真实存在」。

## 要防的是什么

2026-10-07 取用 aws-samples/fis-template-library 的
`agentcore-strands-agent-faults` 模板（P4）。它的 README 自己警告：

  > Replace the example tool names before running. The shipped template's
  > default (`get_move`, `get_pokemon`) targets a demo agent's tools.
  > Tool names are matched exactly against your agent's tools, and a name
  > that matches nothing injects nothing: run the template unmodified
  > against your own agent and the experiment will complete "successfully"
  > while injecting no faults at all.

**名字对不上就什么都不注入，而实验会「成功」完成。** 这是本仓反复出现的
那类缺陷的又一例：系统报告成功，同时悄悄什么都没做。而且它比别的更阴 ——
一个绿色的实验会被当成「这条边已验证」，写进覆盖率，喂给 DR 决策。

## 为什么本仓能守住这条

因为我们有依赖图。`AgentTool` 节点与 `InvokesTool` 边给出了真实的
runtime → tool 映射，所以「实验里写的工具名是否存在、是否真的属于那个
runtime」是可判定的。

「从依赖图推出该验证什么」正是本平台的差异点 ——
这里恰好把它用在了一个上游模板留下的坑上。

## 分层

- 静态部分（不连图）：工具名非空、不是上游模板的 demo 名、
  前置条件未就绪时必须 enabled: false。**这些永远跑。**
- 图校验部分：工具名必须在图里存在且归属正确。
  GDP_OFFLINE=1 或连不上图时 skip —— 门禁不能因为网络抖动把 CI 搞红，
  但静态那几条足以拦住照搬模板这个最常见的错。
"""
from __future__ import annotations

import os
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXP_DIR = ROOT / 'chaos' / 'code' / 'runner'
AGENTCORE_DIR = ROOT / 'chaos' / 'code' / 'experiments' / 'fis' / 'agentcore'

# 上游模板的 demo 工具名。出现在我们的实验里就是照搬没改。
UPSTREAM_DEMO_TOOLS = {'get_move', 'get_pokemon'}

FAULT_TYPE = 'fis_agentcore_tool_chaos'


def _agentcore_experiments() -> list[tuple[pathlib.Path, dict]]:
    out = []
    if not AGENTCORE_DIR.is_dir():
        return out
    for p in sorted(AGENTCORE_DIR.glob('*.yaml')):
        d = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
        if (d.get('fault') or {}).get('type') == FAULT_TYPE:
            out.append((p, d))
    return out


def _tool_names(d: dict) -> list[str]:
    raw = ((d.get('fault') or {}).get('extra_params') or {}).get('tool_names', '')
    return [t.strip() for t in str(raw).split(',') if t.strip()]


# --------------------------------------------------------------- 静态门禁

def test_t135_01_fault_type_registered():
    """故障类型必须在统一目录里注册，否则 runner 认不出来。"""
    cat = yaml.safe_load((EXP_DIR / 'fault_catalog.yaml').read_text(encoding='utf-8'))
    types = {e['type'] for v in cat.values() if isinstance(v, list) for e in v}
    assert FAULT_TYPE in types, f"{FAULT_TYPE} 未在 fault_catalog.yaml 注册"


def test_t135_02_experiments_exist():
    exps = _agentcore_experiments()
    assert exps, f"{AGENTCORE_DIR} 下没有 {FAULT_TYPE} 类型的实验"


def test_t135_03_tool_names_present_and_nonempty():
    """tool_names 必须显式给且非空 —— 空值等于零注入。"""
    for p, d in _agentcore_experiments():
        tools = _tool_names(d)
        assert tools, (
            f"{p.name}: tool_names 为空。工具名是精确匹配的，"
            "空值会让实验「成功」完成而一个故障都没注入。"
        )


def test_t135_04_not_upstream_demo_names():
    """不得照搬上游模板的 demo 工具名。

    这是最常见的错法，也是 README 专门警告的那个。
    """
    for p, d in _agentcore_experiments():
        leaked = set(_tool_names(d)) & UPSTREAM_DEMO_TOOLS
        assert not leaked, (
            f"{p.name}: 工具名 {sorted(leaked)} 是上游模板的 demo 名"
            "（某个 pokemon 示例 agent 的工具），我们这里不存在。"
            "照搬会得到一个绿色的、零注入的实验。"
        )


def test_t135_05_disabled_until_prereqs_ready():
    """前置条件未就绪时必须 enabled: false。

    三件前置（SSM 文档 ChaosExperiment、三个 IAM 角色、
    agent 构建里 vendored 的 strands_agentcore_chaos.py）任一缺失，
    实验跑起来都是静默零注入。留着 enabled: true 会让人误以为有覆盖。

    这条门禁要在前置条件真的就绪后手工放开 —— 届时请连同本 docstring
    一起更新，说明是哪一天、以什么方式确认就绪的。
    """
    for p, d in _agentcore_experiments():
        assert d.get('enabled') is False, (
            f"{p.name}: enabled 不是 false。SSM 文档 / IAM 角色 / "
            "agent 构建里的 chaos 模块三件前置尚未就绪，"
            "此时跑它只会静默零注入，却会被当成「这条边已验证」。"
        )


def test_t135_06_has_real_stop_conditions():
    """必须有真实熔断 —— 上游模板写的是 stopConditions: [{source: none}]。"""
    for p, d in _agentcore_experiments():
        sc = d.get('stop_conditions') or []
        assert sc, (
            f"{p.name}: 没有 stop_conditions。上游模板给的是 "
            '`[{"source": "none"}]`（无熔断），README 只要求用户自备。'
        )
        for c in sc:
            assert c.get('action') == 'abort', \
                f"{p.name}: stop_condition 的 action 不是 abort"


def test_t135_07_uses_during_steady_state():
    """必须用上 during 时机（P3 能力）。

    agent 编造答案是**瞬时**行为 —— 被注入的那次 invocation 失稳，
    下一次可能就正常了。只在首尾各看一眼会完全漏掉。
    """
    for p, d in _agentcore_experiments():
        ss = d.get('steady_state') or {}
        assert ss.get('during'), (
            f"{p.name}: 没有 steady_state.during。agent 层的失稳是瞬时的，"
            "首尾两次求值抓不到。"
        )


def test_t135_08_graph_feedback_off_while_disabled():
    """未就绪期间不得写图谱证据 —— 否则会落一批「零注入得出的结论」。"""
    for p, d in _agentcore_experiments():
        if d.get('enabled') is False:
            gf = d.get('graph_feedback') or {}
            assert gf.get('enabled') is not True, (
                f"{p.name}: 实验 disabled 但 graph_feedback 开着。"
                "前置条件未就绪时的实验结果是零注入，写进图谱就是假证据。"
            )


# --------------------------------------------------------- 图校验（可 skip）

def _graph_tools(nc) -> dict[str, set[str]]:
    """返回 {runtime_name: {tool_name, ...}}，取自 InvokesTool 边。

    ⚠️ 必须走 `neptune_rca` fixture 的 openCypher 通道，**不能**直接
    `from neptune_client_base import neptune_query` —— conftest 刻意把
    `neptune_client_base` 换成了桩（它在生产里来自 Lambda Layer，
    会发真实 SigV4 HTTP 请求，测试里必须桩掉）。
    直接 import 的后果是静默拿到空结果，于是这条图校验永远 skip ——
    一个「因为探测不到所以永远不报警」的门禁，比没有门禁更坏。
    （这一版就是这么翻过一次车的：本地直连拿到 8 条边，
      进了 tests/ 却报「图里没有 InvokesTool 边」。）
    """
    rows = nc.results(
        "MATCH (rt)-[:InvokesTool]->(t) RETURN rt.name AS rt, t.name AS tool"
    )
    out: dict[str, set[str]] = {}
    for r in rows or []:
        rt, tool = r.get('rt'), r.get('tool')
        if rt and tool:
            out.setdefault(str(rt), set()).add(str(tool))
    return out


@pytest.mark.neptune
def test_t135_09_tool_names_exist_in_graph(neptune_rca):
    """工具名必须在依赖图里存在，且归属声明的那个 runtime。

    这是本文件的核心 —— 也是只有本仓做得到的那一条：
    有依赖图，「这个工具名是否真的属于这个 agent」才是可判定的。
    """
    if os.environ.get('GDP_OFFLINE') == '1':
        pytest.skip('GDP_OFFLINE=1')
    graph = _graph_tools(neptune_rca)
    assert graph, (
        "图里查不到任何 InvokesTool 边。这**不是**跳过的理由 —— "
        "本仓确实有 6 个 AgentRuntime 与 8 条 InvokesTool 边（2026-10-07 实测），"
        "查不到说明查询通道或图数据出了问题，而不是「没有可校验的东西」。"
    )

    problems = []
    for p, d in _agentcore_experiments():
        rt = ((d.get('fault') or {}).get('extra_params') or {}).get('runtime_id')
        known = graph.get(str(rt), set())
        if not known:
            problems.append(f"  {p.name}: runtime_id={rt!r} 在图里没有任何 InvokesTool 边")
            continue
        for t in _tool_names(d):
            if t not in known:
                problems.append(
                    f"  {p.name}: 工具 {t!r} 不属于 {rt!r}"
                    f"（图里它的工具是 {sorted(known)}）"
                )
    assert not problems, (
        "实验里的工具名与依赖图不符 —— 工具名是精确匹配的，"
        "对不上就什么都不注入而实验照样「成功」。\n" + "\n".join(problems)
    )

"""tests/test_57_inbound_reachability.py — G2：可达性门禁

## 这条门禁在解什么问题

2026-09-05 追查「5 条 `RoutesTo` 越界带 `dependency_kind`」时，**偶然**发现
`AgentGateway` 在全图零入边 —— 而它是 5 个 agent runtime 的路由枢纽。
后果不是记账错误，而是**图对一个真实的单点故障保持沉默**：
影响面分析会算出「网关挂了业务仍然可用」，比算不出答案更糟。

问题在于发现机制：那是在查另一件事时撞见的，不是一种可靠的发现方式。
本文件把「有没有漏建模一条入站依赖」变成**可自动检测的图性质**。

## 判据不能是「零入边即告警」

实测（2026-09-07）否掉了这个朴素判据：

- `LoadBalancer` 5 个**全部**零入边 —— 它们是合法的图根，
  流量来自互联网，而互联网不在建模范围内。
- `AgentRuntime/graph_dependency_mcp` 零入边 —— 它是**本平台自己**的
  MCP server（`scope=platform`），调用方是 Kiro，不是被观测系统。

所以判据是三层：

1. 只查**显式登记**为「必须有上游」的节点类型（`MUST_HAVE_INBOUND`）；
2. 只查 `scope='observed'` 的实例 —— 平台自身、外部、未定域的一律豁免；
3. 已知未修的缺口进 `KNOWN_GAPS` 白名单，**带原因和跟踪去处**。

第 3 层是刻意的：让门禁**现在就是绿的**，从而任何**新增**的零入边节点会立刻
变红；同时把当前欠账写在代码里、必须逐条销账，而不是塞进一个永久 skip。
与 `test_35::g18`「改分类就得改测试」是同一套思路。
"""
from __future__ import annotations

import pytest

#: 必须有入边的节点类型 —— 即「被观测系统里一定有东西调用它」。
#
# 登记依据是**该类型在真实拓扑里必然有上游**，不是「图里现在恰好有入边」：
#   AgentGateway  网关存在的意义就是被调用；零入边意味着调用方没被建模
#   AgentRuntime  runtime 必须被 petsite、被别的 agent、或被网关调用
#   TargetGroup   必然有一个 LoadBalancer 转发给它
MUST_HAVE_INBOUND = {
    'AgentGateway',
    'AgentRuntime',
    'TargetGroup',
}

#: 合法的图根 —— 登记在此的类型**不要**加进上面。留作文档，防止后人误加。
#
#   LoadBalancer  互联网入口，上游不在建模范围
#   Microservice  可能是最上游的业务入口
LEGITIMATE_ROOTS = {
    'LoadBalancer',
    'Microservice',
}

#: 已知未修的缺口。**每条都要写原因和跟踪去处，修好后必须删除本行。**
#
# 格式: (节点类型, 节点 name) -> 原因
KNOWN_GAPS = {
    ('AgentGateway', 'WaggleAIGateway'):
        '缺 AgentRuntime -[RoutesVia]-> AgentGateway（agent 间调用经网关）。'
        '方案见 todo/agentobv/agent-layer-taxonomy-design_20260905-1640.md §3.2(b)，'
        '待 M1–M6 落地。'
        '注意：**不是**缺 Microservice -> AgentGateway —— PetSite 直调 runtime，'
        '不经网关，见该文档 §3.2(a) 的推翻记录。',
    ('AgentRuntime', 'WaggleAIConcierge'):
        '2026-09-07 已定性：**时序错位，不是采集缺口、也不是真没调用**，无需改代码。'
        'etl_agentcore 的 _DELEGATION_TOOLS 表原先只有 concierge / ordering，'
        '而 orchestrator 实际注册的 tool 名是 concierge_chat / food_ordering，'
        '查表落空导致 Delegates 边建不出来；该映射已于 2026-09-06 补上。'
        '修复生效有实证：Delegates -> WaggleAIOrdering 的 first_seen 是 09-06 02:38，'
        '晚于修复。而 concierge_chat 最后一次被调用是 09-05 09:16（早于修复），'
        '此后未再调用，其 InvokesTool 边已被正确标为 '
        'drift_status=observed_then_silent。'
        '**下一次真实调用发生时 Delegates 边会自然建出**，届时本行应删除。'
        '刻意不硬插边：Delegates 是观测驱动的，凭「对称性应该有」插边等于'
        '把未观测到的关系写成观测事实。',
}

_SCOPE_IN_SCOPE = 'observed'


@pytest.fixture(scope='module')
def zero_inbound(neptune_rca):
    """返回 [(label, name)]：`MUST_HAVE_INBOUND` 类型里 scope=observed 且零入边的实例。"""
    labels = sorted(MUST_HAVE_INBOUND)
    # openCypher 不便直接传集合参数，标签集合是本文件内的字面量常量，
    # 逐类型查询即可，规模很小（三类、几十个节点）。
    found = []
    for lb in labels:
        rows = neptune_rca.results(
            f"MATCH (n:{lb}) "
            f"WHERE n.scope = '{_SCOPE_IN_SCOPE}' "
            f"OPTIONAL MATCH (x)-[r]->(n) "
            f"WITH n, count(r) AS inbound "
            f"WHERE inbound = 0 "
            f"RETURN n.name AS name"
        )
        for r in rows:
            name = r.get('name') if isinstance(r, dict) else None
            if name:
                found.append((lb, name))
    return sorted(found)


@pytest.mark.neptune
def test_g2_01_no_new_unreachable_observed_nodes(zero_inbound):
    """被观测域内、声明必须有上游的节点，不得零入边（已知缺口除外）。

    **这条会红的典型场景**：新接了一个 ETL 写进一批 AgentRuntime，
    但忘了写「谁调用它们」的边 —— 于是图里多出一批不可达节点，
    影响面分析会认为它们不受任何上游故障影响。
    """
    unexpected = [(lb, nm) for lb, nm in zero_inbound
                  if (lb, nm) not in KNOWN_GAPS]
    assert not unexpected, (
        "以下节点在被观测域内却零入边，说明**有一条入站依赖没被建模**：\n  "
        + "\n  ".join(f"{lb}/{nm}" for lb, nm in unexpected)
        + "\n\n零入边不等于没有上游，只等于上游没进图。"
          "影响面分析会因此认为它们不受任何上游故障影响 —— 这是偏乐观的错误答案。\n"
          "若确认该类型本就是图根，请把它从 MUST_HAVE_INBOUND 移到 LEGITIMATE_ROOTS；\n"
          "若是暂时修不了的欠账，加进 KNOWN_GAPS 并写明原因与跟踪去处。"
    )


@pytest.mark.neptune
def test_g2_02_known_gaps_are_still_real(zero_inbound):
    """`KNOWN_GAPS` 里的条目必须仍然成立 —— 修好了就要销账。

    没有这一条，白名单会变成只增不减的坟场：缺口修好后条目还在，
    下一个人无法分辨哪些是真欠账。**这条红了是好事**，照提示删行即可。
    """
    still_zero = set(zero_inbound)
    stale = [(lb, nm) for (lb, nm) in KNOWN_GAPS if (lb, nm) not in still_zero]
    assert not stale, (
        "以下 KNOWN_GAPS 条目已经不再零入边（缺口已修），请从 KNOWN_GAPS 删除：\n  "
        + "\n  ".join(f"{lb}/{nm}" for lb, nm in stale)
        + "\n\n白名单只减不增才有意义。"
    )


def test_g2_03_root_types_not_registered_as_must_have_inbound():
    """合法图根不得同时登记为「必须有入边」—— 纯静态一致性检查。

    实测教训：`LoadBalancer` 5 个全部零入边。若把它加进 MUST_HAVE_INBOUND，
    门禁会立刻产出 5 条假告警，而假告警会让整条门禁被当成噪声关掉。
    """
    overlap = MUST_HAVE_INBOUND & LEGITIMATE_ROOTS
    assert not overlap, (
        f"这些类型既登记为必须有入边、又登记为合法图根，自相矛盾: {sorted(overlap)}"
    )

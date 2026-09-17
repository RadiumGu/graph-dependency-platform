"""tests/test_75_alarm_triggers_investigation.py

告警自动触发 DevOps Agent 调查的接线门禁。

## 为什么不是 `create-trigger`

2026-09-15 实测：`aws devops-agent create-trigger` 的 `--condition` 是
tagged union，CLI 文档原文：

    NOTE: This is a Tagged Union structure. Only one of the following
          top level keys can be set: schedule.

**只支持定时触发，没有告警条件。** 所以这件事必须走别的路。
AWS 工作坊正文也说了同一件事：labs 用 CLI 直调是为了自包含，
生产环境的 alarm-driven investigation 由 Lambda 调用。

## 为什么接在 AlertBuffer 的 `is_first` 上

`rca/handler.py` 已经在接 CloudWatch Alarm 的 SNS 事件并做窗口去重。
新建一个 Lambda 等于把告警接入做两遍，两份去重逻辑会漂移。

接在 `is_first=True` 那一刻：**同一个故障只发起一次调查**。
不去重的话一次告警风暴会打满 agent task 配额，
而后面那些任务查的是同一件事。
"""
from __future__ import annotations

import pathlib
import sys
import types

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'rca',):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _alert():
    return types.SimpleNamespace(
        service_name='petsite', alarm_name='petsite-5xx-high',
        metric='HTTPCode_Target_5XX_Count', fingerprint='abcd1234ef567890')


def test_t75_01_默认关闭():
    """默认不开。

    ⚠️ 理由不是"配额会耗尽" —— 实测 2026-09-15
    `get-account-usage` 显示 `monthlyAccountInvestigationHours.limit = -1`
    （无限制），半月用掉 0.54 小时。那个理由已被推翻。

    真实理由：每次首告警都创建一个 backlog task（任务列表会被真实告警
    填满），且调查结论会进 agent 的 system learning ——
    喂进去的告警质量影响它后续判断，而我们的告警里还有已知假警报来源。
    """
    import os

    from actions import devops_agent_trigger as t
    old = os.environ.pop('DEVOPS_AGENT_INVESTIGATE_ENABLED', None)
    try:
        assert t.enabled() is False, (
            '默认应关闭 —— 开启会让每次首告警都创建 backlog task，'
            '且结论进 agent 的 system learning')
        r = t.on_first_alert(_alert())
        assert 'skipped' in r
    finally:
        if old is not None:
            os.environ['DEVOPS_AGENT_INVESTIGATE_ENABLED'] = old


def test_t75_02_开启后写入证据纪律():
    import os

    from actions import devops_agent_trigger as t
    os.environ['DEVOPS_AGENT_INVESTIGATE_ENABLED'] = 'true'
    try:
        r = t.on_first_alert(_alert(), dry_run=True)
        assert 'payload' in r, f'未拼出 payload: {r}'
        d = r['payload']['description']
        # 证据纪律的四条要点
        for frag in ('q22_edge_verification_verdicts', 'refuted',
                     '不得用于推理', '实验模板'):
            assert frag in d, f'description 里缺「{frag}」'
    finally:
        os.environ.pop('DEVOPS_AGENT_INVESTIGATE_ENABLED', None)


def test_t75_03_必须声明这是生产告警而非演练():
    """工作坊记录了一个行为：DevOps Agent 识别出 CPU 尖峰是 FIS 注入的，
    就判定"这是演练"并**不给处置建议**。

    我们的混沌实验也走 FIS / Chaos Mesh，所以真实告警必须说清来源，
    否则可能被当成演练而拿不到处置计划。
    """
    import os

    from actions import devops_agent_trigger as t
    os.environ['DEVOPS_AGENT_INVESTIGATE_ENABLED'] = 'true'
    try:
        d = t.on_first_alert(_alert(), dry_run=True)['payload']['description']
        assert '生产告警' in d and '演练' in d, (
            'description 没声明这是生产告警 —— '
            'DevOps Agent 可能因看到 FIS/Chaos Mesh 活动而判定为演练、'
            '跳过处置建议')
    finally:
        os.environ.pop('DEVOPS_AGENT_INVESTIGATE_ENABLED', None)


def test_t75_04_异常不得影响告警主链路():
    """发起调查是增强。任何异常都要吞掉并记日志。"""
    import os

    from actions import devops_agent_trigger as t
    os.environ['DEVOPS_AGENT_INVESTIGATE_ENABLED'] = 'true'
    try:
        # 缺字段的对象：不该抛异常
        r = t.on_first_alert(object(), dry_run=True)
        assert isinstance(r, dict), '异常路径应返回 dict 而不是抛出'
    finally:
        os.environ.pop('DEVOPS_AGENT_INVESTIGATE_ENABLED', None)


def test_t75_05_handler接在is_first上而非每条告警():
    """接线门禁：必须在去重之后，否则告警风暴会打满 agent 配额。"""
    src = (ROOT / 'rca' / 'handler.py').read_text(encoding='utf-8')
    assert 'devops_agent_trigger' in src, 'handler 没接入调查发起'
    i_first = src.find('if is_first:')
    i_call = src.find('on_first_alert')
    assert i_first != -1, '找不到 `if is_first:` 守卫'
    assert i_first < i_call, (
        '调查发起没有被 `is_first` 守卫住 —— '
        '一次告警风暴会为同一个故障发起几十个调查，'
        '打满 agent task 配额，而它们查的是同一件事')


def test_t75_06_纪律文本不得复制一份():
    """description 的证据纪律必须来自 scripts/devops_agent_investigate。

    本仓库为「同类清单各处一份」付过代价（rca 那份依赖边清单少 Invokes，
    线上漏 16 条边）。
    """
    src = (ROOT / 'rca' / 'actions'
           / 'devops_agent_trigger.py').read_text(encoding='utf-8')
    assert 'devops_agent_investigate' in src, (
        '没有从 scripts/devops_agent_investigate import —— '
        '证据纪律文本会变成两份并漂移')
    assert '证据纪律（必须遵守' not in src, (
        '证据纪律文本被复制到了这里 —— 应当 import 而不是复制')


def test_t75_08_必须写明开启前提是部署而非环境变量():
    """`enabled()` 那个开关只有在接入代码已部署时才有意义。

    2026-09-17 实测踩到：

        petsite-rca-engine 代码最后部署于 2026-08-29
        rca/handler.py 的接入点提交于 2026-09-15

    也就是说**生产 Lambda 里没有 `on_first_alert` 的调用点**。
    此时给它加 `DEVOPS_AGENT_INVESTIGATE_ENABLED=true` 毫无作用 ——
    变量会被设上，但没有任何代码路径去读它。

    为什么值得一条门禁守着：这个状态的表现是"没有新 INVESTIGATION task"，
    而它与"开了但这段时间没有告警"**在数据上完全同形**。
    误判方向是危险的那一侧 —— 会让人以为闭环已经生效。

    本用例只钉文档，不钉部署状态（部署时间不该进单元测试）。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / 'rca' / 'actions'
           / 'devops_agent_trigger.py').read_text(encoding='utf-8')
    assert '部署' in src and 'deploy.sh' in src, (
        'devops_agent_trigger.py 里没有写明"开启前提是部署" —— '
        '下一个人会只加环境变量然后以为闭环已生效。\n'
        '必须写清：先部署 rca/ 到 petsite-rca-engine，再加环境变量。')
    assert 'limit=-1' in src, (
        '没有记录配额实测值 —— '
        '"配额会耗尽"这个已被推翻的理由会被重新写回来。')

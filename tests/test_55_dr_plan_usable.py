"""test_55_dr_plan_usable.py — 钉住 DR Plan 页「点一下就有真实计划」。

## 为什么需要

这一页的「🚀 生成 DR 计划」**一直生成不出计划**，而且连着三层原因，
每一层都不报错、只是把内容变空：

  ① 没设 workload profile。`dr_profile.get_active_profile()` **刻意无默认**
     （错的 profile 会静默产出指向错误域名/SSM 键/命名空间的计划，
     看起来正常直到执行），而页面从来没设过 `DR_PROFILE`。
     实测报的就是这条 `ProfileNotConfigured`。

  ② 默认参数用虚构 AZ 名。`apne1-az1` / `apne1-az2` / `apne1-az4` 在图谱里
     不存在（图谱是 `ap-northeast-1a/c/d`）。这套虚构命名贯穿整个
     dr-plan-generator（examples / fixtures / tests / README / SKILL.md，
     连 `graph/queries.py` 的 docstring 都写着 e.g. apne1-az1），
     **没有别名翻译层** —— AZ scope 从来只在合成 fixture 上验证过。

  ③ 即使换成真实 AZ 名，受影响服务仍是 0（`anchors=10, nodes=394` 但零匹配）。
     这是 dr-plan-generator 侧的锚定问题，不在本页范围内。

实测三种 scope：

    scope=az       source=apne1-az1（虚构）        受影响服务 0
    scope=az       source=ap-northeast-1a（真实）  受影响服务 0
    scope=service  source=petsite                  受影响服务 6   ← 只有这个能用

所以本页的默认必须落在 ③ 之外、真的能出计划的那个场景上。
"""
import os
import re
from pathlib import Path

import pytest

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
PAGE = _DEMO / 'pages' / '8_DR_Plan.py'


def _code_only() -> str:
    """剥掉 docstring 与注释再断言 —— 否则测的是我自己写的说明文字。

    本会话为这件事付过三次学费（见 todo/injection-found-defects #43 与
    tests/test_54 的同名函数）：拿源码文本做断言时，注释里出现的字符串会让
    正确的代码报红、也会让错误的代码通过。
    """
    src = PAGE.read_text(encoding='utf-8')
    src = re.sub(r'"""[\s\S]*?"""', '', src)
    out = []
    for ln in src.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)          # 引号不配平，`#` 可能在字符串里，别切
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_必须显式设置workload_profile():
    """上游刻意无默认，所以页面必须自己选一份 —— 否则永远抛 ProfileNotConfigured。"""
    code = _code_only()
    assert 'set_active_profile' in code, (
        '页面没有调用 set_active_profile。dr_profile.get_active_profile() '
        '刻意不提供默认 profile（错的 profile 会静默产出指向错误域名/SSM 键的计划），'
        '所以这一页必须做出显式选择，否则「生成 DR 计划」永远失败。')
    assert re.search(r'profiles["\']?\s*,\s*["\']petsite\.yaml|profiles/petsite\.yaml',
                     code), (
        '没找到指向 profiles/petsite.yaml 的 profile 路径。'
        '这个展示站展示的是 petsite 这套负载。')


def test_m02_默认参数不得是虚构的AZ名():
    """`apne1-az1` 这套名字在图谱里不存在，用它当默认必然得到空计划。

    只盯**参数赋值**，不盯说明文字 —— 页面上那段解释「为什么 AZ scope 算不出来」
    的告警里必然会引用这个名字，那是在讲问题，不是在用它。
    同理 `EXAMPLE_PLAN_PATH` 指向的
    `dr-plan-generator/examples/az-switchover-apne1-az1.{md,json}`
    是仓内既有的产物**文件名**，改名不属于本页职责。

    第一版直接 `re.findall(r'apne1-az\\d', code)`，把这两类都报了出来 ——
    又一次「断言匹配到自己的说明文字」。判据要对准语法位置，不是对准字符串。
    """
    code = _code_only()
    #: 只有这些位置是「拿它当参数」
    PARAM_PATTERNS = [
        r'default_source\s*=\s*["\'][^"\']*apne1-az',
        r'default_target\s*=\s*["\'][^"\']*apne1-az',
        r'["\']source["\']\s*:\s*["\'][^"\']*apne1-az',
        r'["\']target["\']\s*:\s*["\'][^"\']*apne1-az',
        r'value\s*=\s*["\'][^"\']*apne1-az',
    ]
    bad = []
    for i, ln in enumerate(code.split('\n'), 1):
        for pat in PARAM_PATTERNS:
            if re.search(pat, ln):
                bad.append(f'{i}: {ln.strip()[:80]}')
                break
    assert not bad, (
        '这些地方把虚构 AZ 名当参数用（图谱里是 ap-northeast-1a/c/d）：\n  '
        + '\n  '.join(bad)
        + '\n点「生成」会得到一份有计划 ID、有 RTO、但受影响服务为 0 的空计划 —— '
          '比报错更容易误导人。')


def test_m03_AZ选项必须从图谱取():
    code = _code_only()
    assert 'AvailabilityZone' in code, (
        'AZ 选项应从图谱查（MATCH (a:AvailabilityZone)），不要硬编码 —— '
        '硬编码就是虚构 AZ 名那个问题的来源。')


def test_m04_默认预设必须是实测能出计划的那个():
    """默认不能是「自定义」+ 虚构名，也不能是算不出内容的 AZ scope。"""
    code = _code_only()
    m = re.search(r'PRESET_SCENARIOS\s*=\s*\{([\s\S]{0,900}?)\n\}', code)
    assert m, '没找到 PRESET_SCENARIOS'
    first = m.group(1).split('},')[0]
    assert '"service"' in first or "'service'" in first, (
        '第一个（默认）预设的 scope 不是 service。\n'
        '实测只有 scope=service source=petsite 能算出受影响服务（6 个）；'
        'AZ scope 目前是 0。默认必须落在能出内容的那个场景上。')
    # 选择框里「自定义」不得排在第一位
    sel = re.search(r'"预设场景",\s*\n?\s*(.+)', code)
    assert sel, '没找到预设选择框'
    assert not re.match(r'\[\s*["\']自定义', sel.group(1).strip()), (
        '「自定义」不应是默认选项 —— 它的默认值曾是虚构 AZ 名。')


def test_m05_受影响服务为零时必须明确告知():
    """空计划有计划 ID、有 RTO/RPO、有阶段，唯独没有内容 —— 必须说出来。"""
    code = _code_only()
    assert re.search(r'affected_count', code), '没找到 affected_count'
    assert re.search(r'not result\.get\(["\']affected_count', code), (
        '没有针对「受影响服务为 0」的分支。\n'
        '这种计划看起来生成成功（有 ID、有 RTO、有阶段），内容却是空的，'
        '比报错更容易误导人 —— 必须显式告知并指出该换哪个预设。')


def test_m06_无法推导的RPO不得渲染成None():
    """生成器刻意返回 None，页面不能把这份诚实渲染成看起来像 bug 的 `None 分钟`。"""
    code = _code_only()
    assert not re.search(r"estimated_rpo['\"]?,\s*['\"]—['\"]\s*\)\}\s*分钟", code), (
        'RPO 仍在直接插值。生成器对无法论证的 RPO 刻意返回 None '
        '（"The plan will say so rather than print a number that cannot be '
        'justified"），页面把它渲染成 `None 分钟` 会把这份诚实抹成一个 bug。')
    assert '无法推导' in PAGE.read_text(encoding='utf-8'), (
        'RPO 为 None 时应显示「无法推导」并解释原因。')


@pytest.mark.skipif(not os.environ.get('NEPTUNE_ENDPOINT'),
                    reason='需要活图谱才能验证真的出得来计划')
def test_m07_默认场景在活图谱上真的出得来计划():
    """行为验证：跑一次真实生成，受影响服务必须 > 0。"""
    import logging
    import sys
    import warnings
    warnings.filterwarnings('ignore')
    logging.disable(logging.WARNING)
    if str(_DEMO) not in sys.path:
        sys.path.insert(0, str(_DEMO))
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=600)
    at.run()
    assert not at.exception, f'首屏异常：{at.exception[0].value[:200]}'
    assert at.button, '没有生成按钮'
    at.button[0].click().run(timeout=600)
    assert not at.exception, f'点击后异常：{at.exception[0].value[:200]}'
    errs = [e.value for e in at.error]
    assert not errs, f'生成失败：{errs[0][:250]}'
    affected = [m.value for m in at.metric if m.label == '受影响服务']
    assert affected, '没有「受影响服务」指标'
    assert int(affected[0]) > 0, (
        f'默认场景算出 {affected[0]} 个受影响服务 —— 默认必须能出真实内容。')

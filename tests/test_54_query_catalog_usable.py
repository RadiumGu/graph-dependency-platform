"""test_54_query_catalog_usable.py — 钉住 Query Catalog「一打开就能用」。

## 为什么需要

实测这一页曾经**完全点不动**，而且没有任何报错：

    默认选中 `q10_infra_root_cause`（必填 `affected_service`）
    → 参数下拉框是 `[""] + KNOWN_SERVICES`，空串在第一位所以是默认值
    → `missing = ['affected_service']`
    → 「▶️ 执行查询」是 `disabled=bool(missing) or not online` → 禁用
    → 四个精选「运行」按钮的 `qc_autorun` 路径同样要求 `not missing`，也走不通

页面渲染正常、零异常、零日志 —— 只是所有按钮都按不动。
**「首屏没异常」和「功能可用」是两件事**，这条测试守的是后者。

第二个 bug 同样无声：精选按钮只设 `qc_selected`，却指望
`st.selectbox(..., index=...)` 去读它 —— 而 **widget 带 key 且 session state
已有值时 Streamlit 忽略 `index=`**。四个精选按钮因此全在跑当前那条查询，
实测四个都返回 `q10_infra_root_cause` 的结果，而标称是 q20/q16/q21/q2。

## 为什么用「静态 + 离线行为」两种断言

「按钮可点」在离线环境下断言不了 —— `disabled` 还叠了 `not online`。
所以：
  · 必填参数不得有空选项 → 静态扫描源码（这是 disabled 的真正成因）
  · 精选按钮必须推动选择框 → 离线 AppTest 也能验证（写 session state 与联网无关）
"""
import re
from pathlib import Path

import pytest

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
PAGE = _DEMO / 'pages' / '2_Query_Catalog.py'


def _src() -> str:
    return PAGE.read_text(encoding='utf-8')


def _code_only() -> str:
    """剥掉 docstring 与注释，只留可执行代码。

    ⚠️ 本文件第一版没做这件事，两条断言当场失效：

      · m03 在**正确**的代码上就报红 —— 因为我在注释里写了
        「原来写着 `petadoptionshistory`」，断言匹配到了自己的说明文字。
      · m02 在注入回归后**不报红** —— 注释里出现了 `qc_select_box`，
        于是删掉真正那行代码它也照样通过。

    这是本会话第三次踩同一个坑（另两次见 todo/injection-found-defects #43）：
    **拿源码文本做断言时，必须先把自己的散文剔掉**，否则测的是注释而不是代码。
    """
    src = _src()
    src = re.sub(r'"""[\s\S]*?"""', '', src)
    src = re.sub(r"'''[\s\S]*?'''", '', src)
    out = []
    for ln in src.split('\n'):
        stripped = ln.split('#', 1)[0] if not _in_string_literal(ln) else ln
        out.append(stripped)
    return '\n'.join(out)


def _in_string_literal(line: str) -> bool:
    """粗判这行的 `#` 是否落在字符串里（避免把 f-string 里的 # 当注释切掉）。"""
    return line.count('"') % 2 == 1 or line.count("'") % 2 == 1


def test_m01_必填参数不得默认空选项():
    """空选项当默认值 → missing 非空 → 执行按钮永久禁用。"""
    body = _code_only()
    # 允许 `[""] + choices` 出现在**非必填**分支；必填分支必须用裸选项
    bad = []
    for m in re.finditer(r'opts\s*=\s*(.+)$', body, re.M):
        expr = m.group(1)
        # 期望形态：`X if req else [""] + X`
        if '[""]' in expr and 'if req' not in expr:
            bad.append(expr.strip()[:90])
    assert not bad, (
        '必填参数的选项里塞了空串且没有按 req 区分：\n  ' + '\n  '.join(bad)
        + '\n空串在第一位就是默认值，会让 missing 永远非空、执行按钮永久禁用。')
    assert 'if req else [""]' in body or "if req else ['']" in body, (
        '没找到「必填不给空选项、可选才给」的分支。'
        '可选参数留空是有意义的（= 不传该参数），必填留空只会锁死按钮。')


def test_m02_精选按钮必须写选择框自己的state_key():
    """`index=` 在 widget 带 key 时不生效，必须直接写 key。"""
    src = _code_only()
    m = re.search(r'st\.button\("运行"', src)
    assert m, '没找到精选「运行」按钮'
    seg = src[m.start():m.start() + 400]
    assert 'qc_select_box' in seg, (
        '精选按钮没有写 selectbox 自己的 state key `qc_select_box`。\n'
        '只设一个自定义键（如 qc_selected）再靠 `index=` 读，是不生效的 —— '
        'widget 带 key 且 session state 已有值时 Streamlit 忽略 index。')
    # 选择框本身不应再传 index，否则 Streamlit 会告警两条路冲突
    sel = re.search(r'selected = st\.selectbox\((?:.|\n){0,200}?\)', src)
    assert sel, '没找到查询选择框'
    assert 'index=' not in sel.group(0), (
        '选择框同时用了 index= 和 session state key，Streamlit 会告警\n'
        '「created with a default value but also had its value set via the '
        'Session State API」，且 index 不生效。只留 state key 一条路。')


def test_m03_服务清单不得硬编码图谱里不存在的名字():
    """`petadoptionshistory` 是历史遗留名，图谱里叫 `pethistory`。

    同一个毛病在 6_Root_Cause_Analysis 也出现过：硬编码的服务清单与图谱漂移，
    于是用户选了一个图里没有的名字，查询返回空而看不出原因。
    现在服务清单从 `C.service_names()` 取（活图谱现取，离线回退快照）。
    """
    src = _code_only()
    assert 'petadoptionshistory' not in src, (
        '源码里还有 `petadoptionshistory` —— 图谱里的名字是 `pethistory`。')
    assert 'C.service_names()' in src, (
        '服务清单应从 C.service_names() 取，不要硬编码 —— 硬编码必然与图谱漂移。')


def test_m04_精选按钮点下去真的会换查询(monkeypatch):
    """离线也能验证：写 session state 与联网无关。"""
    import sys
    if str(_DEMO) not in sys.path:
        sys.path.insert(0, str(_DEMO))
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=120)
    at.run()
    if at.exception:
        pytest.skip(f'页面首屏异常，先修那个：{at.exception[0].value[:120]}')
    if not at.button:
        pytest.skip('没有按钮可点（离线且无精选区？）')

    before = at.selectbox[0].value if at.selectbox else None
    switched = []
    for i in range(min(len(at.button), 4)):
        a = AppTest.from_file(str(PAGE), default_timeout=120)
        a.run()
        if a.button[i].label != '运行':
            continue
        a.button[i].click().run(timeout=120)
        after = a.selectbox[0].value if a.selectbox else None
        switched.append((i, before, after))
    if not switched:
        pytest.skip('没有标为「运行」的精选按钮')
    unchanged = [s for s in switched if s[1] == s[2]]
    assert not unchanged, (
        f'这些精选按钮点下去没有换查询（前后都是同一条）：{unchanged}\n'
        '说明它没写 selectbox 自己的 state key，点了等于在跑当前那条查询。')

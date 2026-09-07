"""test_56_smart_query_layout.py — 钉住 Smart Query 的主区只放对话。

## 用户原话

「对话框在最下面，被很多内容隔开，答案在最上面，看着很不方便，
而且对话框上灰色的，看着好像坏了」

## 根因不是 chat_input 的位置

`st.chat_input` 在顶层**总是固定在页面底部** —— 这是 Streamlit 的行为，
也是聊天界面的常规，不该改。真正的问题是**它和对话之间塞了东西**：

    标题 / 引擎状态
    对话历史                    ← 答案在这里
    并排对比开关
    [chat_input 固定在底部]
    新一轮问答渲染
    契约 few-shot 语料（一个过滤输入框 + N 个 expander）  ← 又长又占地
    空状态引导                  ← 第一次打开的人根本滚不到

few-shot 那一大块渲染在对话之后，把答案顶到很上面、输入框压到很下面，
中间隔几十行。所以不变量是：**主区自上而下只有「引擎状态 → 空状态引导 →
对话 → 输入框」，别的都在侧栏。**

「灰色像坏了」是 `disabled=not ONLINE`：离线时输入框被禁用却不解释。
现在离线会显式说明「输入框是禁用状态（不是坏了）」并指向侧栏的语料。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
PAGE = _DEMO / 'pages' / '4_Smart_Query.py'


def _split_main_and_sidebar() -> tuple:
    """把页面源码切成「侧栏块」与「主区」两部分。

    侧栏块 = `with st.sidebar:` 起，到下一个顶层（零缩进）语句止。
    """
    src = PAGE.read_text(encoding='utf-8')
    m = re.search(r'^with st\.sidebar:', src, re.M)
    assert m, '没找到 with st.sidebar:'
    nxt = re.search(r'^\S', src[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[m.start():end], src[:m.start()] + src[end:]


def _strip_comments(text: str) -> str:
    """剥注释与 docstring —— 否则断言会匹配到解释性文字。

    本会话为这件事付过四次学费（见 tests/test_54、test_55 的同名函数，
    以及 todo/injection-found-defects #43）。
    """
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_few_shot语料必须在侧栏不能在主区():
    """它是页面上最长的一块，放主区就会把答案和输入框隔开。"""
    side, main = _split_main_and_sidebar()
    side, main = _strip_comments(side), _strip_comments(main)
    assert 'few-shot' in side or 'FEW_SHOT' in side, (
        'few-shot 语料块不在侧栏。它是页面上最长的一块'
        '（一个过滤输入框 + N 组 Cypher），放在主区会把答案顶上去、'
        '把输入框压下去，中间隔几十行。')
    # 主区不得再有语料的过滤框 / 逐条渲染
    assert 'fs_filter' not in main, (
        '主区还有 few-shot 的过滤输入框（key=fs_filter），应该只在侧栏。')
    assert not re.search(r'for i, ex in enumerate\(shown', main), (
        '主区还在逐条渲染 few-shot 语料，应该只在侧栏。')


def test_m02_并排对比开关必须在侧栏():
    """它是配置不是内容 —— 放主区会挤在对话和输入框之间。"""
    side, main = _split_main_and_sidebar()
    side, main = _strip_comments(side), _strip_comments(main)
    assert 'compare_mode' in side and 'st.toggle' in side, (
        '并排对比开关不在侧栏。它是配置项，不是对话内容。')
    assert not re.search(r'^compare_mode\s*=\s*st\.toggle', main, re.M), (
        '主区还有顶层的 compare_mode toggle，应该只在侧栏。')


def test_m03_空状态引导必须在对话之前():
    """原来它在页面最下面（few-shot 之后），第一次打开的人根本滚不到。"""
    _, main = _split_main_and_sidebar()
    code = _strip_comments(main)
    hint = re.search(r'if not st\.session_state\["chat_history"\]', code)
    loop = re.search(r'^for msg in st\.session_state\["chat_history"\]', code, re.M)
    assert hint, '没找到空状态引导（if not chat_history 分支）'
    assert loop, '没找到对话历史渲染循环'
    assert hint.start() < loop.start(), (
        '空状态引导排在对话历史之后。第一次打开页面时对话是空的，'
        '引导必须在最上面能被看见，而不是在页面底部。')


def test_m04_离线时必须解释输入框为什么是灰的():
    """用户报的「对话框上灰色的，看着好像坏了」= disabled 却不解释。"""
    src = PAGE.read_text(encoding='utf-8')
    assert 'disabled=not ONLINE' in src, '输入框应在离线时禁用（这本身是对的）'
    assert '不是坏了' in src, (
        '离线时禁用了输入框却没有解释。用户会以为页面坏了 —— '
        '必须显式说明是离线导致的禁用，并指出离线时该看什么。')


def test_m05_主区不得出现大段非对话内容():
    """主区顶层的 subheader 会把对话切开 —— 一律挪侧栏或去掉。"""
    _, main = _split_main_and_sidebar()
    code = _strip_comments(main)
    bad = re.findall(r'^st\.subheader\((.{0,60})', code, re.M)
    assert not bad, (
        f'主区还有顶层 st.subheader：{bad}\n'
        '主区自上而下应只有「引擎状态 → 空状态引导 → 对话 → 输入框」，'
        '任何额外区块都会把答案和输入框隔开。')

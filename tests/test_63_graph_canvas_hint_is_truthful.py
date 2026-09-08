"""test_63_graph_canvas_hint_is_truthful.py — 画布提示不得说反话。

## 这条门禁挡的是什么

`3_Graph_Explorer` 原来的提示是：

    if layered and widest > 12:
        st.caption("画布已自动缩放到能装下全图，可用滚轮缩放…")

**那句话是错的。** 页面注入的 `fitReadable()` 先调 `network.fit()`，
但如果结果缩放低于 `MIN_SCALE = 0.75`，它会**放弃装下全图** ——
改为保持可读比例、对准锚点，剩下靠平移。理由是 `fit()` 为了装下全图会无限
缩小，节点标签变成糊点，那样图就白画了。这个取舍是对的，提示说了反话。

## 触发条件也不对

`widest > 12`（最宽一层的节点数）是个**代理量**。实测：一个 20 节点的 petsite
视图最宽层只有 10 个节点 —— 提示不出现 —— 而 x 跨度有 1615px，
主容器约 900px，`fit` 需要 0.56 倍、低于下限，于是钳到 0.75，
**约 26% 的内容在视口外**。需要拖动的时候提示反而不显示。

判据要用**真实 x 跨度**，不是节点计数。这与本会话反复出现的教训同一类：
判据必须对准现象本身，而不是一个恰好相关的代理量。

## 为什么这值得一条门禁

用户看到一张右侧被切掉的图，加上一句「已装下全图」，只会得出一个结论：
**这个图是坏的。** 而它其实是好的，只是需要拖一下。
一句说反话的提示比没有提示更糟。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

PAGE = Path(PROJECT_ROOT) / 'demo' / 'pages' / '3_Graph_Explorer.py'


def _code_only(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_不得无条件宣称已装下全图():
    """`fitReadable` 在缩放低于下限时明确放弃装下全图。"""
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    # 允许在「确实装下了」的分支里这么说；不允许它是唯一/无条件的说法
    hits = [ln.strip()[:90] for ln in code.split('\n')
            if '装下全图' in ln]
    assert hits, '页面完全没提缩放行为 —— 用户会以为图被截断了'
    assert any('跨度' in c for c in _code_only(
        PAGE.read_text(encoding='utf-8')).split('\n')), (
        '页面没有按真实 x 跨度判断是否超出视口。'
        '「已装下全图」只能出现在确实装下的分支里。')


def test_m02_判据必须用真实x跨度不是节点计数():
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    assert re.search(r'max\(\s*_xs\s*\)\s*-\s*min\(\s*_xs\s*\)', code), (
        '没有从 positions 算真实 x 跨度。原来用 `widest > 12`（最宽一层的节点数）'
        '做代理：实测最宽层 10 个节点时提示不出现，而 x 跨度 1615px、'
        '约 26% 内容在视口外 —— **需要拖动时提示反而不显示**。')
    assert '_MIN_SCALE' in code, (
        '没有引用 MIN_SCALE。提示里的百分比必须和 JS 里实际用的缩放下限一致，'
        '否则说的和做的不是一回事。')


def test_m03_提示必须说清为什么不装下全图():
    """光说「可以拖」不够 —— 要说清这是取舍，否则看着还是像坏了。"""
    src = PAGE.read_text(encoding='utf-8')
    assert '刻意的取舍' in src or '刻意' in src, (
        '提示没有说明「不装下全图」是刻意的。用户看到一张右侧被切掉的图，'
        '第一反应是「这个图是坏的」。')
    assert '糊点' in src or '不可读' in src, (
        '提示没有说明装下全图的代价（标签变成糊点）。'
        '不给代价，取舍就站不住。')


def test_m04_JS里的下限与Python提示必须同值():
    """两处写死同一个数必然漂移 —— 至少要能对上。"""
    src = PAGE.read_text(encoding='utf-8')
    js = re.search(r'MIN_SCALE\s*=\s*([0-9.]+)\s*;', src)
    py = re.search(r'_MIN_SCALE\s*=\s*([0-9.]+)', src)
    assert js and py, f'找不到两处 MIN_SCALE：js={bool(js)} py={bool(py)}'
    assert float(js.group(1)) == float(py.group(1)), (
        f'JS 里的 MIN_SCALE={js.group(1)}，Python 提示用的是 {py.group(1)} —— '
        '提示里算出的「视口外百分比」会是错的。')

"""test_63_graph_canvas_hint_is_truthful.py — 画布提示不得说反话。

## 这条门禁挡的是什么

`3_Graph_Explorer` 最早的提示是：

    if layered and widest > 12:
        st.caption("画布已自动缩放到能装下全图，可用滚轮缩放…")

**那句话是错的。** 当时页面注入的 `fitReadable()` 先调 `network.fit()`，但如果
结果缩放低于 `MIN_SCALE = 0.75`，它会**放弃装下全图** —— 改为保持可读比例、
对准锚点，剩下靠平移。取舍是对的，提示说了反话。

触发条件也不对：`widest > 12`（最宽一层的节点数）是个**代理量**。实测一个
20 节点的 petsite 视图最宽层只有 10 个节点（提示不出现），x 跨度却有 1615px、
约 26% 的内容在视口外 —— **需要拖动的时候提示反而不显示**。

## 2026-09-13：机制换了，这条门禁的判据跟着换，意图不变

渲染从 pyvis 换成自绘 SVG（`demo/components/graph_svg`）之后，**不再有缩放这回事**：
字号固定 13px，图比容器大就在框内滚动。所以 `MIN_SCALE` 连同 `fitReadable()`
一起没了，原来断言「必须引用 _MIN_SCALE」「JS 与 Python 同值」的两条判据，
测的是一个不存在的机制。

保留的是**意图**：一句说反话的提示比没有提示更糟。用户看到一张右侧被切掉的图、
加上一句「已装下全图」，只会得出一个结论 —— 这个图是坏的。而它其实是好的。

所以判据改成对准新行为：
  · 判据仍必须是**真实 x 跨度**，不能退回节点计数这种代理量；
  · 页面不得声称「自动缩放到装下全图」，因为它不缩放了；
  · 组件必须真的不缩放（字号是常量，不存在 fit-to-container）。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

PAGE = Path(PROJECT_ROOT) / 'demo' / 'pages' / '3_Graph_Explorer.py'
COMPONENT_JS = (Path(PROJECT_ROOT) / 'demo' / 'components' / 'graph_svg'
                / 'frontend' / 'graph.js')
COMPONENT_CSS = (Path(PROJECT_ROOT) / 'demo' / 'components' / 'graph_svg'
                 / 'frontend' / 'graph.css')


def _code_only(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_超出视口时必须告知且说清是刻意的():
    """光被切掉不解释，用户第一反应是「图坏了」。"""
    src = PAGE.read_text(encoding='utf-8')
    assert '在视口外' in src, (
        '页面没有告知内容超出视口 —— 用户会以为图被截断了。')
    assert '刻意' in src or '不缩小' in src or '保持' in src, (
        '没有说明「不缩到装下」是刻意的取舍。')
    assert '糊' in src or '不可读' in src or '白画' in src, (
        '没有说明装下全图的代价（标签看不清）。不给代价，取舍就站不住。')


def test_m02_判据必须用真实x跨度不是节点计数():
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    assert re.search(r'max\(\s*_xs\s*\)\s*-\s*min\(\s*_xs\s*\)', code), (
        '没有从 positions 算真实 x 跨度。原来用 `widest > 12`（最宽一层的节点数）'
        '做代理：实测最宽层 10 个节点时提示不出现，而 x 跨度 1615px、'
        '约 26% 内容在视口外 —— **需要拖动时提示反而不显示**。')


def test_m03_不得声称自动缩放到装下全图():
    """自绘 SVG 不做 fit-to-container，任何「已缩放到装下」的说法都是假的。"""
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    for bad in ('缩放到能装下', '已装下全图', '自动缩放到装下'):
        assert bad not in code, (
            f'页面仍声称「{bad}」，但渲染改成自绘 SVG 后不缩放 —— '
            '字号固定、超出靠滚动。这句话现在是假的。')


def test_m04_组件必须真的不缩放():
    """提示说「不缩小字号」，组件里就必须没有 fit-to-container 这回事。"""
    js = COMPONENT_JS.read_text(encoding='utf-8')
    css = COMPONENT_CSS.read_text(encoding='utf-8')
    assert 'overflow: auto' in css or 'overflow:auto' in css, (
        '容器没有 overflow:auto —— 不缩放又不能滚动，超出的部分就真的看不到了。')
    assert re.search(r'font:\s*13px', css), (
        '标签字号不是写死的 13px。一旦它变成可变的（比如随缩放算），'
        '「节点名恒定 13px」这句提示就成了假话，而这正是上一个渲染器'
        '（min-zoomed-font-size 硬编码 10）让标签整体消失的原因。')
    assert 'viewBox' in js, '没有 viewBox —— 无法按真实 bbox 定位内容。'

"""dagre 分层 + 自绘 SVG 的依赖图组件（Streamlit Components v2，免构建内联）。

## 为什么有这个组件

`9_Interactive_Explorer` 用的 st-link-analysis 把 Cytoscape 的
`min-zoomed-font-size` 硬编码为 10，而它既不暴露 font-size 也不透传 cy 实例。
fit 后的缩放落在 0.625 以下时**节点标签被整体隐藏**——13 个节点全成了无名圆点，
一张依赖图连节点叫什么都读不出来，且在封装内没有任何入口可以绕开（试过收紧布局
和关掉 fit，前者跨不过阈值、后者让视图不再对准内容）。

自绘之后这类阈值不存在：字号就是 `graph.css` 里 `.gs-label` 的一条声明。

## 为什么现在能免构建

Streamlit **1.51.0** 起提供 Components v2（`st.components.v2`）：frameless、
双向、**纯 Python 内联注册**——把 html/css/js 当字符串传进去，不需要 Node、npm
或 Vite。本仓固定 `streamlit==1.62.0`，在门槛之上。
（对照：`components.v1.html()` 是单向的，且已在 1.56.0 弃用。）

## 布局仍然用 dagre

dagre 自带 acyclic 预处理，**有环也能自动断环**；d3-hierarchy 要求单根/无环/单父，
共享依赖只能靠克隆节点表达；d3-dag 要调用方自己标 reversed 边且不处理自环。
换渲染方式不该顺手丢掉环处理，所以布局库不动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v2 as components

_FE = Path(__file__).parent / "frontend"
_CSS = (_FE / "graph.css").read_text(encoding="utf-8")

# dagre 以 UMD 发布，而 v2 的 `js=` 是一个 ES module：把 UMD 拼在模块前面，
# 它执行时把 `dagre` 挂到全局，后面的 export default 就能直接用。
# 走本地文件而不是 CDN：这一页在无出网的环境里也要能画图，
# 而且锁住版本比锁住某个 CDN 的 latest 可靠。
_JS = (_FE / "dagre.min.js").read_text(encoding="utf-8") + "\n" + (
    _FE / "graph.js"
).read_text(encoding="utf-8")

_component = components.component("kc_graph_svg", js=_JS, html='<div></div>')


def render(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    height: int = 620,
    anchor: str | None = None,
    rankdir: str = "LR",
    nodesep: int = 18,
    ranksep: int = 96,
    key: str = "kc_graph_svg",
) -> dict[str, Any]:
    """画依赖图，返回 {"selected": 节点 id 或 None, "expand": 双击的节点 id 或 None}。

    anchor 是起点节点 id：图比容器大时初始视图对准它，否则用户开局可能
    看到的是图的中段、找不到自己选的服务。

    nodes 每项：id（必需）、label、type、group、degree、accent（左侧色条）、
                fill、stroke、badge、badge_fill
    edges 每项：source、target（必需）、color、width、title

    `expand` 是**一次性**触发（双击展开），读到之后就不会重复出现；
    `selected` 是持久状态，会跨 rerun 保留。
    """
    prev = st.session_state.get(f"_gs_{key}", {})

    result = _component(
        data={
            "nodes": nodes,
            "edges": edges,
            "anchor": anchor,
            "rankdir": rankdir,
            "nodesep": nodesep,
            "ranksep": ranksep,
            "_css": _CSS,
            "_state": {"selected_node": prev.get("selected")},
        },
        # `default` 只声明**持久 state**。trigger（这里是 expand）不进 default：
        # 它是一次性事件，给它一个默认值会让它变成一个「一直有值」的字段，
        # 于是每次 rerun 都读到同一个节点、无限展开。照 streamlit-d3-network
        # 的做法：state 进 default，trigger 只靠 on_<name>_change 注册。
        default={"selected_node": None},
        height=height,
        key=key,
        on_selected_node_change=lambda: None,
        on_expand_change=lambda: None,
    )

    out = {
        "selected": getattr(result, "selected_node", None) if result else None,
        "expand": getattr(result, "expand", None) if result else None,
    }
    st.session_state[f"_gs_{key}"] = {"selected": out["selected"]}
    return out

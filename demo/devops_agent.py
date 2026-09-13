"""devops_agent.py —— `aws devops-agent` 的最小调用封装。

## 为什么单独一个模块

RCA 页要做两件需要真实调用的事：让访客亲手复现「agent 会不会自发查图谱」那个实验，
以及带着页面已收齐的事实去追问。两者共用同一条调用链，判定逻辑也共用，所以放一处。

## 调用链

`CreateChat` → `SendMessage`（返回 **EventStream**，流式，不是 dict）→
`ListPendingMessages`（按 executionId 取回完整消息）。

`SendMessage` 的 `context` 参数是「带上下文追问」的入口 —— 不必把事实塞进问题正文，
那样会混淆「我问了什么」和「我给了什么材料」。

## 关于花钱

每次调用都是真实的 Bedrock 推理，有费用也有几十秒延迟。所以这一层只提供函数，
是否触发、触发频率由页面控制（见 `_RATE_LIMIT_SECONDS`）。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

REGION = os.environ.get("REGION", "ap-northeast-1")

#: 两次真实调用之间的最小间隔（秒）。页面上任何人都能按那个按钮，
#: 没有这个闸门，一次围观就能把 agent space 打满、把账单拉高。
_RATE_LIMIT_SECONDS = 90


def client():
    """返回 devops-agent 客户端；boto3 太旧或凭据缺失时抛错由调用方处理。"""
    import boto3
    return boto3.client("devops-agent", region_name=REGION)


def ask(
    agent_space_id: str,
    question: str,
    *,
    context: str | None = None,
    asset_ids: list[str] | None = None,
    user_id: str = "graph-demo-visitor",
    timeout_s: int = 180,
) -> dict[str, Any]:
    """问一次，返回 {"execution_id":…, "answer":…, "error":…}。

    不抛异常：这是给页面用的，任何失败都要变成能显示的文字，
    而不是让整页崩掉。
    """
    out: dict[str, Any] = {"execution_id": None, "answer": "", "error": None}
    try:
        c = client()
        chat = c.create_chat(agentSpaceId=agent_space_id, userId=user_id,
                             userType="IAM")
        exec_id = chat.get("executionId") or chat.get("chatId")
        out["execution_id"] = exec_id

        kw: dict[str, Any] = {
            "agentSpaceId": agent_space_id,
            "userId": user_id,
            "content": question,
        }
        if exec_id:
            kw["executionId"] = exec_id
        if context:
            kw["context"] = context
        if asset_ids:
            kw["assetIds"] = asset_ids

        # SendMessage 返回 EventStream。把它读干是拿到回答的前提 ——
        # 不读就直接去 ListPendingMessages 会拿到空的。
        resp = c.send_message(**kw)
        chunks: list[str] = []
        stream = resp.get("body") or resp.get("stream")
        if stream is not None:
            deadline = time.time() + timeout_s
            for event in stream:
                if time.time() > deadline:
                    out["error"] = f"读流超过 {timeout_s}s，截断"
                    break
                chunks.append(_text_of(event))
        out["answer"] = "".join(chunks).strip()

        # 流里取不到正文时按文档退到 ListPendingMessages。
        if not out["answer"] and exec_id:
            pend = c.list_pending_messages(agentSpaceId=agent_space_id,
                                           executionId=exec_id)
            out["answer"] = "\n".join(
                _text_of(m) for m in (pend.get("messages") or [])).strip()
    except Exception as exc:                                     # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _text_of(obj: Any) -> str:
    """从事件/消息里尽量取出人可读的正文。

    形状随 API 版本变化，所以不写死路径：递归找字符串值。宁可多取一点噪音，
    也不要因为某一版换了键名就整个显示为空。
    """
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, dict):
        for k in ("text", "content", "delta", "message", "outputText"):
            if k in obj:
                return _text_of(obj[k])
        return "".join(_text_of(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "".join(_text_of(v) for v in obj)
    return ""


#: 判定「这次回答到底有没有查依赖图谱」。
#:
#: 判据取自 `demo/fixtures/agent_unaided_answer.json` 的 `detection_rule`，
#: 那是踩过两次坑之后定下来的：
#:   · 不能数 tool_use 块 —— 真实工具调用体现在 tool_summary，按 tool_use 数恒为 0；
#:   · 不能只看 exp- 前缀 —— 有的回答把实验写成「HTTP chaos · 2026-09-05」而不是
#:     原始 ID，只按前缀数会把它误判成没查图谱。
#:
#: 我自己又踩了同一类的第三个坑，用 20 条实录回答验证时暴露出来：
#:   · **百分比不能当图谱证据**。原来写 `\d+\.\d+\s*%` 想匹配「退化幅度 0.4%」，
#:     但回答里到处是普通百分比（「100% terminate」「50% 调用」），
#:     于是一条通篇只有 EXT 模板 ID 的回答被判成查了图谱。
#:     改为只认与退化/降级同现的百分比。
#:   · **exp- 必须大小写敏感**。执行 ID 形如 `EXPqjusfhv86R3t8F4`，
#:     不区分大小写时会被 `exp-` 规则误伤 —— 而执行 ID 属于控制面，不是图谱证据。
#:
#: 三条判据按可靠性排序，命中任一即算查了。
_GRAPH_TELLS = (
    (r"injection_confirmed", "提到 injection_confirmed（边上的属性名，控制面看不到）", 0),
    (r"\bexp-[a-z0-9-]{4,}", "引用了 exp- 前缀的真实实验 ID", 0),
    (r"(?:退化|降级|degradation|degraded)[^。\n]{0,24}?\d+(?:\.\d+)?\s*%"
     r"|\d+(?:\.\d+)?\s*%[^。\n]{0,12}?(?:退化|降级|degradation)",
     "引用了实验退化幅度", re.I),
)
_TEMPLATE_TELL = r"\bEXT[A-Za-z0-9]{6,}"


def judge_used_graph(answer: str) -> dict[str, Any]:
    """返回 {"used_graph": bool|None, "evidence": [...], "template_only": bool}。

    `used_graph=None` 表示证据不足以判定 —— 这比猜一个更有用，
    因为这一页的整个论点就建立在「判定要有依据」上。
    """
    if not answer:
        return {"used_graph": None, "evidence": [], "template_only": False}
    ev = [why for rx, why, fl in _GRAPH_TELLS if re.search(rx, answer, fl)]
    tmpl = bool(re.search(_TEMPLATE_TELL, answer))
    if ev:
        return {"used_graph": True, "evidence": ev, "template_only": False}
    if tmpl:
        return {"used_graph": False,
                "evidence": ["只出现 EXT 前缀的 FIS 模板 ID，没有任何图谱侧证据"],
                "template_only": True}
    return {"used_graph": None, "evidence": [], "template_only": False}


def rate_limited(state: dict, key: str = "last_call_ts") -> float:
    """还需等待的秒数；0 表示可以调用。state 用 st.session_state 传进来。"""
    last = state.get(key) or 0
    return max(0.0, _RATE_LIMIT_SECONDS - (time.time() - float(last)))

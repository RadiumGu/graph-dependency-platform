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

#: 两次**成功**调用之间的最小间隔（秒）。页面上任何人都能按那个按钮，
#: 没有这个闸门，一次围观就能把 agent space 打满、把账单拉高。
#:
#: 定成 60 是因为一次调用本身要 50 秒左右，期间界面是阻塞的（转圈），
#: 所以闸门真正防的不是连点、而是反复发起 —— 比一次调用略长就够了。
#: 原来定 90 秒偏长：调一次要等一分半才能再调，正常试用被当成滥用对待。
#:
#: **失败的调用不计入**（时间戳由调用方在拿到回答后才写）。失败没有产生推理
#: 成本，罚它没有意义 —— 而且 API 参数报错这类失败恰恰需要马上改一改再试。
_RATE_LIMIT_SECONDS = 60


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
        #
        # 2026-09-13：这里原来写 `resp.get("body") or resp.get("stream")`，
        # 那是照别的流式 AWS API 的惯例猜的键名。实测这个 API 把流放在 `events`
        # 下，于是流从没被读过、answer 恒空 —— 而 ask() 刻意不抛异常，
        # 这个 bug 就静默通过了（1.4s 返回空回答，判定给出 used_graph=None，
        # 页面把「调用失败」显示成了「判不出来」）。
        # 改为**按类型**找：EventStream 有 __iter__ 而不是 dict/str，
        # 键名再变一次也不用改这里。
        resp = c.send_message(**kw)
        stream = None
        for k, v in resp.items():
            if k == "ResponseMetadata" or isinstance(v, (str, bytes, dict, list)):
                continue
            if hasattr(v, "__iter__"):
                stream = v
                break

        if stream is None:
            out["error"] = ("SendMessage 的响应里找不到 EventStream，"
                            f"只有 {sorted(k for k in resp if k != 'ResponseMetadata')}")
        else:
            out["answer"], err = _read_stream(stream, timeout_s)
            if err:
                out["error"] = err

        # 流里取不到正文时按文档退到 ListPendingMessages。
        if not out["answer"] and exec_id:
            pend = c.list_pending_messages(agentSpaceId=agent_space_id,
                                           executionId=exec_id)
            out["answer"] = "\n".join(
                _text_of(m) for m in (pend.get("messages") or [])).strip()
    except Exception as exc:                                     # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _read_stream(stream: Any, timeout_s: int) -> tuple[str, str | None]:
    """读干 EventStream，返回 (回答正文, 错误)。

    ## 事件契约（2026-09-13 实测）

    ```
    responseCreated     {responseId, sequenceNumber}
    contentBlockStart   {index, type, id, sequenceNumber}      type: text|chat_title|context_usage
    contentBlockDelta   {index, delta:{textDelta:{text}}, …}
    contentBlockStop    {index, type, text, last, …}
    responseCompleted   {responseId, usage:{inputTokens,…}, …}
    responseFailed      {responseId, errorCode, errorMessage, …}
    ```

    ## 为什么必须按 index 追踪块类型

    第一版把每个事件递归找出的字符串全拼进正文，结果 **26.6% 是噪音**：响应 ID、
    UUID、事件类型名，以及整段 `context_usage` 遥测 JSON（context_window、
    utilization）。原因是 `chat_title` 和 `context_usage` 这两类块**也走
    textDelta** —— 它们和正文在事件层面长得一模一样，只能靠所属块的 type 区分。

    所以这里只取 `type == "text"` 的块。白名单而不是黑名单：将来多一种遥测块，
    黑名单会把它漏进正文里，而白名单只会漏掉正文的新形式 —— 后者看得见（回答变空），
    前者看不见（用户读到一堆 JSON 却以为是 agent 的话）。

    ## 为什么错误要单独返回

    `responseFailed` 不返回出来的话，调用失败会表现成「回答为空」，判定器给出
    used_graph=None，页面显示「这次判不出来」—— 把 API 拒绝伪装成判定不确定。
    实际踩到过：assetIds 传了 SKILL 型资产，API 报
    `Asset '…' is type 'SKILL', expected 'ATTACHMENT'`。
    """
    kinds: dict[int, str] = {}       # block index -> type
    parts: list[str] = []
    errs: list[str] = []
    deadline = time.time() + timeout_s

    for event in stream:
        if time.time() > deadline:
            errs.append(f"读流超过 {timeout_s}s，截断")
            break
        if not isinstance(event, dict):
            continue

        start = event.get("contentBlockStart")
        if isinstance(start, dict):
            kinds[start.get("index", 0)] = str(start.get("type") or "")

        delta = event.get("contentBlockDelta")
        if isinstance(delta, dict):
            if kinds.get(delta.get("index", 0), "text") == "text":
                td = (delta.get("delta") or {}).get("textDelta") or {}
                if isinstance(td.get("text"), str):
                    parts.append(td["text"])

        failed = event.get("responseFailed")
        if isinstance(failed, dict):
            errs.append("%s: %s" % (failed.get("errorCode") or "ERROR",
                                    failed.get("errorMessage") or "(无消息)"))

    return "".join(parts).strip(), ("；".join(errs) if errs else None)


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

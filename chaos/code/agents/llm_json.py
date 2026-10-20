"""从 LLM 输出里提取 JSON 数组 —— 单一实现。

## 为什么有这个模块（2026-10-11）

`learning_strands.py` 与 `hypothesis_strands.py` 各有一份**逐字相同**的
`_extract_json_array` + `_JSON_BLOCK_RE`。两份一样脆弱，而脆弱点实测可触发。

本仓对这类重复已有定论 —— `tests/test_97_cfn_loader_single_source.py` 守着
CFN loader 的单一来源，它的报错说得很准：

    6 份重复里曾有 3 份是错的，重复本身就是那些错误能存在的原因。

## 修掉的脆弱点（2026-10-11 构造输入实测）

原退化路径是：

    lo = text.find("[")      # 第一个 [
    hi = text.rfind("]")     # 最后一个 ]
    json.loads(text[lo:hi+1])

当 ```json fence 丢失、而文本里又有别的 `[` 时，这个区间就是垃圾：

    输入   覆盖域 [network] 与 [data] 缺失。
           [{"priority":1,"category":"coverage"}]
    截取   [network] 与 [data] 缺失。\n[{...}]     ← json.loads 失败
    返回   []                                      ← 当成「LLM 没给建议」

这不是假想输入：learning agent 实跑产出的建议描述里充满
`[compute]` / `[network]` / `[data]` 这类方括号引用。

实测六种形态，只有第 ⑤ 种（fence 丢失 + 描述含 `[`）会返回 0 条，
其余五种原实现都能救回来 —— 所以它是个**低频但真实**的失败模式，
而低频正是它能长期存在的原因。

## 为什么从后往前扫

prompt 明确要求「最后以 ```json ... ``` 包裹 JSON 数组作为最终答案」，
所以目标数组在文本末尾。从后往前找平衡的 `[...]`，天然避开前文里的
方括号，也不需要去理解 LLM 的散文部分。

## 为什么诊断信息要带出来

原实现在解析失败时**丢弃原始文本**，调用方只拿到一个空 list。
于是 golden 失败时的信息是 `got 0 recs, expected >= 1` —— 而这同一句
覆盖了三种完全不同的原因：

    ① LLM 调用失败（节流 / 超时）        → 返回值里 error 非空
    ② LLM 输出了内容但解析不出来         → error 为空、原始文本被丢弃 ← 盲区
    ③ LLM 真的认为没有建议               → error 为空、原始文本是空数组

2026-10-11 的 l004 失败就卡在 ②：整套跑两次都失败、单跑和按序跑前四个
都通过，而因为原始文本没留下，无法确定它到底输出了什么。
一次需要 6.7 分钟复现、复现了还查不出真因的失败，等于没有判据。
"""

from __future__ import annotations

import json
import re

#: ```json ... ``` 代码块。
#:
#: `\s*` 不带 `\n` 要求 —— LLM 偶尔输出 ```json[{...}]``` 这种紧凑形式，
#: 原实现的 `\s*\n` 匹配不到它（实测形态 ②）。
#: `(?i:json)` 容忍 ```JSON（实测形态 ③）。
_JSON_BLOCK_RE = re.compile(r"```(?:(?i:json))?\s*(.*?)```", re.DOTALL)

#: 退化扫描最多尝试几个 `]` 候选。
#:
#: 有上限是为了避免病态输入把这个函数变成 O(n²) 的热点 ——
#: 正常输出里目标数组就在最后一两个 `]` 上。
_MAX_CLOSING_CANDIDATES = 8


def _scan_last_json_array(text: str) -> list | None:
    """从后往前找最后一个能解析成 list 的平衡 `[...]` 区间。

    不去跳过 JSON 字符串内部的方括号 —— 那需要一个真正的扫描器，
    而这里有更便宜的判据：`json.loads` 自己会拒绝不平衡的结果，
    拒绝了就换下一个候选。
    """
    tried = 0
    hi = len(text)
    while tried < _MAX_CLOSING_CANDIDATES:
        hi = text.rfind("]", 0, hi)
        if hi < 0:
            return None
        tried += 1
        depth = 0
        for lo in range(hi, -1, -1):
            ch = text[lo]
            if ch == "]":
                depth += 1
            elif ch == "[":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[lo: hi + 1])
                    except json.JSONDecodeError:
                        break          # 这个候选不行，退回去找前一个 `]`
                    if isinstance(data, list):
                        return data
                    break
    return None


def extract_json_array(text: str) -> tuple[list, str | None]:
    """提取 JSON 数组。

    返回 ``(数组, 诊断)``：

    * 成功 → ``(data, None)``
    * 失败 → ``([], 一句说明 + 截断后的原始文本)``

    **诊断字段是这个函数存在的一半理由。** 调用方必须把它带进返回值，
    否则「LLM 输出了东西但解析不出来」会和「LLM 说没有建议」长得一样 ——
    那正是 2026-10-11 l004 失败查不出真因的原因。
    """
    if not text or not text.strip():
        return [], "LLM 返回空文本（既非调用失败也非空数组 —— 响应本身是空的）"

    # ① 优先 fence 内的内容。逐个试，返回第一个能解析成 list 的。
    blocks = 0
    for m in _JSON_BLOCK_RE.finditer(text):
        blocks += 1
        candidate = m.group(1).strip()
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data, None

    # ② 从后往前扫平衡的 [...]。
    data = _scan_last_json_array(text)
    if data is not None:
        return data, None

    head = text[:400].replace("\n", "\\n")
    tail = text[-400:].replace("\n", "\\n") if len(text) > 800 else ""
    diag = (
        f"无法从 LLM 输出里解析出 JSON 数组"
        f"（全文 {len(text)} 字符，fence 块 {blocks} 个）。"
        f"\n  开头: {head}"
    )
    if tail:
        diag += f"\n  结尾: {tail}"
    return [], diag

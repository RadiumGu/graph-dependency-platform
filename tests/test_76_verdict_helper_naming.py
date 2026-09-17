"""tests/test_76_verdict_helper_naming.py — 判定辅助函数不得以 `_verdict` 为前缀。

## 这条门禁在防什么

`tests/test_73_iam_deny_probe.py` 用 `_body_of(src, "def _verdict")` 取判定函数
的函数体，再对它断言（要求同时用业务证据、要求 MIN_BASELINE_REQUESTS 等）。

那是**前缀匹配**。2026-09-17 我新增了一个 `_verdict_semantic_only()`，
把它定义在 `_verdict()` **之前**，于是 `_body_of` 取到的是我的新函数 ——
t73_04 与 t73_07 双双变红，报的却是「confirmed 的条件里没有同时要求业务侧证据」
和「完全切断分支没有要求基线达到请求数下限」。

**门禁报的位置与真实问题差了一个函数**，排查时很容易以为是判定逻辑被改坏了。
（我当时正是先怀疑自己改坏了 `_verdict`。）

已把该函数改名为 `_semantic_channel_verdict`。本门禁防的是下一次：
只要有人再写 `_verdict_xxx`，t73 的断言就会静默地打到错的函数上 ——
**静默是这里最坏的性质**，因为断言仍然在跑、仍然给出红/绿，只是对象错了。

## 为什么不改 t73 的取法而改命名约定

改 `_body_of` 让它精确匹配 `def _verdict(` 也能解决，但那是别人的门禁文件，
而且精确匹配会在函数签名换行时再次失效（`def _verdict(\n    base: ...`）。
约束命名更稳：一个前缀只对应一个函数，是任何按名字取源码片段的工具都需要的前提。
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = _ROOT / "scripts" / "verify_via_iam_deny.py"

#: 被 t73 用作源码切片锚点的函数名。它们必须是**唯一**以该串开头的定义。
_ANCHORS = ("_verdict", "_persist_verdict", "_biz_degraded")


def test_t76_01_no_function_shadows_a_body_of_anchor():
    src = _PROBE.read_text(encoding="utf-8")
    defs = re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)\(", src, re.M)

    problems = []
    for anchor in _ANCHORS:
        # 以锚点为前缀、但不等于锚点本身的定义 —— 就是会抢锚的那些。
        shadows = [d for d in defs if d.startswith(anchor) and d != anchor]
        # `_persist_verdict` 本身以 `_verdict` 结尾而非开头，不构成遮挡；
        # 这里只看前缀，所以不会误报。
        if shadows:
            first = min(defs.index(s) for s in shadows)
            anchor_at = defs.index(anchor) if anchor in defs else len(defs)
            if first < anchor_at:
                problems.append(
                    f"`{anchor}` 被 {shadows} 遮挡（它们定义在前面）—— "
                    f"tests/test_73 的 _body_of(src, 'def {anchor}') 会取到错的函数体")
            else:
                problems.append(
                    f"`{anchor}` 存在同前缀函数 {shadows} —— 目前顺序上没遮挡，"
                    f"但改动顺序就会遮挡，属于隐藏地雷")

    assert not problems, (
        "判定辅助函数命名会让按名字切源码的门禁打到错的函数上：\n  "
        + "\n  ".join(problems)
        + "\n\n最坏的性质是**静默**：断言仍在跑、仍给红绿，只是对象错了。"
          "\n改法：给新函数换一个不以锚点为前缀的名字"
          "（例如 `_semantic_channel_verdict` 而不是 `_verdict_semantic_only`）。")


def test_t76_02_anchors_still_exist():
    """锚点函数本身必须还在 —— 否则 t73 的断言会因为找不到而失效。"""
    src = _PROBE.read_text(encoding="utf-8")
    missing = [a for a in _ANCHORS if f"def {a}(" not in src]
    assert not missing, (
        f"这些被 t73 当锚点用的函数不见了: {missing}。"
        " t73 的 _body_of 找不到锚点时取到的是空串或整个文件 —— "
        "断言会以错误的理由变绿或变红。")

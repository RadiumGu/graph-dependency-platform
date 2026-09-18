"""tests/test_78_staleness_classes_have_meanings.py

## 这条门禁在防什么

站点的「边还在不在」区块把动态边分成五种状态，每种配一段说明。
状态来自一条 openCypher 的 `CASE` 分支，说明来自页面里的 `_meaning` 字典。

**两者分开维护，就会分叉。** 加一个 CASE 分支而忘了补说明，站点会把原始的
英文键（`observed_then_silent`）直接显示给用户 —— 不报错、不变红，
只是那一行突然没有了含义。而这一节的全部价值就在于那些含义：

    deactivated            强信号消失 → **允许**断言依赖已不存在
    observed_then_silent   弱信号沉默 → **绝不**下这个结论
    declared_not_observed  声明了但没观测到 → 审计发现，不是边失效
    no_timestamp           落在失效判定管辖之外 → 过期机制看不见它们
    observing              在观测中

少了任何一段说明，读者就可能把后三类当成第一类 ——
**从图谱里读出一个它没有断言的东西**，正是本平台要防的那件事。

## 为什么不用 AppTest 而做静态断言

AppTest 对这一节无效：整个区块在 `if C.neptune_online() and _labels:` 里，
测试环境下 Neptune 离线 → 区块整块跳过 → **AppTest 全绿但一行都没渲染**。
本仓库已记过这条教训（「AppTest 全绿 ≠ 渲染正确」），所以这里比对源码本身。
"""
from __future__ import annotations

import pathlib
import re

_PAGE = (pathlib.Path(__file__).resolve().parents[1]
         / "demo" / "pages" / "1_Edge_Verification.py")


def _section() -> str:
    """取出「边还在不在」这一节的源码。"""
    src = _PAGE.read_text(encoding="utf-8")
    start = src.find("_stale_q = (")
    assert start > 0, "找不到陈旧边分类区块（_stale_q）—— 区块被删或改名了？"
    end = src.find("# ── 决策承重边", start)
    assert end > start, "找不到区块结尾锚点"
    return src[start:end]


def test_t78_01_every_case_branch_has_a_meaning():
    sec = _section()

    # 查询里的 CASE 分支：THEN 'xxx'
    branches = set(re.findall(r"THEN '([a-z_]+)'", sec))
    # ELSE 分支
    branches |= set(re.findall(r"ELSE '([a-z_]+)' END", sec))
    assert branches, f"抽不到 CASE 分支，正则该更新了。区块前 200 字:\n{sec[:200]}"

    # _meaning 字典的键
    mstart = sec.find("_meaning = {")
    assert mstart > 0, "找不到 _meaning 字典"
    mend = sec.find("_rows = []", mstart)
    meanings = set(re.findall(r'"([a-z_]+)": \(', sec[mstart:mend]))

    missing = branches - meanings
    assert not missing, (
        f"这些状态没有中文说明，站点会直接显示英文键: {sorted(missing)}\n"
        "这一节的全部价值就在那些含义 —— 少一段，读者就可能把"
        "「弱信号沉默（不可判定）」当成「已判定不存在」。")

    extra = meanings - branches
    assert not extra, (
        f"_meaning 里有查询产不出的键: {sorted(extra)} —— "
        "要么查询少了分支，要么说明是过时残留。")


def test_t78_02_the_three_staleness_kinds_stay_distinguished():
    """三类「陈旧」必须在文案里保持区分 —— 这是本节的论点，不是措辞。"""
    sec = _section()
    code_and_text = sec  # 这里刻意连注释一起看：说明文案本身就是被保护的对象

    # 允许断言消失的那一类
    assert "active=false" in code_and_text, "没有说明 deactivated 对应 active=false"
    # 明确不允许的那一类
    assert ("绝不置 active=false" in code_and_text
            or "绝不碰 `active`" in code_and_text), (
        "没有写明稀疏源「绝不置 active=false」—— 那是与「已判定不存在」的分界线，"
        "也是本项目最核心不变量（零流量与健康无法区分）在这一节的体现。")
    # 管辖之外的那一类
    assert "管辖之外" in code_and_text, (
        "没有说明「落在失效判定管辖之外」这一类。"
        "它的 stale 数恒为 0，而那个 0 的含义是「看不见」不是「都好」。")


def test_t78_03_query_uses_the_pages_own_conventions():
    """查询必须走本页既有约定：标签内联 + gquery(cypher) 单参 + 读 results。

    2026-09-18 我在这一节先写成了 `C.run_query(q, params)` —— 两个错：
    `run_query` 不存在（真名 `gquery`），且 `gquery` 的签名只接一个参数。
    静态锁一下，省得下一次再猜。
    """
    sec = _section()
    assert "C.run_query" not in sec, "`run_query` 不存在，真名是 `gquery`"
    assert re.search(r"C\.gquery\(_stale_q\)", sec), (
        "gquery 只接一个位置参数（cypher）—— 不能传 params 字典。"
        "标签应按本页既有写法内联进查询串。")
    assert '.get("results")' in sec, (
        "gquery 返回 {'results': [...]} 或 {'error': ...}，不是列表 —— "
        "直接当列表用会得到空结果且不报错。")

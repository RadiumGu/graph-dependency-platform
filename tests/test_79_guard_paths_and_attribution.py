"""tests/test_79_guard_paths_and_attribution.py

## 这条门禁在防什么

两件在 2026-09-23 一起发生的事：

### ① 路径常量在目录改名后过时，而门禁抄了同一个错值

`b3431f4` 把顶层 `./mcp` 改名成 `graph_mcp`（`./mcp` 遮蔽 PyPI 的 mcp 包，
让 `strands.tools.mcp` 报一个指不到真因的 ModuleNotFoundError）。
改名提交补了 `tests/test_42` 的「`./mcp` 不许回来」门禁，
**但漏改两处引用**：

    scripts/emit_graph_coverage_metrics.py:62   SKILL_FILE = _ROOT / 'mcp' / ...
    tests/test_65_coverage_metrics_script.py:44 SKILL      = PROJECT_ROOT / 'mcp' / ...

两处是各自独立的字面量，**朝同一个方向错**。所以 test_65 变红时报的是
「缺少 skill 源文件」—— 听起来像文件被删了，而真相是常量过时。
已改成从被测脚本用 ast 解析出 SKILL_FILE，不再有第二份字面量可漂移。

本门禁做的是更一般的一件事：**扫出所有指向不存在路径的 `_ROOT / ...` 常量。**
一个写死路径的常量在改名后不会报错，只会在某个凌晨的 cron 里变成一条假告警。

### ② 一个原因发出三条告警，其中一条把「没测」报成「测到了坏结果」

那次 cron 通知是：

    ⚠️ 依赖图守卫跳闸：
      - skill 资产不在了
      - skill 状态不是 ACTIVE（None）
      - skill 内容与仓库不一致（本地 e3b0c44298fc / 远端 None）

**三条全是假的** —— 远端资产完好（present=1 / active=1 / sha 一致，
asset_id `ki-4b88354a-…`）。`skill_guard` 在读不到本地文件时把三个远端标志
一起置 0 就返回了。

最有害的是第三条：「远端 None」让人以为查过远端而它返回空，
而那次运行里 **boto3 一次都没被调用**。
`e3b0c44298fc` 本身就是线索 —— 那是**空字符串**的 sha256 前缀，
说明哈希算的是读不到时的 `''`，不是「内容变了」。

一个把「没测」报成「测到了坏结果」的告警比没有告警更糟：
它把排查引向 AWS 控制台，而问题在一行路径常量上。
这与本项目的核心不变量是同一条 ——
**零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted。**
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_EMIT = _ROOT / "scripts" / "emit_graph_coverage_metrics.py"
_CRON = pathlib.Path("/home/ec2-user/.kiro/crew/crons/graph_coverage.py")


def _root_relative_path_constants(src: str) -> dict:
    """抽出形如 `NAME = _ROOT / 'a' / 'b.md'` 的常量，返回 {名字: 相对片段}。"""
    out = {}
    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = getattr(node.targets[0], "id", None)
        if not name or not name.isupper():
            continue
        parts, cur = [], node.value
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            if not isinstance(cur.right, ast.Constant):
                parts = []
                break
            parts.append(cur.right.value)
            cur = cur.left
        if parts and getattr(cur, "id", None) == "_ROOT":
            out[name] = tuple(reversed(parts))
    return out


def test_t79_01_every_hardcoded_repo_path_constant_resolves():
    """指标脚本里每个 `_ROOT / ...` 常量都必须指向真实存在的路径。

    这是最直接的一条：路径常量在目录改名后不报错，只在凌晨的 cron 里
    变成假告警。改名时漏改一处，这里立刻变红。
    """
    consts = _root_relative_path_constants(_EMIT.read_text(encoding="utf-8"))
    assert consts, "抽不到任何 _ROOT 相对路径常量 —— 解析该更新了"

    missing = {}
    for name, parts in consts.items():
        p = _ROOT.joinpath(*parts)
        if not p.exists():
            missing[name] = str(p)
    assert not missing, (
        "这些路径常量指向不存在的位置（很可能是某次目录改名后漏改）:\n  "
        + "\n  ".join(f"{k} → {v}" for k, v in missing.items())
        + "\n\n本项目的 MCP 代码在 `graph_mcp/`（`./mcp` 曾遮蔽 PyPI 的 mcp 包，"
          "见 b3431f4 与 tests/test_42 的改名门禁）。")


def test_t79_02_local_miss_does_not_masquerade_as_a_remote_verdict():
    """本地读不到时，三个远端标志必须留 None 而不是 0。

    0 断言「查过远端、结果是坏的」；None 表示「没查」。
    这条路径上 boto3 一次都没被调用，所以只能是后者。
    """
    spec = importlib.util.spec_from_file_location("_em79", _EMIT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    real = m.SKILL_FILE
    try:
        # 指到一个不存在的路径，模拟「常量过时」那次的情形。
        m.SKILL_FILE = _ROOT / "__definitely_not_here__" / "x.md"
        g = m.skill_guard()
    finally:
        m.SKILL_FILE = real

    assert g.get("local_readable") == 0, f"没有报出本地读不到: {g}"
    for k in ("skill_present", "skill_active", "skill_matches_repo"):
        assert g.get(k) is None, (
            f"{k} 被置成了 {g.get(k)!r} —— 本地读不到时远端并未被检查，"
            "置 0 等于断言「查过了、是坏的」。那次三条假告警就是这么来的。")
    note = g.get("note") or ""
    assert "未被检查" in note, (
        f"note 没有说明远端未被检查: {note!r}。"
        "缺了这句，读告警的人会去翻 AWS 控制台，而问题在一行路径常量上。")


def test_t79_03_cron_reports_one_problem_per_cause():
    """cron 的告警组装必须让「本地读不到」与三个远端判据互斥。"""
    if not _CRON.exists():
        import pytest
        pytest.skip(f"cron 脚本不在本机: {_CRON}")

    src = _CRON.read_text(encoding="utf-8")
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())

    assert "local_readable" in code, (
        "cron 没有区分「本地读不到」—— 那会让一个原因发出三条告警。")
    # 三个远端判据必须在 else 分支里（与 local_readable 互斥）。
    m = re.search(r"if guard\.get\('local_readable'\) == 0:(.*?)\n    if cov",
                  code, re.S)
    assert m, "找不到 local_readable 的判定块 —— 结构变了，本门禁该更新"
    block = m.group(1)
    assert "else:" in block, (
        "三个远端判据没有放在 else 分支里 —— 本地读不到时它们会一起触发。")
    for flag in ("skill_present", "skill_active", "skill_matches_repo"):
        assert flag in block, f"{flag} 不在互斥块内: {block[:200]}"


def test_t79_04_empty_sha_is_recognisable_in_the_note():
    """空内容的 sha 前缀必须能被认出来，不该被当成「内容变了」。

    `e3b0c44298fc` 是空字符串 sha256 的前缀。它出现在告警里时，
    含义是「没读到内容」而不是「内容不一致」—— 这一点必须写在代码里，
    否则下一个人还要再查一遍这个哈希是什么。
    """
    import hashlib
    empty = hashlib.sha256(b"").hexdigest()[:16]
    assert empty.startswith("e3b0c44298fc"), (
        f"空串 sha256 前缀变了？实测 {empty} —— 那本门禁的措辞要更新")

    src = _EMIT.read_text(encoding="utf-8")
    assert "e3b0c44298fc" in src, (
        "代码里没有记下这个哈希的含义。它是空字符串的 sha256 前缀，"
        "在告警里出现时说明「读不到内容」而非「内容变了」—— "
        "写下来省得下一个人再查一遍。")

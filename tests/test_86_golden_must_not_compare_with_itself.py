"""test_86_golden_must_not_compare_with_itself.py — golden 不得把同一引擎跑两遍。

## 背景

2026-09-20 七个模块的 direct 实现全部删除，factory 也去掉了回退分支：
`make_*_engine()` 现在只可能返回 strands 实现。

但两套 golden 的 `_engine_names()` 默认仍返回 `["direct", "strands"]`，
于是**同一个 strands 引擎被跑了两遍**，两次结果分别写进
`BASELINE-strands.md` 与 `BASELINE-direct.md`。

2026-09-21 首次用 cron 真实触发全量 golden 时实测到两个后果：

  1. **耗时翻倍** —— 这是全量跑到超时（>35 分钟）的主要原因之一。
  2. **假基线** —— `BASELINE-direct.md` 里是 strands 的第二次运行，
     两份差异只是运行间波动（p50 6210 vs 6929 ms、token 513487 vs 514544），
     会被读成「两个引擎的性能差异」。

f5900d3 删过两个同形状的「跟自己对比」空壳 shadow 测试，当时漏了 golden。
本文件补上门禁，让它不能再悄悄长回来。

## 为什么用静态扫描

真跑一次全量 golden 要 $3-5 与半小时，且需要 AWS 凭据与 VPC 内 Neptune ——
不可能每次提交都跑。而「参数化里又出现 direct」是纯文本事实，
静态扫描就能抓，还能进离线 CI。
"""
from __future__ import annotations

import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GOLDEN_TESTS = sorted(_ROOT.glob("tests/test_*golden*.py"))


def test_t86_01_扫描范围不得为空():
    """守门禁自己：扫到 0 个文件也会「未发现问题」。"""
    assert len(_GOLDEN_TESTS) >= 7, (
        f"只找到 {len(_GOLDEN_TESTS)} 个 golden 测试文件，预期至少 7 个。"
        f"文件被改名或移动了，本门禁的 glob 失效 —— 修 glob，别删这条。")


@pytest.mark.parametrize("path", _GOLDEN_TESTS, ids=lambda p: p.name)
def test_t86_02_不得把direct列入引擎matrix(path: pathlib.Path):
    """golden 的引擎清单里不得再出现 direct。

    direct 已不存在，列它只会让同一个引擎跑两遍并产出一份标着 direct 的
    strands 数据。
    """
    src = path.read_text(encoding="utf-8")
    # 只看代码行，跳过注释与 docstring 里的历史说明
    code_lines = []
    in_doc = False
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)

    bad = re.findall(r"""\[\s*["']direct["']\s*,\s*["']strands["']\s*\]""", code)
    assert not bad, (
        f"{path.name}: 引擎 matrix 里仍列着 direct（{bad}）。\n"
        f"direct 实现已于 2026-09-20 删除、factory 去掉了回退，"
        f"所以这会把同一个 strands 引擎跑两遍，并写出一份标着 direct 的"
        f"strands 基线。默认应只返回 [\"strands\"]。")


def test_t86_03_不得留下direct基线文件():
    """`BASELINE-direct.md` 不该存在 —— 它的内容必然是误导。"""
    stale = sorted(_ROOT.glob("tests/golden/**/BASELINE-direct.md"))
    assert not stale, (
        "这些 direct 基线文件应当删除：\n  "
        + "\n  ".join(str(p.relative_to(_ROOT)) for p in stale)
        + "\ndirect 实现已删除，文件里的数字实际来自 strands 的第二次运行。"
          "留着会被当成两个引擎的对比数据读。")

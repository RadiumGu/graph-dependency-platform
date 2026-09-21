"""test_87_docs_links_must_resolve.py — docs/ 里的仓内路径引用必须可达。

## 为什么需要这道门禁

2026-09-21 把 39 份有长期价值的文档从 `todo/` 归类进 `docs/design|lessons|runbooks`。
移动本身容易，**难的是同步更新引用** —— 当时全仓有 20 多处代码注释与文档互链指向
那些旧路径（其中 `scripts/emit_resilience_scorecard.py` 还真读 `todo/*.json`）。

漏改一处就是一条死链，而死链不会让任何测试变红：文档引用是注释，没人执行。
于是这次整理的价值会被下一次移动悄悄抹掉。本文件把「引用可达」变成可执行的断言。

## 刻意排除 docs/migration/

`docs/migration/timeline.md` 与 `decisions/` 下的 ADR 是**历史决策记录**，
它们指向 `rca/neptune/nl_query_direct.py` 这类已删除的文件是正常的 ——
那正是它们记录的内容（「这个文件在某天被删了」）。
把历史记录也要求可达，等于逼人篡改历史，所以这些目录整体豁免。

## 判据是「路径形状的字符串」，不是全部文本

只检查形如 `<顶层目录>/<路径>.<后缀>` 的引用（顶层目录取自仓库实际结构）。
不检查散文里提到的文件名，因为那些常是泛指（「各个 *_direct.py」）。
宁可漏报，不误报 —— 一个误报频发的门禁会被加进忽略名单，那就等于没有。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 历史记录目录：允许指向已删除的文件
EXEMPT_DIRS = ("docs/migration/",)

#: 仓库顶层目录名 —— 引用必须以它们之一开头才被当作「仓内路径」
TOP_DIRS = ("docs", "todo", "tests", "rca", "chaos", "scripts", "profiles",
            "demo", "infra", "dr-plan-generator", "shared", "experiments")

_PATH_RE = re.compile(
    r"\b((?:" + "|".join(re.escape(d) for d in TOP_DIRS) + r")"
    r"/[\w\-./\u4e00-\u9fff]+\.(?:md|py|yaml|yml|sh|json|ts))")


#: 溯源抬头里的「原路径」**故意**指向已不存在的位置 —— 那是这次归类留下的
#: 历史信息（这份文档原来在哪）。把它当失效引用会让每个归档文档都判失败，
#: 于是门禁只能被整体忽略。所以逐行跳过抬头。
_PROVENANCE_MARKERS = ("归档溯源", "**原路径**")

#: 已删除或已改名的文件 —— 文档在做**历史陈述**时会指向它们，这是正常的。
#:
#: 之所以用显式清单而不是「凡是不存在就豁免」：后者等于取消这道门禁。
#: 列在这里意味着「我们知道它不存在、也确认引用它的那句话是在讲历史」。
#: 新增条目时请确认这两点，不要拿它当消红的快捷方式。
_KNOWN_REMOVED = {
    # 2026-09-20 七个模块的 direct 实现全部删除（docs/migration/timeline.md 有记录）
    "rca/neptune/nl_query_direct.py",
    "rca/collectors/layer2_direct.py",
    "chaos/code/agents/hypothesis_direct.py",
    "chaos/code/agents/learning_direct.py",
    "chaos/code/policy/guard_direct.py",
    "chaos/code/runner/runner_direct.py",
    # 展示站重建时页面改名（1_Graph_Explorer → 3_Graph_Explorer 等）
    "demo/pages/1_Graph_Explorer.py",
    # Neptune 客户端基类在 2026-08 收敛到 infra/lambda/shared/python/ 下
    "shared/neptune_client_base.py",
    "shared/python/neptune_client_base.py",
    # graph_rag_reporter 从 neptune/ 移到 core/
    "infra/lambda/rca_window_flush/neptune/graph_rag_reporter.py",
    # 早期草稿里引用过、从未落地或已删的资料
    "rca/.mcp.json",
    "demo/doc/topology-ap-northeast-1.md",
    "docs/relationships/relationship_synthesis.md",
    "docs/root/dev/writing-intel-modules.md",
    "experiments/fis/rds/fis-aurora-reboot-petlistadoptions.yaml",
    # 讲稿里用省略号表示的示意路径，不是真实文件
    "chaos/.../fault_catalog.yaml",
}


def _strip_provenance(text: str) -> str:
    return "\n".join(
        line for line in text.split("\n")
        if not any(m in line for m in _PROVENANCE_MARKERS))


def _docs_md_files() -> list[pathlib.Path]:
    return sorted(
        p for p in (ROOT / "docs").rglob("*.md")
        if not any(e in str(p.relative_to(ROOT)) for e in EXEMPT_DIRS))


_FILES = _docs_md_files()


def test_t87_01_扫描范围不得为空():
    """守门禁自己：扫到 0 个文件也会「未发现问题」。"""
    assert len(_FILES) >= 35, (
        f"只找到 {len(_FILES)} 个待检 md，预期 ≥35（docs/ 下 design 10 + lessons 26 "
        f"+ runbooks 3 + 根目录若干）。目录结构变了就要同步改本门禁的下限。")


def test_t87_04_已删除清单不得含仍然存在的文件():
    """守门禁自己：`_KNOWN_REMOVED` 里若某个文件其实还在，这条豁免就是个洞。

    文件被删→豁免，后来又被加回来→豁免还在，于是指向它的引用不再被校验。
    这条断言让「豁免过期」本身可被发现。
    """
    alive = sorted(f for f in _KNOWN_REMOVED if (ROOT / f).exists())
    assert not alive, (
        "这些文件已存在，应从 _KNOWN_REMOVED 里移除，让它们重新受校验：\n  "
        + "\n  ".join(alive))


@pytest.mark.parametrize(
    "md", _FILES, ids=[str(p.relative_to(ROOT)) for p in _FILES])
def test_t87_02_文档里的仓内路径必须存在(md: pathlib.Path):
    """每个仓内路径引用都要指向真实存在的文件。

    两类豁免：溯源抬头（记录原路径）、`_KNOWN_REMOVED`（对已删文件的历史陈述）。
    """
    text = _strip_provenance(md.read_text(encoding="utf-8"))
    missing = []
    for m in _PATH_RE.finditer(text):
        rel = m.group(1)
        if rel in _KNOWN_REMOVED:
            continue
        if not (ROOT / rel).exists():
            missing.append(rel)

    assert not missing, (
        f"{md.relative_to(ROOT)} 里这些路径不存在：\n  "
        + "\n  ".join(sorted(set(missing)))
        + "\n\n若文件被移动过，请更新引用；若引用的是已删除的东西且属历史陈述，"
          "请把该文档移进 docs/migration/（豁免目录）或改写为不带路径的描述。")


def test_t87_03_索引必须覆盖三个分类目录():
    """`docs/README.md` 必须为 design / lessons / runbooks 各自列出条目。

    索引漏掉整个分类，等于那批文档对新人不存在 —— 这正是本次整理要解决的问题，
    不该在整理完成后又悄悄退回去。
    """
    readme = ROOT / "docs" / "README.md"
    assert readme.exists(), "docs/README.md 不存在 —— 它是整个文档体系的入口"
    text = readme.read_text(encoding="utf-8")

    for sub in ("design", "lessons", "runbooks"):
        d = ROOT / "docs" / sub
        if not d.is_dir():
            continue
        files = [p.name for p in d.glob("*.md")]
        linked = [f for f in files if f"{sub}/{f}" in text]
        assert linked, (
            f"docs/README.md 没有链接任何 docs/{sub}/ 下的文档"
            f"（该目录有 {len(files)} 个文件）")
        # 不要求 100% 覆盖（允许索引按主题分组时省略个别底稿），但至少覆盖多数
        ratio = len(linked) / max(len(files), 1)
        assert ratio >= 0.7, (
            f"docs/README.md 只链接了 docs/{sub}/ 下 {len(linked)}/{len(files)} "
            f"个文档（{ratio:.0%}）。未被索引的文档对新人等于不存在，请补全：\n  "
            + "\n  ".join(sorted(set(files) - set(linked))))

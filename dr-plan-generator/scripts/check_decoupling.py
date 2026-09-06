#!/usr/bin/env python3
"""
scripts/check_decoupling.py — 守住 dr-plan-generator 与具体 workload 的解耦

检查三件会让解耦悄悄退化的事：

1. **核心代码里的 workload 字面量**（petsite / petadoptions / …）。
   这类硬编码不会报错，只会让工具在换一个客户时生成看起来正常、
   实则指向错误资源的计划。tests/ examples/ docs/ 是白名单。
2. **反向 import 父仓库**（``from profiles`` / ``from shared``）。
   一旦出现，本模块就不能作为独立制品交付。
3. **指向仓库根的 sys.path hack**（``'..', '..'``）。
   同上，而且会在 pip 安装后静默失效。

作为 CI 步骤运行；发现问题以非零码退出。
"""

from __future__ import annotations

import ast
import os
import re
import sys
from typing import Iterator, List, Set, Tuple

PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

#: 核心代码里不允许出现的 workload 专有名词（大小写不敏感）。
FORBIDDEN_LITERALS = (
    "petsite",
    "petadoptions",
    "payforadoption",
    "petsearch",
    "pethistory",
    "petfood",
)

#: 这些目录不参与检查：测试数据、示例产物、文档里出现具体名字是正常的。
EXEMPT_DIRS = {"tests", "examples", "docs", "skills", "references", "__pycache__", "plans", "snapshots"}

#: 本检查器自身：它的字面量就是规则定义。
EXEMPT_FILES = {os.path.join("scripts", "check_decoupling.py")}

_PARENT_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(profiles|shared)\b", re.M)
_PATH_HACK_RE = re.compile(r"sys\.path\.insert\([^)]*['\"]\.\.['\"]\s*,\s*['\"]\.\.['\"]")


def _docstring_nodes(tree: ast.AST) -> Set[int]:
    """收集所有 docstring 常量节点的 id。

    只查**运行时字符串**是刻意的：注释与 docstring 里出现 workload 名字是
    合理的（解释语义、举例说明），把它们算成违规会逼人把文档写得晦涩，
    而真正的风险是被拼进命令的字符串。注释本身不进 AST，自动排除。
    """
    doc_ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                doc_ids.add(id(first.value))
    return doc_ids


def _runtime_strings(source: str) -> Iterator[Tuple[int, str]]:
    """产出 ``(lineno, value)``：源码里所有非 docstring 的字符串字面量。

    Args:
        source: Python 源码。

    Yields:
        行号与字符串值。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    doc_ids = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in doc_ids:
                continue
            yield node.lineno, node.value


def _iter_core_py_files() -> List[str]:
    """遍历核心 Python 文件（跳过豁免目录与自身）。"""
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(PKG_ROOT):
        rel = os.path.relpath(dirpath, PKG_ROOT)
        parts = set(rel.split(os.sep))
        if parts & EXEMPT_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            if os.path.relpath(path, PKG_ROOT) in EXEMPT_FILES:
                continue
            found.append(path)
    return sorted(found)


def check() -> Tuple[int, List[str]]:
    """执行全部检查。

    Returns:
        ``(violation_count, messages)``。
    """
    messages: List[str] = []

    for path in _iter_core_py_files():
        rel = os.path.relpath(path, PKG_ROOT)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()

        for lineno, value in _runtime_strings(text):
            lowered = value.lower()
            for literal in FORBIDDEN_LITERALS:
                if literal in lowered:
                    messages.append(
                        f"{rel}:{lineno}: workload literal {literal!r} in a runtime "
                        f"string — read it from the profile instead: {value.strip()[:80]!r}"
                    )

        for m in _PARENT_IMPORT_RE.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            messages.append(
                f"{rel}:{line_no}: imports parent-repo package {m.group(1)!r} "
                "— dr-plan-generator must be deliverable on its own"
            )

        for m in _PATH_HACK_RE.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            messages.append(
                f"{rel}:{line_no}: sys.path hack pointing at the repo root "
                "— breaks silently once the package is pip-installed"
            )

    return len(messages), messages


def main() -> int:
    """入口。"""
    count, messages = check()
    if count:
        print(f"Decoupling check FAILED — {count} violation(s):\n", file=sys.stderr)
        for msg in messages:
            print(f"  {msg}", file=sys.stderr)
        return 1
    print("Decoupling check passed: no workload literals, no parent-repo imports.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

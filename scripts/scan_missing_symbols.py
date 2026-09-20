#!/usr/bin/env python3
"""scan_missing_symbols.py — 扫出「引用了不存在的符号」这类缺陷。

## 为什么需要它

2026-09-20 一天之内抓到三个同形状的缺陷，全都是**靠线上日志偶然暴露**的：

    fault_classifier.classify_group()    从来不存在 → except 降级，五个月没人发现
    rca_engine.analyze_group()           同上
    NeptuneGraphManager                  整个仓库无定义，`# type: ignore` 压掉了警告

共同点：调用一个不存在的符号，外面包着 except，于是
**不报错、不变红、也不让任何指标异常** —— 只是那段设计从来没生效过。

靠运气发现三个之后，该主动扫一遍。

## 扫两种引用形式

    A. `from X import Y`        —— Y 不在 X 的顶层定义里
    B. `alias.attr(...)`        —— alias 由 import 绑到某个项目模块，attr 不在其中

B 是关键：`classify_group` 那类正是这种形式，只扫 import 抓不到。

## 限度（写清楚，避免过度信任）

- 只检查**项目内**模块，第三方库跳过（它们的属性常是动态生成的）
- 动态属性（`setattr` / `globals()` 注入 / `__getattr__`）会误报，用白名单排除
- 类实例的方法调用不在扫描范围内（只扫模块级属性访问）

所以它是**降低漏网率**的工具，不是完备证明。有输出就该逐条看，
确认是真缺失还是动态生成。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 扫描范围：会被打进部署包 / 真实运行的代码
SCAN_DIRS = ["rca", "chaos/code", "scripts", "demo"]

# 这些目录不扫：实验代码与构建产物
SKIP_PARTS = {"__pycache__", "cdk.out", "node_modules", "experiments", ".git", "venv"}

# 已知使用动态属性的模块（扫描器无法静态确认，人工确认过）
DYNAMIC_OK = {
    "boto3", "botocore", "st", "streamlit", "pytest", "yaml", "json",
    "os", "sys", "time", "re", "logging", "math", "random", "collections",
}


def _iter_py_files():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if SKIP_PARTS & set(p.parts):
                continue
            yield p


def _module_file(mod: str) -> pathlib.Path | None:
    """把点号模块名映射到项目内文件。找不到返回 None（= 第三方或动态）。"""
    parts = mod.split(".")
    # 项目里的 import 根有多个：顶层、rca/、chaos/code/
    for prefix in ([], ["rca"], ["chaos", "code"]):
        cand = ROOT.joinpath(*prefix, *parts).with_suffix(".py")
        if cand.exists():
            return cand
        pkg = ROOT.joinpath(*prefix, *parts, "__init__.py")
        if pkg.exists():
            return pkg
    return None


_TOPLEVEL_CACHE: dict[pathlib.Path, set[str]] = {}


def _toplevel_names(path: pathlib.Path) -> set[str]:
    """一个模块文件里所有**顶层**可被 import / 属性访问的名字。"""
    if path in _TOPLEVEL_CACHE:
        return _TOPLEVEL_CACHE[path]
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        _TOPLEVEL_CACHE[path] = names
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.If, ast.Try)):
            # 条件/兜底里的定义也算（常见于 try: import ... except: 定义替身）
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        names.add(a.asname or a.name)
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    _TOPLEVEL_CACHE[path] = names
    return names


def scan_file(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """返回 [(行号, 类型, 描述)]。"""
    out: list[tuple[int, str, str]] = []
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return out

    alias_to_mod: dict[str, str] = {}   # 局部名 → 项目模块名

    for node in ast.walk(tree):
        # ── A. from X import Y ──
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mf = _module_file(node.module)
            if mf is not None:
                have = _toplevel_names(mf)
                for a in node.names:
                    if a.name == "*":
                        continue
                    # 子模块形式（from pkg import submodule）也算存在
                    if _module_file(f"{node.module}.{a.name}") is not None:
                        continue
                    if a.name not in have:
                        out.append((node.lineno, "MISSING-IMPORT",
                                    f"from {node.module} import {a.name} —— "
                                    f"{mf.relative_to(ROOT)} 里没有这个顶层名"))
            # 记录 alias：from core import fault_classifier
            for a in node.names:
                sub = f"{node.module}.{a.name}"
                if _module_file(sub) is not None:
                    alias_to_mod[a.asname or a.name] = sub
        # ── import X.Y as Z ──
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _module_file(a.name) is not None:
                    alias_to_mod[a.asname or a.name.split(".")[0]] = a.name

    # ── B. alias.attr 访问 ──
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in DYNAMIC_OK or base not in alias_to_mod:
                continue
            mf = _module_file(alias_to_mod[base])
            if mf is None:
                continue
            if node.attr not in _toplevel_names(mf):
                out.append((node.lineno, "MISSING-ATTR",
                            f"{base}.{node.attr} —— "
                            f"{mf.relative_to(ROOT)} 里没有这个顶层名"))
    return out


def main() -> int:
    findings: list[tuple[pathlib.Path, int, str, str]] = []
    for p in _iter_py_files():
        for lineno, kind, desc in scan_file(p):
            findings.append((p.relative_to(ROOT), lineno, kind, desc))

    if not findings:
        print("✅ 未发现引用不存在的符号")
        return 0

    # 去重（同一 module.attr 在一个文件里可能被引用多次）
    seen = set()
    uniq = []
    for f in findings:
        key = (f[0], f[3])
        if key not in seen:
            seen.add(key)
            uniq.append(f)

    print(f"⚠️ 发现 {len(uniq)} 处可疑引用（去重后）：\n")
    for path, lineno, kind, desc in sorted(uniq):
        print(f"  {path}:{lineno}  [{kind}]")
        print(f"      {desc}")
    print()
    print("注意：动态属性（setattr / __getattr__ / globals 注入）会误报，")
    print("      逐条确认是真缺失还是动态生成，别直接当 bug 清单用。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

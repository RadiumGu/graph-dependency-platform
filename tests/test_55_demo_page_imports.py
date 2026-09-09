"""tests/test_55_demo_page_imports.py — demo 页面的仓内依赖必须进镜像

## 这条门禁防的是一个真实踩到的失效模式

2026-09-09 新增 `demo/pages/10_Compliance_Report.py`，它 `import compliance_export`。
本地跑得好好的（仓库根在 sys.path 上），但 `demo/Dockerfile` 的 COPY 列表里
**没有** `compliance_export/` —— 镜像里根本没这个目录。

后果的形状很关键：**构建不报错、启动不报错、健康检查通过**，只在访客点开那一页
时抛 ModuleNotFoundError。而线上环境（EC2 `openclaw-instance-v2` 上的
`/home/ubuntu/tech/graph-dependency-platform`）是另一份代码拷贝，同样会缺目录。

这与本仓库反复出现的那类缺陷同源：**一个看起来在工作、实际在特定路径上才失效
的东西**（ConsumesFrom 幽灵标签、drift 标签清单不全、rca_window_flush 的死代码）。

## 判据

扫 `demo/` 下所有 .py 的 import，取仓内顶层模块名（即仓库根下存在同名目录或
.py 的那些），断言每个都出现在 Dockerfile 的 COPY 列表里。

刻意**不**检查第三方包 —— 那是 requirements.txt 的事，且装不上会在构建时就炸，
不属于本判据要防的「静默失效」。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DEMO = _ROOT / "demo"
_DOCKERFILE = _DEMO / "Dockerfile"

#: 这些顶层名在仓库根下存在，但**不需要**进镜像。
#: 每一项都要写清理由 —— 白名单是本判据唯一的逃生口，不能变成垃圾桶。
_NOT_NEEDED = {
    "demo": "就是它自己（Dockerfile 已 COPY demo/）",
    "tests": "测试不进运行镜像",
    "scripts": "运维脚本不进运行镜像",
    "infra": "IaC 不进运行镜像",
    "experiments": "实验产物不进运行镜像",
    "todo": "笔记不进运行镜像",
    "docs": "文档不进运行镜像",
}


def _repo_toplevel_names() -> set:
    """仓库根下的顶层可导入名（目录含 __init__.py 或裸目录 / .py 文件）。"""
    names = set()
    for p in _ROOT.iterdir():
        if p.name.startswith(".") or p.name.startswith("_"):
            continue
        if p.is_dir():
            names.add(p.name)
        elif p.suffix == ".py":
            names.add(p.stem)
    return names


def _demo_imports() -> dict:
    """{顶层模块名: [引用它的文件, ...]} —— 只含仓内模块。"""
    repo_names = _repo_toplevel_names()
    out: dict = {}
    for py in sorted(_DEMO.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):          # walk = 含函数体内的延迟导入
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                top = m.split(".")[0]
                if top in repo_names:
                    out.setdefault(top, []).append(str(py.relative_to(_ROOT)))
    return out


def _dockerfile_copied() -> set:
    """Dockerfile 里 COPY 进镜像的顶层名。"""
    src = _DOCKERFILE.read_text(encoding="utf-8")
    copied = set()
    for m in re.finditer(r"^COPY\s+(\S+)\s+", src, re.M):
        first = m.group(1).rstrip("/")
        copied.add(first.split("/")[0])
    return copied


def test_m01_页面import的仓内模块必须进镜像():
    """每个被 demo 引用的仓内顶层模块都要在 Dockerfile 的 COPY 列表里。

    漏了不会在构建时报错 —— 只在容器里点开那一页时抛 ModuleNotFoundError，
    而本地跑得好好的。
    """
    assert _DOCKERFILE.exists(), "找不到 demo/Dockerfile"
    imports = _demo_imports()
    assert imports, "没解析出任何仓内 import —— 判据很可能失效了"

    copied = _dockerfile_copied()
    missing = []
    for top, users in sorted(imports.items()):
        if top in _NOT_NEEDED or top in copied:
            continue
        missing.append("%s（被 %s 引用）"
                       % (top, ", ".join(sorted(set(users))[:3])))
    assert not missing, (
        "以下仓内模块被 demo 页面 import，但 demo/Dockerfile 没有 COPY：\n  "
        + "\n  ".join(missing)
        + "\n\n镜像里没有这些目录，页面会在容器里抛 ModuleNotFoundError —— "
          "而构建、启动、健康检查全都不会报错。\n"
          "修法：在 demo/Dockerfile 的 COPY 段加一行。")


def test_m02_白名单每一项都必须有理由():
    """白名单是唯一逃生口，不能变成垃圾桶。"""
    for name, reason in _NOT_NEEDED.items():
        assert reason and len(reason) > 4, (
            "白名单项 %r 没有写清理由。每一项都要说明为什么不需要进镜像。" % name)
    stale = sorted(n for n in _NOT_NEEDED if n not in _repo_toplevel_names())
    assert not stale, (
        "白名单里这些顶层名在仓库根下已不存在，应移除：%s\n"
        "过期的白名单条目会掩盖真正的缺失。" % stale)


def test_m03_compliance_export_必须在列():
    """点名断言 —— 这是踩出这条门禁的那个具体缺陷，值得单独钉住。

    泛化判据（m01）已经覆盖它，但泛化判据可能被将来的重构绕过（比如有人把
    import 改成 importlib 动态导入）。这条点名断言是第二道防线。
    """
    page = _DEMO / "pages" / "10_Compliance_Report.py"
    if not page.exists():
        pytest.skip("合规报告页不存在")
    src = page.read_text(encoding="utf-8")
    assert "compliance_export" in src, "合规报告页应引用 compliance_export"
    assert "compliance_export" in _dockerfile_copied(), (
        "demo/Dockerfile 没有 COPY compliance_export/ —— "
        "合规报告页在容器里会抛 ModuleNotFoundError。")

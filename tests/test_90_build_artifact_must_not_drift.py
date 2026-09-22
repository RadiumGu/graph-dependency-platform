"""test_90 —— 构建产物不得与权威源漂移。

`infra/lambda/rca_window_flush/` 是 `build.sh` 的输出目录（`DEST_DIR` 默认就是
脚本自己所在目录），CDK 从它 `fromAsset` 打包
（`infra/lib/alert-buffer-stack.ts:166`）。权威源是 `rca/`。

## 为什么这道门禁是必要的

产物被 git 跟踪，而 `rca/` 更新后产物不会自动跟上。2026-09-21 的审查对着这份
陈旧副本得出两条「已确证」的结论，2026-09-22 实测全部推翻：

    「产物缺 analyze_group → 窗口聚合 RCA 从未生效」
        → 线上部署包有 analyze_group，rca_engine.py 1035 行与 rca/ 逐字节一致

    「产物 fault_classifier 在 combined_tier0 >= 2 时自动升 P0」
        → combined_tier0 在线上与权威源里出现 0 次，只在陈旧产物里有 6 次

当时 11 个 .py 全部漂移（neptune_queries.py 差 410 行，handler.py 是产物比
权威源多 133 行），而逐个下载线上部署包比对是 11/11 `线上 ≡ rca/`。

也就是说：这份产物**谁也不影响，只误导读代码的人**。危害不是功能失效，
而是让判断建立在一份与线上无关的副本上 —— 比缺失更糟，因为它看起来是真的。

本门禁把漂移变成可见失败。修法是从 `rca/` 重新同步，不是改产物里的副本。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "infra" / "lambda" / "rca_window_flush"
AUTHORITY_DIR = ROOT / "rca"

# 产物目录里**不来自** rca/ 的文件：它们是构建脚本与说明，不参与一致性比对。
_NOT_FROM_AUTHORITY = {"build.sh", "GENERATED.md"}


def _tracked_py() -> list[str]:
    """被 git 跟踪的产物 .py 相对路径。

    只比对被跟踪的文件：build.sh 还会 pip install 一堆依赖进同一目录，
    那些不在版控里，也不该参与比对。
    """
    out = subprocess.run(
        ["git", "ls-files", str(ARTIFACT_DIR.relative_to(ROOT))],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    prefix = str(ARTIFACT_DIR.relative_to(ROOT)) + "/"
    return [
        f[len(prefix):] for f in out
        if f.endswith(".py") and f.startswith(prefix)
        and f[len(prefix):] not in _NOT_FROM_AUTHORITY
    ]


def test_t90_01_扫描范围不得为空():
    tracked = _tracked_py()
    assert tracked, (
        "git 里找不到被跟踪的产物 .py。如果产物已按结构性方案从版控移除，"
        "本文件应连同一起删除 —— 但别让它静默通过"
    )


@pytest.mark.parametrize("rel", _tracked_py())
def test_t90_02_产物必须与权威源逐字节一致(rel):
    art = ARTIFACT_DIR / rel
    src = AUTHORITY_DIR / rel
    assert src.exists(), (
        f"产物里有 {rel}，但 rca/ 权威源里没有对应文件 —— "
        f"要么它是产物独有的残留（应删），要么权威源少了文件"
    )
    a = art.read_bytes()
    b = src.read_bytes()
    assert a == b, (
        f"{rel} 产物与权威源漂移：产物 {len(a)} 字节 / 权威 {len(b)} 字节。\n"
        f"修法是 cp rca/{rel} infra/lambda/rca_window_flush/{rel}（或重跑 build.sh），"
        f"**不是**修改产物里的副本 —— 要改代码请改 rca/。\n"
        f"注意：线上跑的是部署时重新 build 出来的包，不是这份产物。"
        f"要判断线上行为请下载 gp-window-flush 的部署包核实，见 GENERATED.md"
    )


def test_t90_03_产物目录必须带警示说明():
    """防止说明被删后又有人对着产物下结论。"""
    md = ARTIFACT_DIR / "GENERATED.md"
    assert md.exists(), (
        "缺 infra/lambda/rca_window_flush/GENERATED.md —— 它说明本目录是构建产物、"
        "权威源是 rca/、以及如何核实线上真实代码"
    )
    text = md.read_text(encoding="utf-8")
    for kw in ("构建产物", "rca/", "gp-window-flush"):
        assert kw in text, f"GENERATED.md 里缺少关键信息: {kw}"


def test_t90_04_CDK仍从该目录打包():
    """本门禁的前提是 CDK 从产物目录打包。若前提变了要回来复核。"""
    stack = ROOT / "infra" / "lib" / "alert-buffer-stack.ts"
    assert stack.exists(), "找不到 alert-buffer-stack.ts"
    src = stack.read_text(encoding="utf-8")
    assert "lambda/rca_window_flush" in src, (
        "CDK 不再从 lambda/rca_window_flush 打包 —— 产物与权威源的关系变了，"
        "请复核本文件与 GENERATED.md 的前提"
    )

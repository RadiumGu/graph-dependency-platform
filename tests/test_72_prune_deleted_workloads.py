"""tests/test_72_prune_deleted_workloads.py — 图谱清理工具的安全约束

`scripts/prune_deleted_workloads.py` 会 `DETACH DELETE` 图谱节点。这是本仓库
少数会**删除**数据的工具，所以它的安全性不能只靠 docstring 里的纪律。

## 三条约束，对应三种真实可能的失效

    t72_01  必须 dry-run 默认，`--apply` 才实写
    t72_02  「活集群里没有」这条判据不得被绕过，且查询失败必须中止
    t72_03  清理目标必须声明式且带理由，不许从命令行临时指定

第三条尤其重要：一次性命令行调用不留痕迹，而删除操作必须能回答「谁在什么时候
以什么理由删了什么」。声明在 `PRUNE_TARGETS` 里，git 就是那份记录。
"""
from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "prune_deleted_workloads.py"


def _src() -> str:
    assert _SCRIPT.exists(), "找不到 %s" % _SCRIPT
    return _SCRIPT.read_text(encoding="utf-8")


def test_t72_01_必须dry_run默认():
    """删除类工具默认必须是 dry-run。

    与 `scripts/clean_nondependency_verify_attrs.py` 同一约定 —— 保持一致，
    免得两个清理脚本一个默认写一个默认不写。
    """
    src = _src()
    assert '"--apply" in sys.argv' in src, (
        "没有看到 `--apply` 开关。删除类工具必须 dry-run 默认。")
    # 实际的删除语句必须在 apply 分支之后
    apply_idx = src.index('apply = "--apply" in sys.argv')
    del_idx = src.index("DETACH DELETE")
    assert del_idx > apply_idx, "DETACH DELETE 出现在 apply 判定之前"
    assert "if not apply:" in src, (
        "没有 `if not apply:` 提前返回 —— dry-run 可能会走到删除分支。")


def test_t72_02_活集群判据不得被绕过():
    """「图谱里有、活集群里没有」是唯一删除判据，且查询失败必须中止。

    ## 为什么这条要单独立门禁

    `_live_k8s_names()` 返回 None 表示 kubectl 查询失败。若调用方把 None
    当成空集，「活集群里没有」这个判据会对**所有节点**成立 —— 一次 kubectl
    抖动就能摘掉整张图。

    这与本仓库的 skip 纪律同源：连不上外部系统时只对连接失败放行，
    绝不把工具失败读成「返回空」。
    """
    src = _src()
    assert "def _live_k8s_names" in src, "缺少活集群清单函数"
    assert "if live is None:" in src, (
        "没有处理 `_live_k8s_names()` 返回 None 的情况。"
        "查询失败被当成空集会摘掉整张图。")
    # None 分支必须是中止，不是继续
    tree = ast.parse(src)
    main_fn = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    assert main_fn is not None, "找不到 main()"
    seg = src[src.index("if live is None:"):]
    seg = seg[:seg.index("\n\n")] if "\n\n" in seg else seg
    assert "return" in seg, (
        "`live is None` 分支没有 return —— 必须中止，不能继续执行删除。")

    # 保留判据必须存在：活集群里有就不摘
    assert 'in live' in src, (
        "没有看到「名字在活集群清单里则保留」的判据。")


def test_t72_03_清理目标必须声明式且带理由():
    """目标写在 PRUNE_TARGETS 里，每项必须有 namespace / cfn_stack / why。

    不接受从命令行传 namespace —— 一次性调用不留痕迹，
    而删除操作必须能回答「谁在什么时候以什么理由删了什么」。
    """
    src = _src()
    assert "PRUNE_TARGETS" in src, "缺少声明式的 PRUNE_TARGETS"

    tree = ast.parse(src)
    node = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PRUNE_TARGETS" for t in n.targets):
            node = n.value
            break
    assert node is not None, "PRUNE_TARGETS 不是模块级赋值"
    targets = ast.literal_eval(node)
    assert targets, "PRUNE_TARGETS 为空"
    for t in targets:
        for k in ("namespace", "cfn_stack", "why"):
            assert k in t and t[k], (
                "PRUNE_TARGETS 的某项缺少 %r：%s" % (k, t))
        assert len(t["why"]) >= 20, (
            "理由过短，说不清为什么可以删：%r" % t["why"])

    # 不得提供命令行指定 namespace 的入口
    assert not re.search(r"add_argument\(\s*['\"]--namespace", src), (
        "出现了 `--namespace` 命令行参数。目标必须声明在 PRUNE_TARGETS 里，"
        "否则删除操作不留痕迹。")


def test_t72_04_审计清单必须先落盘再删():
    """删了什么必须可追溯，且清单要在删除之前写。

    顺序反了的话，删除中途失败会得到一份不完整的记录 ——
    而不完整的删除记录比没有记录更危险（你以为记全了）。
    """
    src = _src()
    assert "manifest.write_text" in src, "没有写审计清单"
    w = src.index("manifest.write_text")
    d = src.index("DETACH DELETE")
    assert w < d, "审计清单在 DETACH DELETE 之后才写 —— 顺序必须反过来"


def test_t72_05_两道守卫都必须存在():
    """namespace 已消失 + CFN 栈已删除，两道守卫缺一不可。

    只查 K8s 不够：AWS 侧资源还在时 `aws-etl` 下一轮会把节点重新学回来，
    此时摘掉只是徒劳，还会掩盖「资源其实没删干净」这个事实。
    """
    src = _src()
    assert "def _namespace_gone" in src, "缺少 namespace 守卫"
    assert "def _stack_gone" in src, "缺少 CFN 栈守卫"
    assert "DELETE_COMPLETE" in src, (
        "栈守卫没有检查 DELETE_COMPLETE 状态。")
    # 两道守卫都必须能拒绝（返回 False 后 continue）
    assert src.count("if not ok:") >= 2, (
        "两道守卫必须各自能拒绝执行，实际只看到 %d 处拒绝分支。"
        % src.count("if not ok:"))

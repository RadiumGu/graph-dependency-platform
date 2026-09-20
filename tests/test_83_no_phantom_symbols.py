"""不得引用不存在的符号，也不得留下「跟自己对比」的空壳测试。

## 为什么要有这组门禁

2026-09-20 一天之内抓到三个「调用不存在的符号」的缺陷，**全都是靠线上
日志偶然暴露的**：

    fault_classifier.classify_group()    从来不存在 → except 降级，五个月无人发现
    rca_engine.analyze_group()           同上
    NeptuneGraphManager                  整个仓库无定义，`# type: ignore` 压掉警告

它们的共同点不是「写错了名字」，而是**外面包着 except，所以不报错、
不变红、也不让任何指标异常** —— 那段设计从来没生效过，而一切看起来正常。

靠运气发现三个之后，把检查固化下来：`scripts/scan_missing_symbols.py`
静态扫 `from X import Y` 与 `alias.attr` 两种形式。本门禁调用它。

## 同类的第二种形状：空壳测试

删掉 direct 实现后，`test_learning_shadow.py` / `test_policy_guard_shadow.py`
仍在跑，而它们是这样构造被测对象的：

    os.environ["LEARNING_ENGINE"] = "direct"
    direct = make_learning_engine()          # factory 已去回退 → 返回 strands
    os.environ["LEARNING_ENGINE"] = "strands"
    strands = make_learning_engine()         # 同样是 strands

实证两者 `type(e1) is type(e2) == True` —— **测试在跟自己对比**，
必然通过，零价值。这也是一种假绿：测试还在，但它守的东西已经消失。
"""
import subprocess
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_t83_01_不得引用不存在的符号():
    """`scripts/scan_missing_symbols.py` 必须无发现。

    有发现时**不要直接把它当 bug 清单**：动态属性（setattr / `__getattr__` /
    globals 注入）会误报。逐条确认是真缺失还是动态生成；确认是动态的，
    就把模块名加进扫描器的 `DYNAMIC_OK`，并在那里写清为什么。
    """
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_missing_symbols.py")],
        capture_output=True, text=True, timeout=600,
    )
    assert r.returncode == 0, (
        "发现引用了不存在的符号 —— 这类缺陷外面通常包着 except，"
        "不会报错也不会让测试变红，只是那段逻辑从来不生效：\n\n"
        + r.stdout + r.stderr
    )


def test_t83_02_扫描器本身不得空跑():
    """守住门禁自己：扫描器必须真的扫到文件与符号。

    这条不是多余的。本会话吃过一次「判据正确但取样位置错误 = 假绿」的亏：
    如果扫描器因为路径问题扫到 0 个文件，它会开心地报「未发现」，
    而 T83-01 会一直绿着 —— 那比没有这个门禁更糟，因为它给人虚假的安心。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import scan_missing_symbols as S

    files = list(S._iter_py_files())
    assert len(files) > 100, f"扫描器只扫到 {len(files)} 个文件，疑似路径失效"

    # 项目内模块必须能被定位，否则 MISSING-ATTR 那半边扫描是空跑
    probes = ["core.fault_classifier", "neptune.neptune_queries",
              "engines.factory", "collectors.layer2_tools"]
    located = [m for m in probes if S._module_file(m) is not None]
    assert len(located) == len(probes), (
        f"这些项目模块定位不到 {set(probes) - set(located)} —— "
        "说明 _module_file 的搜索前缀失效，属性扫描形同空跑"
    )


def test_t83_03_不得留下跟自己对比的shadow测试():
    """direct 实现已全部删除，任何 direct-vs-strands 对比测试都是空壳。

    这类测试会「通过」，因为两边拿到的是同一个类。留着它们比删掉更坏：
    CI 绿灯会让人以为「两个引擎的等价性有守护」，而其实什么都没验证。
    """
    leftovers = sorted(p.name for p in (ROOT / "tests").glob("*shadow*.py"))
    assert not leftovers, (
        f"仍有 shadow 对比测试残留：{leftovers}。\n"
        "direct 实现已全部删除，factory 也去掉了回退分支，所以设 "
        "ENGINE=direct 拿到的仍是 strands —— 这些测试在跟自己对比，"
        "必然通过且零价值。应当删除，或改写成只针对 strands 的契约测试"
        "（参考 tests/test_81_nlquery_contract_strands.py）。"
    )


def test_t83_04_rca不得依赖不在部署包里的chaos模块():
    """`rca/` 顶层代码不得 import chaos 的 `runner.*` / `agents.*`。

    `runner/` 与 `agents/` **都不在 rca 的部署包清单里**
    （build.sh 只复制 core/neptune/actions/collectors/data/search/engines），
    所以这类 import 在 Lambda 里必然 ImportError —— 本地跑得通、线上必挂，
    是最难发现的一类。`probe_neptune` 就是这么坏了不知多久。

    `engines/factory.py` 是**已知例外**：它按 env 构造 chaos 侧的
    hypothesis / learning 引擎，而那两个函数只被 chaos CLI 与测试调用，
    两个 Lambda 都不走。例外写在这里而不是默默放过 —— 若将来 Lambda
    需要它们，必须先把 agents/ 加进打包清单。
    """
    import re

    ALLOW = {"rca/engines/factory.py"}
    pat = re.compile(r"^\s*(from\s+(runner|agents)[\s.]|import\s+(runner|agents)\b)")
    bad = []
    for p in (ROOT / "rca").rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(ROOT))
        if rel in ALLOW:
            continue
        for i, ln in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            if pat.match(ln):
                bad.append(f"{rel}:{i}  {ln.strip()}")
    assert not bad, (
        "rca/ 不得 import chaos 的 runner/ 或 agents/（它们不在部署包清单里，"
        "Lambda 里会 ImportError）：\n  " + "\n  ".join(bad)
    )

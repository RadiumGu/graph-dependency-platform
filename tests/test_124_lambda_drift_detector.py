"""test_124_lambda_drift_detector.py — 守住双向漂移检测与「合并 ≠ 部署」的教训。

## 守什么

1. **两个方向都要查。** 「线上有、源码没有」会被重新部署静默回滚；
   「源码有、线上没有」则让所有人以为已修好而实际没在跑 ——
   后者更隐蔽，因为查源码、查 PR、查 CI 全都显示「已修复」。

2. **方向 A 不能单独当结论。** 「函数比栈新」只说明栈外改过。实测
   neptune-etl-trigger 比栈新 3163 小时却与仓库逐字一致 ——
   栈外改动恰好是把线上对齐到仓库。A 的 5 条被 B 收敛成 1 条真风险。

3. **不能用「逐字一致」做跨形态比较。** esbuild 打包产物与未打包源码
   永远不可能逐字相同；SOURCE_MAP 只放同形态的 Python 模块。

4. **报告必须先报覆盖面。** 不在 SOURCE_MAP 里的函数没被内容比对查过，
   不说出来就会被当成「全查过了」。

5. **etl_deepflow 的前置条件已全部核实** —— 这不是一个「待调研」项，
   残留未知只有三处运行时变量参数。这个区分丢了，下一个人会从头查一遍。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
SCRIPT = ROOT / "crons" / "lambda_drift.py"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.50")
    return txt[start : txt.index("## 六、待记录", start)]


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


class TestBothDirections:
    def test_detects_source_ahead_of_production(self, src: str):
        assert "源码有、线上没有" in src

    def test_detects_production_ahead_of_source(self, src: str):
        assert "线上有、源码没有" in src

    def test_explains_consequence_of_each(self, src: str):
        assert "所有人以为已修好，实际没在跑" in src
        assert "任何重新部署都会静默回滚修复" in src

    def test_direction_a_is_not_conclusive_on_its_own(self, src: str):
        """函数比栈新 ≠ 有风险；必须用内容比对核实。"""
        assert "不能单独当结论" in src


class TestCoverageHonesty:
    def test_reports_direction_b_coverage(self, src: str):
        assert "方向 B 覆盖面" in src

    def test_says_what_was_not_checked(self, src: str):
        assert "没有被内容比对检查过" in src

    def test_source_map_is_the_coverage(self, src: str):
        assert "这个表就是覆盖面本身" in src


class TestCrossFormatComparisonTrap:
    def test_esbuild_trap_recorded_in_script(self, src: str):
        assert "esbuild" in src
        assert "同形态" in src

    def test_source_map_holds_only_python(self, src: str):
        """那个 Node Lambda 刻意不在表里 —— 它线上是打包产物。

        2026-10-04 起 value 是**成员列表**（tuple of (path, member)），所以
        这里要嵌套遍历。只放未打包 .py 这条不变 —— json / 打包产物不要进来，
        跨形态比对永远不可能逐字相同。
        """
        import ast

        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "SOURCE_MAP" for t in node.targets
            ):
                vals = ast.literal_eval(node.value)
                assert vals, "SOURCE_MAP 不能是空的"
                for fn, pairs in vals.items():
                    assert isinstance(pairs, tuple) and pairs, (
                        f"{fn} 的 value 必须是非空的成员列表 —— "
                        "单成员判据在 2026-10-04 两次给出假绿"
                    )
                    for pair in pairs:
                        assert isinstance(pair, tuple) and len(pair) == 2, (
                            f"{fn} 的成员项必须是 (path, member) 二元组，实际 {pair!r}"
                        )
                        _path, member = pair
                        assert member.endswith(".py"), f"{member} 不是未打包的 .py"
                return
        pytest.fail("找不到 SOURCE_MAP")


class TestThirdInstanceOfMergedNotDeployed:
    def test_three_instances_recorded(self, section: str):
        assert_contains(section, "**第三次「合并 ≠ 部署」**")
        assert_contains(section, "早 3 分 53 秒")

    def test_third_kind_is_the_most_hidden(self, section: str):
        assert_contains(section, "查源码、查 PR、查 CI 全都显示「已修复」")

    def test_the_commit_title_irony_recorded(self, section: str):
        """提交标题说「唯一无门禁的写入方」—— 没部署，所以那句话仍成立。"""
        assert_contains(section, "所以那句话至今仍然成立")


class TestPreconditionsAreClosed:
    def test_vocabulary_checked(self, section: str):
        assert_contains(section, "字面断言值是否都在词表里")

    def test_layer_has_the_module(self, section: str):
        assert_contains(section, "反证层可用")

    def test_layer_lag_is_additive_only(self, section: str):
        assert_contains(section, "差异是**纯新增**")

    def test_residual_unknown_named(self, section: str):
        """不能说「零风险」—— 三处参数是运行时变量。"""
        assert_contains(section, "取值只能在运行时判断")

    def test_default_mode_is_enforce_not_warn(self, section: str):
        assert_contains(section, "**默认是 enforce 不是 warn**")

    def test_two_paths_tradeoff_recorded(self, section: str):
        assert_contains(section, "一次收敛 5.5 个月的漂移")


class TestImageDriftConclusion:
    def test_cdk_assets_built_from_source(self, section: str):
        """这解释了为什么镜像层面风险低。"""
        assert_contains(section, "部署时从本地源码构建")

    def test_health_check_direction_was_recorded_backwards(self, section: str):
        assert_contains(section, "方向记反了")


class TestMainModuleCanHideDrift:
    """单成员判据会给出假绿 —— 两次实测，所以 SOURCE_MAP 改成多成员。

    2026-10-04 两个独立实例，都是「被检查的那个模块恰好一致」：

        gp-window-flush        入口 window_flush_handler.py 一致，
                               而 neptune/neptune_queries.py 漂移 181 B
                               （PR #47 的 5 处 Pod active 过滤）
        neptune-etl-from-aws   表里的 neptune_client.py 一致，
                               而 neptune_client_base.py + business_layer.py
                               合计漂移 3110 B（含清 6 条业务层假边）

    主模块往往是最稳定的那个（入口签名很少改），业务逻辑在依赖模块里 ——
    所以单成员判据在结构上偏向漏报。本段守住多成员不被退回去。
    """

    @staticmethod
    def _source_map(src: str) -> dict:
        import ast

        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "SOURCE_MAP" for t in node.targets
            ):
                return ast.literal_eval(node.value)
        pytest.fail("找不到 SOURCE_MAP")

    def test_drifted_functions_are_multi_member(self, src: str):
        """两个实测漂移过的函数必须是多成员 —— 退回单成员就是退回假绿。"""
        sm = self._source_map(src)
        for fn in ("gp-window-flush", "neptune-etl-from-aws"):
            assert fn in sm, f"{fn} 不在 SOURCE_MAP 里"
            assert len(sm[fn]) > 1, (
                f"{fn} 只有 {len(sm[fn])} 个成员。它是 2026-10-04 实测到"
                "「被检查的模块恰好一致、漂移在别的模块里」的实例，"
                "单成员会让它报假绿。"
            )

    def test_actually_drifted_members_are_covered(self, src: str):
        """实测漂移过的那几个模块必须在表里 —— 它们是判据的来源。"""
        sm = self._source_map(src)
        required = {
            # 函数名: 必须覆盖的成员（都是 2026-10-04 实测确认漂移过的）
            "gp-window-flush": {"neptune/neptune_queries.py"},
            "neptune-etl-from-aws": {"neptune_client_base.py", "business_layer.py"},
        }
        for fn, must in required.items():
            members = {m for _p, m in sm.get(fn, ())}
            missing = must - members
            assert not missing, (
                f"{fn} 没覆盖实测漂移过的模块：{sorted(missing)}。\n"
                "这些模块正是暴露「单成员假绿」的证据，去掉它们等于把判据的"
                "依据删掉。"
            )

    def test_package_downloaded_once_per_function(self, src: str):
        """多成员必须复用同一个下载好的包，不能每个成员下一次整包。

        etl_aws 有 7 个成员、gp-window-flush 有 5 个。沿用原来
        「每取一个成员下载一次整包」会把一次巡检的下载量放大一个量级
        （单包 1~15 MB）。
        """
        assert "_deployed_package" in src, (
            "缺 _deployed_package —— 多成员比对必须先整包下载一次再逐个取成员"
        )
        assert "_member_from_package" in src, "缺 _member_from_package"
        # check() 里应当调整包下载而不是逐成员下载
        assert "blob = _deployed_package(lam, fn)" in src, (
            "check() 没有按函数整包下载一次"
        )

    def test_checked_b_still_counts_functions(self, src: str):
        """checked_b 必须仍按函数计数 —— #51 把静默判据接在它上面。

        改成按模块计数会让 `checked_b/len(SOURCE_MAP)` 的分母语义错位，
        而那个表达式参与 #51 刚修好的「没有新漂移就 Skip」。
        """
        assert "checked_b}/{len(SOURCE_MAP)}" in src or \
               "{checked_b}/{len(SOURCE_MAP)}" in src, (
            "找不到按函数计数的覆盖面表达式"
        )
        assert "verified_any" in src, (
            "缺 verified_any —— 必须「至少一个成员真比对成功」才算该函数已核实，"
            "否则「全部成员都取不到」会冒充已核实"
        )

    def test_false_green_evidence_is_recorded(self, src: str):
        """「单成员会给出假绿」的实测记录必须留在代码里。

        不写的话，下一个人看到 etl_aws 列了 7 个模块会觉得啰嗦而精简回一个 ——
        而那恰好是这次要修的东西。
        """
        assert_contains(src, "单成员会给出假绿")
        assert_contains(src, "恰好一致")


class TestMemberMatchMustNotGuess:
    """成员匹配不能用裸 endswith —— 它会命中同后缀的别的文件。

    2026-10-04 实测：多成员改造后给 gp-window-flush 加了 `handler.py`，
    裸 `endswith("handler.py")` 命中了 **window_flush_handler.py**（9586 B）
    而非根目录的 handler.py（14454 B），于是报出一条**不存在的漂移**。

    同包内以 handler.py 结尾的路径实测有 9 个。假漂移比漏报更危险 ——
    它会更快训练人忽略这个检测（与 #51「永远红着的告警」同一机制）。
    """

    def test_no_bare_endswith_on_member(self, src: str):
        """`_member_from_package` 的**代码**里不得有 `endswith(member)`。

        用 AST 而不是字符串搜索：本文件与检测器的 docstring 都要引用这个
        反例来说明踩过的坑，字符串搜索会把注释当成违规抓出来
        （写这条测试时就先这么失败了一次）。
        """
        import ast

        tree = ast.parse(src)
        fn_node = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_member_from_package"
            ),
            None,
        )
        assert fn_node, "找不到 _member_from_package"

        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "endswith"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "member"
            ):
                pytest.fail(
                    "成员匹配用了裸 endswith(member)。它会把 "
                    "window_flush_handler.py 当成 handler.py —— 实测产生过"
                    "假漂移。退化匹配必须带 '/' 分隔符。"
                )

    def test_exact_match_tried_first(self, src: str):
        """member 就是包内路径时必须直接命中，不走模糊匹配。"""
        assert "if member in names" in src, (
            "缺精确匹配分支 —— 绝大多数成员就是包内根路径，应当直接取"
        )

    def test_separator_required_in_fallback(self, src: str):
        """退化匹配必须带 '/' —— 这是区分 handler.py 与 *_handler.py 的关键。"""
        assert 'endswith("/" + member)' in src, (
            "退化匹配没带 '/' 分隔符。不带的话 foo_handler.py 会被当成 handler.py。"
        )

    def test_ambiguous_match_returns_none_not_a_guess(self, src: str):
        """候选不唯一时必须返回 None（未核实），不能猜一个。

        猜错产生假漂移；返回 None 会让报告显示「未核实」—— 后者是诚实的，
        前者会让人不再相信这个检测。
        """
        assert "len(cands) == 1" in src, (
            "没有「候选唯一才取」的判断 —— 多候选时猜一个会产生假漂移"
        )
        assert_contains(src, "而**不猜**")


# ─────────────────────────────────────────────────────────────────────────
# 方向 C：包完整性
#
# 守的是 2026-10-04 我自己造成的那次故障：cdk deploy 把一个**没装依赖的
# 目录**打包上线，gp-window-flush 从 14.6 MB / 2008 条目变成
# 0.29 MB / 36 条目，缺 urllib3。
#
# 这一组存在的理由是：**当时三个判据全部给了绿灯**，其中包括这个检测器
# 自己的方向 B —— 5 个模块逐字比对全部「✓ 一致」，因为业务代码确实一致，
# 少的是 14 MB 依赖。方向 B 比的是模块内容，缺的是模块本身。
#
# 所以这不是「把方向 B 做得更细」能覆盖的，必须是一个独立维度。
# ─────────────────────────────────────────────────────────────────────────


def _load_detector():
    """直接 import 检测器 —— kiro_crew 只在 check() 内部 import，
    所以模块级 import 不需要 cron 运行时在场。

    这组用**行为测试**而非文本断言：方向 C 的价值全在它实际抓不抓到缺
    依赖，而一个只检查注释存在的测试对此一无所知。
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_ld_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ld_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _zip_with(names: list[str]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return buf.getvalue()


def _complete_package_for(mod, fn: str) -> list[str]:
    """造一个刚好满足该函数判据的包。"""
    floor, required = mod.PACKAGE_REQUIRED[fn]
    names = [f"{r}/__init__.py" for r in required]
    names += [f"{required[0]}/pad_{i}.py" for i in range(floor)]
    return names


class TestPackageIntegrityIsADistinctDimension:
    @pytest.fixture(scope="class")
    def mod(self):
        return _load_detector()

    def test_complete_package_reports_nothing(self, mod):
        """完整的包不能报问题 —— 假阳性比漏报更快毁掉一个检测。"""
        for fn in mod.PACKAGE_REQUIRED:
            blob = _zip_with(_complete_package_for(mod, fn))
            assert mod._package_integrity(blob, fn) is None, fn

    def test_catches_missing_runtime_dependency(self, mod):
        """缺一个必需顶层项就要报，并且要**指名**缺的是哪个。

        这正是那次故障的形状：业务代码齐全，缺的是 urllib3。
        """
        fn = "gp-window-flush"
        floor, required = mod.PACKAGE_REQUIRED[fn]
        names = [
            n for n in _complete_package_for(mod, fn)
            if not n.startswith("urllib3/")
        ]
        names += [f"{required[1]}/pad_{i}.py" for i in range(floor)]
        res = mod._package_integrity(_zip_with(names), fn)
        assert res is not None, "缺 urllib3 却报了绿灯 —— 正是故障当天的情形"
        assert "urllib3" in res, f"没指名缺什么，无法处置: {res}"

    def test_catches_wholesale_dependency_loss(self, mod):
        """36 条目 vs 2008 条目这种数量级丢失必须被抓到。用故障当天的真实数字。"""
        fn = "gp-window-flush"
        res = mod._package_integrity(
            _zip_with([f"biz/mod_{i}.py" for i in range(36)]), fn
        )
        assert res is not None
        assert "36" in res, f"没说清实际条目数: {res}"

    def test_silent_for_functions_without_dependencies(self, mod):
        """单文件函数刻意不在表里，必须返回 None 而不是报「条目太少」。"""
        for fn in ("neptune-etl-trigger", "neptune-etl-from-xray",
                   "neptune-etl-from-agentcore"):
            assert fn not in mod.PACKAGE_REQUIRED, (
                f"{fn} 是单文件函数，给它写下限等于守一个恒为真的判据"
            )
            assert mod._package_integrity(_zip_with(["a.py"]), fn) is None

    def test_floors_leave_headroom(self, mod):
        """下限必须留余量。

        下限贴着实测值写，依赖一升级就误报；而周期性误报的门禁在这个仓库
        已有结论 —— 4.47 因此移除了队列积压告警。
        """
        observed = {           # 2026-10-04 实测条目数
            "neptune-etl-from-cfn": 144,
            "neptune-etl-from-deepflow": 119,
            "neptune-etl-from-aws": 133,
            "neptune-etl-from-appsignals": 38,
            "gp-window-flush": 2008,
            "petsite-rca-engine": 1982,
        }
        for fn, (floor, _) in mod.PACKAGE_REQUIRED.items():
            assert floor > 0, fn
            assert floor < observed[fn], (
                f"{fn} 下限 {floor} ≥ 实测 {observed[fn]} —— 一上线就误报"
            )
            assert floor >= observed[fn] * 0.5, (
                f"{fn} 下限 {floor} 低于实测的一半，抓不到大规模丢失"
            )

    def test_reuses_the_already_downloaded_package(self, mod, src: str):
        """方向 C 必须复用方向 B 下好的 blob，不许再下一次整包。"""
        assert "_package_integrity(blob, fn)" in src, "方向 C 没有复用方向 B 的 blob"
        for fn in mod.PACKAGE_REQUIRED:
            assert fn in mod.SOURCE_MAP, (
                f"{fn} 在 PACKAGE_REQUIRED 但不在 SOURCE_MAP —— "
                f"方向 C 复用方向 B 的 blob，不在 SOURCE_MAP 就永远不会被检查"
            )

    def test_integrity_result_reaches_the_report(self, src: str):
        """算出来必须报出去。

        这个仓库里「写了但没人读」已有多例（失活 Pod 标记写了 1466 个但
        7 个查询都不过滤；SOURCE_MAP 对 etl_aws 报绿而漏掉 2 个真漂移）。
        一个算出完整性结论却不进报告的方向 C 就是下一例。
        """
        import ast

        tree = ast.parse(src)
        fn_check = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "check"
        )
        used = {
            n.id for n in ast.walk(fn_check)
            if isinstance(n, ast.Name) and n.id == "integrity"
        }
        assert used, "check() 里根本没用到 integrity"
        # integrity 必须**真的来自** _package_integrity ——
        # 反向验证发现：只断言「integrity 出现在 if 里」时，把赋值退化成
        # `integrity = None` 这条测试照样通过。那正是「算了但没人读」的
        # 退化形态，必须钉住赋值的来源而不只是变量的使用。
        assigned_from = [
            n for n in ast.walk(fn_check)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "integrity" for t in n.targets
            )
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", "") == "_package_integrity"
        ]
        assert assigned_from, (
            "integrity 不是由 _package_integrity() 赋值的 —— "
            "被退化成常量的方向 C 会让这个检测静默失效"
        )
        conds = [
            n for n in ast.walk(fn_check)
            if isinstance(n, ast.If)
            and any(
                isinstance(x, ast.Name) and x.id == "integrity"
                for x in ast.walk(n.test)
            )
        ]
        assert conds, "integrity 算了但没有参与「是否报告」的判断"

    def test_records_why_direction_b_cannot_catch_this(self, src: str):
        """必须写下方向 B 结构上抓不到它 —— 否则下一个人会以为
        「把方向 B 做细一点」就够了。"""
        assert_contains(src, "方向 B")
        assert_contains(src, "抓不到")
        assert "2008" in src and "36" in src, "没有留下那次故障的实测数字"
        assert "ModuleNotFoundError" in src


class TestSourceMapCoversEveryGraphWriter:
    """2026-10-04 全量核对：表外还有 4 个函数在写同一张图，**四个全在漂移**。

    守的是覆盖面本身 —— 漏一个函数等于那个函数永远绿。
    """

    @pytest.fixture(scope="class")
    def mod(self):
        return _load_detector()

    @pytest.mark.parametrize("fn", [
        "neptune-etl-from-xray",
        "neptune-etl-from-appsignals",
        "neptune-etl-from-agentcore",
        "petsite-rca-engine",
    ])
    def test_previously_missing_function_is_covered(self, mod, fn: str):
        assert fn in mod.SOURCE_MAP, (
            f"{fn} 实测漂移过，却不在 SOURCE_MAP 里 —— 它会永远报绿"
        )

    def test_shared_source_functions_both_covered(self, mod):
        """petsite-rca-engine 与 gp-window-flush 打的是同一份 rca/ 源码，
        同一个漂移会同时出现在两个函数上。只比一个就只修一半。"""
        a = {m for _, m in mod.SOURCE_MAP["gp-window-flush"]}
        b = {m for _, m in mod.SOURCE_MAP["petsite-rca-engine"]}
        assert "neptune/neptune_queries.py" in (a & b), (
            "两个函数共享的 neptune_queries.py 必须在两边都比 —— "
            "PR #47 的 active 过滤实测同时漂在两个函数上"
        )

    def test_four_for_four_hit_rate_recorded(self, src: str):
        """4/4 命中率要写下来 —— 它是「没有判据看着的地方漂移不会停」的
        实测证据，不是一句口号。"""
        assert "四个全在漂移" in src
        assert "不会自己停下" in src

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

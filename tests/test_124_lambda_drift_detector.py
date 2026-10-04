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
        """那个 Node Lambda 刻意不在表里 —— 它线上是打包产物。"""
        import ast

        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "SOURCE_MAP" for t in node.targets
            ):
                vals = ast.literal_eval(node.value)
                assert vals, "SOURCE_MAP 不能是空的"
                for _path, member in vals.values():
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

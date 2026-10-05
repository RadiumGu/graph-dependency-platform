"""test_127_bypass_survey_self_check.py — 前置条件必须由脚本自己守，且要能安静下来。

## 守什么

1. **前置条件不能写在正文里交给人。** 第一版只在报告里写「⚠️ 删除 prio 3
   之前必须先确认旁路目标本身是活的」，然后靠我每次手工查 ——
   那意味着这个前置条件从来不在脚本的保证范围内。

2. **查询失败也算前置条件不成立。** 不能把「查不到」默认成「健康」，
   否则 Skip 逻辑会把「服务停了所以没人用」误当成「没人用」。

3. **必须能安静下来，但只在情况没变时。** 有人用了旁路 / 目标不健康 /
   有解析失败，三者任一都要重报。

4. **这次是提前用掉教训，不是事后补救。** 前两次（队列告警 4.47、
   漂移检测器 4.51）都是响过之后才修。

5. 一个我没采信的数字：用两次非原子的读做减法得出的「孤立数」。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
SCRIPT = ROOT / "crons" / "petsite_watch.py"


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.53")
    return txt[start : txt.index("## 六、待记录", start)]


class TestPreconditionIsEnforcedByTheScript:
    def test_target_groups_declared(self, src: str):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "BYPASS_TARGET_GROUPS" for t in node.targets
            ):
                vals = ast.literal_eval(node.value)
                assert len(vals) >= 2, "两个旁路目标组都要查"
                return
        pytest.fail("找不到 BYPASS_TARGET_GROUPS")

    def test_health_is_actually_queried(self, src: str):
        assert "describe_target_health" in src

    def test_query_failure_counts_as_precondition_unmet(self, src: str):
        """不能把「查不到」默认成「健康」。"""
        assert "不默认它健康" in src
        assert "查询失败：" in src

    def test_no_longer_delegates_the_check_to_a_human(self, src: str):
        """原来那句「删除 prio 3 之前必须先确认」已不该再是唯一保障。"""
        assert "把这件事交给人" in src, "必须把这个教训写在脚本里"


class TestMustBeAbleToGoQuiet:
    def test_skips_when_nothing_changed(self, src: str):
        assert "旁路使用面无变化" in src

    def test_three_reasons_to_speak(self, src: str):
        assert "有人真的用了旁路" in src
        assert "旁路目标变得不健康" in src
        assert "有解析失败" in src

    def test_changed_covers_all_three(self, src: str):
        assert "changed = bool(bypass_total or unhealthy or unparsed)" in src

    def test_streak_is_persisted(self, src: str):
        """安静之后证据仍要可查。"""
        assert "STREAK_FILE" in src
        assert "连续 {streak} 次 0 条" in src

    def test_streak_resets_when_something_changes(self, src: str):
        assert "_write_streak(0)" in src

    def test_references_the_earlier_lessons(self, src: str):
        assert "4.47" in src and "4.51" in src


class TestAppliedEarlyNotAfterTheFact:
    def test_recorded_as_preemptive(self, section: str):
        assert_contains(section, "在它变成噪声**之前**动手")

    def test_prior_two_were_reactive(self, section: str):
        assert_contains(section, "都是事后补救")

    def test_why_skipping_without_health_check_is_wrong(self, section: str):
        assert_contains(section, "误当成「没人用」")


class TestNumberIDidNotTrust:
    def test_non_atomic_subtraction_rejected(self, section: str):
        assert_contains(section, "**两次非原子的读")

    def test_same_algorithm_as_yesterdays_wrong_16(self, section: str):
        assert_contains(section, "同一个算法")

    def test_zero_orphans_is_not_proof_race_stopped(self, section: str):
        """15 分钟期望 0.2 次 —— 0 是统计上相符的。"""
        assert_contains(section, "**0 是统计上相符的**")

    def test_reliable_signals_named(self, section: str):
        assert_contains(section, "`orphans_recovered` 的计数")


class TestHealingHeld:
    def test_eighteen_hour_series_recorded(self, section: str):
        assert_contains(section, "前 1/3 均值 96.6%")

    def test_criterion_for_decline_is_stated(self, section: str):
        assert_contains(section, "后 1/3 明显低于前 1/3")

    def test_contrast_with_before(self, section: str):
        assert_contains(section, "86.2% → 47.0%")

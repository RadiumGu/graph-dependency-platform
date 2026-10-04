"""test_125_drift_detector_must_go_quiet.py — 周期性检查必须能安静下来。

## 为什么需要这个门禁

4.50 的漂移检测器上线 20 分钟后第一次触发，报的就是我 20 分钟前刚核实、
并已升级给人决策的那一条。它会**每天报同一条**，直到有人部署那个门禁 ——
而「一条永远不会消失的告警会训练人忽略整个频道」正是 2026-10-02 移除
队列积压告警的理由（4.47）。48 小时内差点第二次踩同一个坑。

所以守三件事：

1. **确认必须绑在 CodeSha256 上，不能绑在函数名上。** 绑函数名就是永久静音，
   那和删掉检测没区别 —— 门禁部署后它也不会再告诉你「情况变了」。
2. **已被方向 B 逐字核实过的，方向 A 不再单列。** 否则 4 条噪声天天出现。
3. **没有新发现时必须 Skip。** 不发无事通知，也不重报已确认项。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
SCRIPT = ROOT / "crons" / "lambda_drift.py"


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.51")
    return txt[start : txt.index("## 六、待记录", start)]


class TestAcknowledgementIsBoundToCodeSha:
    def test_acknowledged_table_exists(self, src: str):
        assert "ACKNOWLEDGED" in src

    def test_each_entry_carries_a_sha_and_a_reason(self, src: str):
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "ACKNOWLEDGED" for t in node.targets
            ):
                vals = ast.literal_eval(node.value)
                assert vals, "ACKNOWLEDGED 不能为空（否则这个机制没被用上）"
                for fn, entry in vals.items():
                    assert len(entry) == 2, f"{fn} 缺 sha 或原因"
                    sha, reason = entry
                    # base64 的 CodeSha256 是 44 字符、以 = 结尾
                    assert sha.endswith("="), f"{fn} 的确认不像 CodeSha256：{sha!r}"
                    assert len(reason) > 8, f"{fn} 的确认没写原因"
                return
        pytest.fail("找不到 ACKNOWLEDGED")

    def test_sha_mismatch_invalidates_the_acknowledgement(self, src: str):
        """绑函数名就是永久静音 —— 必须比对 sha。"""
        assert "shas.get(fn) == ack[0]" in src

    def test_redeploy_is_called_out_in_the_report(self, src: str):
        assert "确认失效" in src

    def test_why_not_bound_to_name_is_documented(self, src: str):
        assert "不是绑在函数名上" in src or "不绑在函数名" in src


class TestDirectionAIsDeduplicated:
    def test_source_map_covered_entries_are_dropped(self, src: str):
        assert "fn not in SOURCE_MAP" in src

    def test_remaining_entries_admit_they_are_unverified(self, src: str):
        assert "未经内容核实" in src

    def test_tells_reader_what_to_do_with_them(self, src: str):
        assert "要么加进 SOURCE_MAP，要么人工核一次" in src


class TestMustBeAbleToGoQuiet:
    def test_skips_when_nothing_fresh(self, src: str):
        assert "没有新漂移" in src

    def test_no_all_clear_notification(self, src: str):
        assert "刻意不发无事通知" in src

    def test_references_the_alarm_lesson(self, src: str):
        """这个机制存在的理由就是 4.47 那条教训。"""
        assert "4.47" in src


class TestTheLessonAboutMyself:
    def test_two_cases_are_distinguished(self, section: str):
        """永久架构属性 vs 真实可修的待办 —— 处置不同。"""
        assert_contains(section, "alarm 的是**永久架构属性**")
        assert_contains(section, "已升级待决策的待办")

    def test_writing_a_lesson_is_not_applying_it(self, section: str):
        assert_contains(section, "**写下教训不等于应用教训**")

    def test_new_recurring_check_must_answer_when_it_stops(self, section: str):
        assert_contains(section, "它什么时候会停")

    def test_three_paths_verified(self, section: str):
        assert_contains(section, "模拟部署")

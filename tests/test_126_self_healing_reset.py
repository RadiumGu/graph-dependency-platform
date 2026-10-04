"""test_126_self_healing_reset.py — 守住「重置的判据是可用性，不是交易行」。

## 守什么

1. **判据必须是 availability='no'，不能退回「有没有交易行」。**
   后者有一个按主键删也闭合不了的竞态：CompleteAdoption 先建交易行
   （service.go:99）后置可用性（:106），而 cleanup 约每 8 秒一次、比领养
   还频繁，于是它落在两行之间**合法地**删掉那行，随后宠物才变 'no'，
   此后在 transactions 里不留痕迹。

2. **扫描失败只降级、不中断重置。** 安全网失效不该连坐主路径。

3. **两次竞态要能被区分。** 下一个人看到「又是重置竞态」会以为已修过。

4. 我自己踩的两个坑：键在两处拼且顺序不一致（去重静默失效）；
   「只在 >0 时记日志」让信号缺失变成歧义。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.52")
    return txt[start : txt.index("## 六、待记录", start)]


class TestCriterionIsAvailability:
    def test_criterion_recorded(self, section: str):
        assert_contains(section, "**判据刻意是「availability='no'」而不是「有没有交易行」**")

    def test_why_availability_is_authoritative(self, section: str):
        assert_contains(section, "交易行只是它的一个副产品")

    def test_any_future_orphan_path_self_corrects(self, section: str):
        assert_contains(section, "包括将来新引入的")


class TestTwoRacesMustBeDistinguishable:
    def test_not_the_same_race(self, section: str):
        """下一个人看到「又是重置竞态」会以为已修过。"""
        assert_contains(section, "与 `a01fa93d` 修的不是同一个竞态")

    def test_first_race_was_newer_rows(self, section: str):
        assert_contains(section, "新产生**的\n行")

    def test_second_race_row_was_legitimately_observed(self, section: str):
        assert_contains(section, "**是被正当观察到的**")

    def test_cleanup_is_more_frequent_than_adoption(self, section: str):
        """这是窗口为什么常被命中的数量依据。"""
        assert_contains(section, "比领养（20 次/3 分钟）还频繁")

    def test_chasing_windows_lost_twice(self, section: str):
        assert_contains(section, "追逐单个窗口已经输了两次")


class TestSafetyNetMustNotBreakMainPath:
    def test_degrade_not_abort(self, section: str):
        assert_contains(section, "**扫描失败刻意不中断重置**")

    def test_failure_is_loud(self, section: str):
        assert_contains(section, "响亮记错误")

    def test_iam_checked_before_deploy(self, section: str):
        """没有 Scan 权限的话修复会静默降级 —— 必须部署前核实。"""
        assert_contains(section, "部署前就核实过")

    def test_no_new_dependency(self, section: str):
        assert_contains(section, "**不新增依赖**")

    def test_interface_endpoint_handling_matched(self, section: str):
        assert_contains(section, "走公网而超时")


class TestHealingNeededNoManualRestore:
    def test_contrast_with_previous_fix(self, section: str):
        assert_contains(section, "停止流血 ≠ 补回")

    def test_backlog_cleared_in_first_cycle(self, section: str):
        assert_contains(section, "上线后第一个周期")

    def test_evidence_recorded(self, section: str):
        assert_contains(section, "orphans_recovered  count=15")
        assert_contains(section, "5 no / 21 yes")


class TestMyOwnTwoMistakes:
    def test_key_built_in_two_places(self, section: str):
        assert_contains(section, "去重会**静默失效**")

    def test_key_now_has_one_source(self, section: str):
        assert_contains(section, "键只在 `petKey()` 一处拼")

    def test_conditional_log_made_absence_ambiguous(self, section: str):
        assert_contains(section, "让信号缺失变成歧义")

    def test_verification_marker_must_be_unconditional(self, section: str):
        assert_contains(section, "**验证标记应当无条件记录**")


class TestABrokenParseLooksLikeARealNumber:
    def test_the_wrong_16_recorded(self, section: str):
        """解析失败产出的数字与真数字长得一模一样。"""
        assert_contains(section, "一个解析失败产出的数字看起来和真数字一模一样")

    def test_how_it_was_fixed(self, section: str):
        assert_contains(section, "raw_decode()")

"""test_119_housekeeping_restock.py — 「失败长得像成功」第三次,以及收窄的前置件。

## 守什么

1. **同一个故障族已经出现三次**：HTTP 200 + 零异常 + 什么都没做。
   - 4.40 upsert 造出残缺行（接口返回 200）
   - 4.42 领养假成功（页面显示 Adoption Complete）
   - 4.45 本次：`/housekeeping/` 302 跟随后变成 200
   共同点都是**调用方不检查结果**。这条归纳必须留在记录里，
   否则第四次还会以新形态出现。

2. **告警连续响了 5 天没人看** —— 建告警不等于有人响应。
   这句话比那个告警本身更重要。

3. **枚举不出消费者时不能收窄** —— 09-26 按出口 IP 收窄网关失败，
   10-02 又在 ALB 旁路上遇到同一件事（访问日志是关的）。
   门禁守住「先开日志再收窄」这个次序，别让下一个人跳过前置件。

4. 我 09-27 那次误判的订正（手工修复掩盖了真正的变量）。
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
    start = txt.index("### 4.45")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheThirdTimeSameFamily:
    def test_redirect_looked_like_success(self, section: str):
        assert_contains(section, "**HttpClient 默认跟随重定向**")

    def test_family_is_named_as_third_occurrence(self, section: str):
        """不是「又一个 bug」，是同一个族的第三次。"""
        assert_contains(section, "第三次遇到**同一个故障族**")

    def test_common_cause_is_unchecked_result(self, section: str):
        assert_contains(section, "**调用方不检查结果**")

    def test_fix_makes_failure_loud(self, section: str):
        assert_contains(section, "让下一次断掉是响亮的")


class TestAnAlarmNobodyWatched:
    def test_five_days_in_alarm(self, section: str):
        assert_contains(section, "**连续 5 天**")

    def test_alarm_without_a_responder(self, section: str):
        """这句比告警本身更重要。"""
        assert_contains(section, "**告警是对的,没人去看。**")


class TestMyOwn0927Misjudgement:
    def test_root_cause_was_not_orphaning(self, section: str):
        assert_contains(section, "**不是这次的主因**")

    def test_manual_repair_masked_the_variable(self, section: str):
        """手工修复掩盖了真正的变量 —— 这是方法论层面的教训。"""
        assert_contains(section, "**手工修复掩盖了真正的变量**")

    def test_orphaning_downgraded_not_dismissed(self, section: str):
        """降级为次要，但不是不存在 —— 条件写清楚了。"""
        assert_contains(section, "repository.go:173")


class TestNarrowingNeedsEnumerationFirst:
    def test_cannot_narrow_without_enumerating(self, section: str):
        assert_contains(section, "**枚举不出消费者时不能收窄。**")

    def test_access_logs_were_off(self, section: str):
        assert_contains(section, "access_logs.s3.enabled = false")

    def test_prio3_scope_is_stated(self, section: str):
        """prio 3 给的是对 petsite 本体的免认证访问，不是某个工具。"""
        assert_contains(section, "免认证访问")

    def test_logging_enabled_as_prerequisite(self, section: str):
        assert_contains(section, "openclaw-alb-logs-1770913299")

    def test_wait_for_enough_log_window(self, section: str):
        """4 分钟样本看不见低频调用方 —— 这条在 09-26 已经学过一次。"""
        assert_contains(section, "最好跨整点与日切")


class TestGraphLoopClosed:
    def test_aurora_edge_now_exists(self, section: str):
        assert_contains(section, "payforadoption-api-go → postgres")

    def test_neptune_side_admitted_unverified(self, section: str):
        """X-Ray 有边 ≠ 图里有边。没核实的部分要明说。"""
        assert_contains(section, "**Neptune 侧是否已摄取尚未核实**")


class TestMeasuredEffect:
    def test_before_and_after_recorded(self, section: str):
        assert_contains(section, "attempts=184 persisted=98 ratio=53.26%")

    def test_canary_was_doing_the_restocking(self, section: str):
        """以前还能看到宠物，是金丝雀在替它补货 —— 解释了为什么没早点暴露。"""
        assert_contains(section, "cwsyn-petsite-e2e-canary")

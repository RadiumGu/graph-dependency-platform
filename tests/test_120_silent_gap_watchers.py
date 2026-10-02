"""test_120_silent_gap_watchers.py — 把「没人看着」变成被监控，并守住判据选择。

## 守什么

1. **队列积压的判据是年龄，不是条数。** 队列有保留期，超期消息被静默丢弃，
   所以条数会自己「恢复」而积压从未被处理 —— 用条数做告警会在最需要它的
   时候自己变绿。这个判据选择比告警本身更容易被改错。

2. **是我的修复让缺失的消费者显形的。** 07:35 之前队列是空的，不是因为
   被消费了，而是因为领养 5 天没成功、没有生产者。这个因果顺序如果丢了，
   下一个人会以为「队列积压是新引入的问题」而回滚我的修复。

3. **不发无事通知。** 无事通知会让人学会忽略频道，那正好毁掉看护任务本身。

4. **ALB 日志不记请求头** —— 所以旁路枚举只能靠 matched_rule_priority。
   这条约束不写下来，下一个人会去日志里搜那个头然后得出「没人用」的错误结论。

5. 预演抓到的两个真错误（导入路径、明文测试文件）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
SLO = ROOT / "infra" / "tokyo" / "04-adoption-outcome-slo.yaml"
SCRIPT = ROOT / "crons" / "petsite_watch.py"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.46")
    return txt[start : txt.index("## 六、待记录", start)]


class TestQueueCriterionIsAgeNotCount:
    """⚠️ 这个类在 2026-10-02 14:30 被改过 —— 见 4.47。

    原来三条断言「模板里有一条队列年龄告警」。那条告警当天就被移除了
    （队列在 CDK 设计上就是只写的，告警永远回不到 OK），所以那三条断言
    守的是一个已被推翻的要求，门禁因此红了 —— **门禁在正确工作**。

    但「年龄比条数可靠」这个判断**本身仍然成立**，只是不再适用于
    一条活着的告警。所以改成守知识留档，而不是守告警存在。
    由 test_121 负责守「那条告警不能被加回来」。
    """

    def test_age_vs_count_knowledge_is_preserved(self):
        """判断仍要留着：若将来真部署了消费者，这个判据选择会再次相关。"""
        tpl = SLO.read_text(encoding="utf-8")
        assert "ApproximateAgeOfOldestMessage" in tpl, (
            "年龄 vs 条数的判断应作为注释留在模板里，供将来真有消费者时参考"
        )

    def test_why_count_would_lie_is_recorded(self, section: str):
        # ⚠️ 片段必须**不跨行**。我第一版写了一个跨越换行的长句并用
        #    .replace("\n","") 凑，结果断言匹配不到 —— 「断言跨行匹配不到」
        #    这个失误族的又一次。照抄真实行里的片段。
        assert_contains(section, "而**不是消息条数**")
        assert_contains(section, "条数是个会骗人的指标")

    def test_record_points_to_its_own_correction(self, section: str):
        """4.46 说"加了这条告警"，若不指向 4.47 这份 runbook 就在说谎。"""
        assert_contains(section, "4.47")


class TestCausalOrderMustNotBeLost:
    def test_queue_was_empty_for_lack_of_producer(self, section: str):
        assert_contains(section, "不是因为被消费了,而是因为没有生产者")

    def test_fix_revealed_the_missing_consumer(self, section: str):
        assert_contains(section, "**修好领养链路才让这个缺失的消费者显形。**")

    def test_consumer_gap_is_a_prior_decision(self, section: str):
        """2026-08-29 刻意不补 —— 本次不改变那个决定。"""
        assert_contains(section, "刻意不补的架构决定")


class TestWatchdogDesign:
    def test_cloudwatch_cannot_express_duration(self, section: str):
        assert_contains(section, "**没有「已持续 N 小时」这种条件**")

    def test_no_all_clear_notifications(self, section: str):
        assert_contains(section, "无事通知会让人学会忽略这个频道")

    def test_script_skips_when_nothing_stale(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert "Skip(" in src and "没有持续超时的告警" in src

    def test_script_imports_from_cron_script(self):
        """预演抓到的错误：正确路径是 kiro_crew.cron_script。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "from kiro_crew.cron_script import" in src
        assert "cron_api" not in src


class TestBypassSurveyConstraints:
    def test_alb_logs_do_not_carry_headers(self, section: str):
        assert_contains(section, "**ALB 访问日志不记请求头**")

    def test_criterion_is_matched_rule_priority(self, section: str):
        assert_contains(section, "matched_rule_priority")

    def test_script_filters_non_gzip_test_file(self):
        """ALB 开启日志时写的明文 ELBAccessLogTestFile 会让 gzip 抛异常。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "ELBAccessLogTestFile" in src
        assert '.log.gz' in src

    def test_waf_discovery_recorded(self, section: str):
        """actions_executed 里的 waf 是此前不知道的事实。"""
        assert_contains(section, "**前面有 WAF**")

    def test_internet_scanning_recorded(self, section: str):
        assert_contains(section, "开放代理探测")


class TestPreviewBeforeRegistering:
    def test_lesson_recorded(self, section: str):
        assert_contains(section, "**注册前先预演**")

"""test_121_alarms_must_be_actionable.py — 守住「告警必须可处置」。

## 为什么需要这个门禁

2026-10-02 我在 08:20 给领养历史队列加了一条告警，盯最老消息年龄。
它正确触发了，又被 alarm-watchdog 正确捞了出来 —— 然后我才发现
**那条告警本身是错的**：队列在 CDK 设计上就是只写的，积压是永久架构属性，
告警永远回不到 OK。

一条永远红着的告警会训练人忽略整个频道，而那恰好是
adoption-success-ratio 响 5 天没人看的机制。所以这个门禁守两件事：

1. 那条告警**不能被重新加回来**（下一个人会很容易觉得"积压该告警"）。
2. 判断它错的那串证据必须留着，否则重新发现一遍要花同样的时间。
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
    start = txt.index("### 4.47")
    return txt[start : txt.index("## 六、待记录", start)]


@pytest.fixture(scope="module")
def template() -> str:
    return SLO.read_text(encoding="utf-8")


class TestAlarmStaysRemoved:
    def test_no_queue_age_alarm_resource(self, template: str):
        assert "AdoptionHistoryQueueStaleAlarm" not in template
        assert "petadoptions-history-queue-not-consumed" not in (
            # 名字只应出现在解释性注释里，不应是一个 AlarmName 属性
            "\n".join(
                l for l in template.splitlines() if l.strip().startswith("AlarmName:")
            )
        )

    def test_age_metric_not_used_as_an_alarm(self, template: str):
        """ApproximateAgeOfOldestMessage 不应再作为 MetricName 出现。"""
        metric_lines = [
            l for l in template.splitlines() if l.strip().startswith("MetricName:")
        ]
        assert not any("ApproximateAgeOfOldestMessage" in l for l in metric_lines)

    def test_do_not_re_add_warning_present(self, template: str):
        assert "不要重新加回来" in template

    def test_right_alarm_if_consumer_ever_ships(self, template: str):
        """留下正确答案，否则下一个人只知道"别加"不知道"该加什么"。"""
        assert "DLQ 深度告警" in template

    def test_watchdog_no_longer_watches_it(self):
        src = SCRIPT.read_text(encoding="utf-8")
        watched = src[src.index("WATCHED_ALARMS") : src.index("STALE_HOURS")]
        assert "petadoptions-history-queue-not-consumed" not in watched.replace(
            "# petadoptions-history-queue-not-consumed 已移除 ——", ""
        )


class TestEvidenceThatTheQueueIsWriteOnly:
    def test_cdk_usages_recorded(self, section: str):
        assert_contains(section, "只有两处用法")
        assert_contains(section, "grantConsumeMessages")

    def test_dlq_is_decorative(self, section: str):
        """我最初把 DLQ 的存在误读成"按有消费者设计"的证据。"""
        assert_contains(section, "是**装饰**")
        assert_contains(section, "**那是误读**")

    def test_history_service_never_inserts(self, section: str):
        assert_contains(section, "**从不 INSERT**")

    def test_upstream_has_none_either(self, section: str):
        assert_contains(section, "也没有任何 SQS 消费者")


class TestTheLessonItself:
    def test_forever_red_trains_people_to_ignore(self, section: str):
        assert_contains(section, "会训练人忽略整个频道")

    def test_alarm_must_answer_what_to_do(self, section: str):
        assert_contains(section, "收到的人该做什么")

    def test_todo_belongs_in_docs_not_pager(self, section: str):
        assert_contains(section, "待办属于文档,不属于寻呼机")

    def test_the_contradiction_is_named(self, section: str):
        """决定不补 + 为它建告警 = 互相矛盾，这是根因。"""
        assert_contains(section, "决定不补和为它建告警是互相矛盾的")

    def test_visibility_belongs_in_docs(self, section: str):
        assert_contains(section, "**可见性的正确载体是文档,不是告警**")


class TestWatchdogItselfIsValidated:
    def test_watchdog_caught_it_as_designed(self, section: str):
        assert_contains(section, "**这个机制本身是有效的**")

    def test_gap_kept_as_known_limitation(self, section: str):
        assert_contains(section, "留给人定")

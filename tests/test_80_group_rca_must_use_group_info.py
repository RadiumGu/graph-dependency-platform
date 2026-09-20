"""组内告警必须真的进入 RCA，且不得破坏「谁最早出错」的判定。

## 这组门禁守的是什么

`window_flush_handler.py:138` 从第一天就在调 `rca_engine.analyze_group()`，
而它**从来不存在** —— 被 except 接住、降级成 `analyze(根因服务)`，
组级信息完全不进 RCA。2026-09-20 补实现。

实现时踩到的三个真实陷阱，每个都有对应门禁：

1. **不能 append**：`step4_score` 用 `error_services[0]['service']` 认定
   「最早出错的服务」并给 +40 分（单项最大权重），而 step1 的 SQL 带
   `ORDER BY first_error ASC` —— 顺序是语义的一部分。append 到末尾会让
   组内服务永远拿不到那 40 分；insert 到开头又会凭空把 40 分塞给它。

2. **时间格式不同会静默失效**：DeepFlow 给 `'2026-09-20 17:00:00'`（naive），
   告警给 `'2026-09-20T17:00:00Z'`（aware），直接比较抛 TypeError。
   而 `step3b_temporal_validation` 的调用点包着 try/except，
   抛了会被吞成 `{}`，时序校验无声失效。

3. **向后兼容**：`petsite-rca-engine` 的 handler.py 调的是两参数版 `analyze()`，
   不传 extra 时行为必须与加参数之前完全一致。
"""
import sys
import pathlib
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RCA = ROOT / "rca"
for p in (str(RCA), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _Alert:
    def __init__(self, service_name, start_time="", severity="P2"):
        self.service_name = service_name
        self.start_time = start_time
        self.severity = severity
        self.raw = {}


# ─────────────────────────────────────────────────────────────
# T80-01: 函数必须存在
# ─────────────────────────────────────────────────────────────

def test_t80_01_analyze_group必须存在且可调用():
    """handler 在调它。缺失会被 except 静默降级，不会让任何测试变红。"""
    from core import rca_engine

    assert hasattr(rca_engine, "analyze_group"), (
        "rca_engine.analyze_group 不存在。window_flush_handler.py:138 在调它，"
        "缺失会被 except 降级成 analyze(根因服务)，组级信息完全不进 RCA —— "
        "而 groups_failed 仍是 0，所以不会有任何信号。"
    )
    assert callable(rca_engine.analyze_group)


# ─────────────────────────────────────────────────────────────
# T80-02 ~ 04: 合并语义 —— 按时间重排，不是 append
# ─────────────────────────────────────────────────────────────

def test_t80_02_组内更早的服务必须排到最前():
    """组内告警若比 DeepFlow 观测更早，必须排到首位（才拿得到 step4 的 +40）。

    这条直接否掉「append 到末尾」这种实现：那样组内服务永远排在后面，
    即使它确实最早出错。
    """
    from core.rca_engine import _merge_error_services

    primary = [{"service": "petsite", "first_error": "2026-09-20 17:05:00",
                "error_count": 9, "error_rate_pct": 30}]
    extra = [{"service": "petfood", "first_error": "2026-09-20T17:01:00Z",
              "error_count": 0, "error_rate_pct": 0, "source": "event_group_alert"}]

    out = _merge_error_services(primary, extra)

    assert out[0]["service"] == "petfood", (
        f"组内告警 17:01 早于 DeepFlow 观测 17:05，必须排首位，实得 {[s['service'] for s in out]}。"
        "若实现是 append，这里会是 petsite。"
    )


def test_t80_03_组内更晚的服务不得抢占首位():
    """反方向：组内告警更晚时，不得把 DeepFlow 找到的最早服务挤下去。

    这条否掉「insert 到开头」这种实现 —— 那会凭空把 +40 分塞给组内服务。
    """
    from core.rca_engine import _merge_error_services

    primary = [{"service": "petsite", "first_error": "2026-09-20 17:00:00",
                "error_count": 9, "error_rate_pct": 30}]
    extra = [{"service": "petfood", "first_error": "2026-09-20T17:09:00Z",
              "error_count": 0, "error_rate_pct": 0}]

    out = _merge_error_services(primary, extra)

    assert out[0]["service"] == "petsite", (
        "DeepFlow 观测到的 17:00 早于组内告警 17:09，首位必须仍是 petsite。"
        "若实现是 insert(0)，这里会被 petfood 抢占，凭空获得 step4 的 +40 分。"
    )


def test_t80_04_跨格式时间必须可比不得抛异常():
    """naive 与 aware 混排不得抛 TypeError。

    DeepFlow 给 '2026-09-20 17:00:00'（fromisoformat → naive），
    告警给 '2026-09-20T17:00:00Z'（→ aware）。直接比较会抛：
        TypeError: can't compare offset-naive and offset-aware datetimes
    而且纯字符串比较同样错 —— 空格(32) < 'T'(84)，
    ClickHouse 格式会系统性地"总是更早"。
    """
    from core.rca_engine import _merge_error_services

    primary = [{"service": "a", "first_error": "2026-09-20 18:00:00"}]
    extra = [{"service": "b", "first_error": "2026-09-20T17:00:00Z"}]

    out = _merge_error_services(primary, extra)  # 不抛即通过

    assert out[0]["service"] == "b", (
        "17:00(aware) 早于 18:00(naive)，b 应在前。"
        "若用字符串比较，'2026-09-20 18' < '2026-09-20T17'（空格<T），会错判成 a 更早。"
    )


def test_t80_05_时间无法解析的条目不得被丢弃():
    """解析失败的条目排到末尾，但必须保留 —— 丢弃会让少一个候选无声发生。"""
    from core.rca_engine import _merge_error_services

    primary = [{"service": "a", "first_error": "not-a-time"}]
    extra = [{"service": "b", "first_error": "2026-09-20T17:00:00Z"}]

    out = _merge_error_services(primary, extra)

    assert {s["service"] for s in out} == {"a", "b"}, "无法解析时间的条目不得被丢弃"
    assert out[0]["service"] == "b", "能解析的排前，不能解析的排后"


def test_t80_06_同名服务保留DeepFlow那份():
    """两边都有同一服务时保留 primary —— 它带真实 error_count/error_rate。"""
    from core.rca_engine import _merge_error_services

    primary = [{"service": "petsite", "first_error": "2026-09-20 17:00:00",
                "error_count": 42, "error_rate_pct": 88}]
    extra = [{"service": "petsite", "first_error": "2026-09-20T17:00:00Z",
              "error_count": 0, "error_rate_pct": 0, "source": "event_group_alert"}]

    out = _merge_error_services(primary, extra)

    assert len(out) == 1, "同名服务不得重复"
    assert out[0]["error_count"] == 42, (
        "必须保留 DeepFlow 那份的真实 error_count，"
        "而不是被告警构造的 0 覆盖（0 会让它在 step4 打分时吃亏）"
    )


# ─────────────────────────────────────────────────────────────
# T80-07: 向后兼容 —— 不传 extra 时行为不变
# ─────────────────────────────────────────────────────────────

def test_t80_07_不传extra时不改动error_services():
    """`petsite-rca-engine` 的 handler.py 调两参数版 analyze()，行为必须不变。

    用 mock 拦住 step1 之后的所有外部调用，只观察 error_services 是否被动过。
    """
    from core import rca_engine

    step1_out = [{"service": "petsite", "first_error": "2026-09-20 17:00:00",
                  "error_count": 5, "error_rate_pct": 20}]
    captured = {}

    def fake_step3(affected, error_services):
        captured["seen"] = list(error_services)
        return []

    with mock.patch.object(rca_engine, "step1_deepflow_errors", return_value=list(step1_out)), \
         mock.patch.object(rca_engine, "step1b_deepflow_l4_errors", return_value=[]), \
         mock.patch.object(rca_engine, "step2_cloudtrail_changes", return_value=[]), \
         mock.patch.object(rca_engine, "step3_graph_candidates", side_effect=fake_step3), \
         mock.patch.object(rca_engine, "step3b_temporal_validation", return_value={}), \
         mock.patch.object(rca_engine, "step4_score", return_value=[]), \
         mock.patch.object(rca_engine, "step3c_log_sampling", return_value={}), \
         mock.patch("engines.factory.make_layer2_engine", side_effect=RuntimeError("skip")):
        rca_engine.analyze("petsite", {})

    assert captured["seen"] == step1_out, (
        "不传 extra_error_services 时，step3 看到的 error_services 必须与 step1 输出"
        f"逐字相同。实得 {captured['seen']}"
    )


def test_t80_08_传extra时组内服务必须到达step3(): 
    """组级信息必须真的流到图谱候选查询 —— 这是 analyze_group 唯一的实质增量。

    背景：`analyze()` 只读 classification 的 signal / affected_capabilities，
    所以光把组级字段塞进 classification 是**不起作用**的。
    组信息必须走 error_services 这条路才能影响判定。
    """
    from core import rca_engine
    from core.topology_correlator import EventGroup

    captured = {}

    def fake_step3(affected, error_services):
        captured["services"] = [s["service"] for s in error_services]
        return []

    g = EventGroup(
        root_candidate_service="petsite",
        root_candidate_alert=_Alert("petsite", "2026-09-20T17:05:00Z"),
        evidence_alerts=[_Alert("petfood", "2026-09-20T17:01:00Z")],
    )

    with mock.patch.object(rca_engine, "step1_deepflow_errors", return_value=[]), \
         mock.patch.object(rca_engine, "step1b_deepflow_l4_errors", return_value=[]), \
         mock.patch.object(rca_engine, "step2_cloudtrail_changes", return_value=[]), \
         mock.patch.object(rca_engine, "step3_graph_candidates", side_effect=fake_step3), \
         mock.patch.object(rca_engine, "step3b_temporal_validation", return_value={}), \
         mock.patch.object(rca_engine, "step4_score", return_value=[]), \
         mock.patch.object(rca_engine, "step3c_log_sampling", return_value={}), \
         mock.patch("engines.factory.make_layer2_engine", side_effect=RuntimeError("skip")):
        out = rca_engine.analyze_group(g, {})

    assert "petfood" in captured["services"], (
        f"组内证据告警的服务必须进入 step3 的图谱候选查询，实得 {captured['services']}"
    )
    assert "petsite" in captured["services"], "根因告警服务也必须在"
    assert captured["services"][0] == "petfood", (
        "petfood 告警 17:01 早于 petsite 17:05，应排首位以便 step4 给它 +40"
    )
    # 组级元信息必须带出，供审计
    assert out["group_id"] == g.group_id
    assert out["group_service_count"] == 2
    assert set(out["group_evidence_services"]) == {"petsite", "petfood"}


def test_t80_09_组内告警条目必须带审计来源标记():
    """告警带进来的候选必须可辨识 —— 排查时要能区分观测与告警。"""
    from core import rca_engine
    from core.topology_correlator import EventGroup

    captured = {}

    def fake_step3(affected, error_services):
        captured["items"] = list(error_services)
        return []

    g = EventGroup(
        root_candidate_service="petsite",
        root_candidate_alert=_Alert("petsite", "2026-09-20T17:00:00Z"),
    )

    with mock.patch.object(rca_engine, "step1_deepflow_errors", return_value=[]), \
         mock.patch.object(rca_engine, "step1b_deepflow_l4_errors", return_value=[]), \
         mock.patch.object(rca_engine, "step2_cloudtrail_changes", return_value=[]), \
         mock.patch.object(rca_engine, "step3_graph_candidates", side_effect=fake_step3), \
         mock.patch.object(rca_engine, "step3b_temporal_validation", return_value={}), \
         mock.patch.object(rca_engine, "step4_score", return_value=[]), \
         mock.patch.object(rca_engine, "step3c_log_sampling", return_value={}), \
         mock.patch("engines.factory.make_layer2_engine", side_effect=RuntimeError("skip")):
        rca_engine.analyze_group(g, {})

    item = captured["items"][0]
    assert item.get("source") == "event_group_alert", (
        "告警构造的 error_services 条目必须带 source 标记，否则排查时无法区分"
        "「DeepFlow 真实观测到的错误」和「告警带进来的候选」"
    )
    assert item["error_count"] == 0, (
        "告警只说明异常、不提供错误量，不得凭空编造 error_count 去参与打分"
    )

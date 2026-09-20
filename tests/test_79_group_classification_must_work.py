"""按 EventGroup 分类必须真的生效，而不是静默降级成单告警分类。

## 这组门禁守的是什么

`window_flush_handler.py` 从第一天就在调 `fault_classifier.classify_group()`，
但那个函数**从来不存在** —— 调用被 `except` 接住、降级成
`classify(根因服务)`，于是「按 EventGroup 分类」这个设计从写下来就没生效过
一次。而 `groups_failed` 始终是 0（有兜底），所以五个月没人发现。

2026-09-20 补实现。这组测试守两件事：
  1. 函数存在且可调用（最低要求，直接挡住"再次静默降级"）
  2. 组级语义真的起作用，且**单调不降** —— 组信息只会让判定更严重

单调不降不是实现偷懒，是有意的安全属性：万一拓扑关联有噪声、把无关告警
并进一组，最坏情况是维持根因判定，不会把一个真 P0 误降成 P1。
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
    """最小 UnifiedAlertEvent 替身：只需 service_name / severity / raw。"""

    def __init__(self, service_name, severity="P2", raw=None):
        self.service_name = service_name
        self.severity = severity
        self.raw = raw or {}


def _group(root_svc="petsite", alerts=None, **kw):
    from core.topology_correlator import EventGroup

    alerts = alerts or []
    root = alerts[0] if alerts else _Alert(root_svc)
    return EventGroup(
        root_candidate_service=root_svc,
        root_candidate_alert=root,
        evidence_alerts=alerts[1:] if len(alerts) > 1 else [],
        **kw,
    )


@pytest.fixture
def fc_graph_stub():
    """把 fault_classifier 的两次图谱查询换成可控替身。

    `classify()` 内部查 q4_service_info（服务优先级）+ q1_blast_radius
    （受影响能力/服务），两者决定基底 severity。测试要控制基底，
    才能单独观察组级修正的效果。
    """
    from core import fault_classifier as fc

    def _install(priority="Tier2", capabilities=None, services=None):
        caps = capabilities if capabilities is not None else []
        svcs = services if services is not None else []
        return mock.patch.multiple(
            fc.nq,
            q4_service_info=mock.Mock(return_value={"priority": priority}),
            q1_blast_radius=mock.Mock(
                return_value={"capabilities": caps, "services": svcs}
            ),
        )

    return _install


# ─────────────────────────────────────────────────────────────
# T79-01: 函数必须存在 —— 直接挡住"再次静默降级"
# ─────────────────────────────────────────────────────────────

def test_t79_01_组级分类函数必须存在且可调用():
    """handler 调的三个 group 版函数，至少 classify_group 必须真的存在。

    这条是最低门禁：它红了就说明又回到了"调不存在的函数、靠 except 降级"
    的状态，而那种状态不会让任何测试变红、也不会让 groups_failed 非 0。
    """
    from core import fault_classifier as fc

    assert hasattr(fc, "classify_group"), (
        "fault_classifier.classify_group 不存在。"
        "window_flush_handler.py 在调它，缺失会被 except 静默降级成 "
        "classify(根因服务)，使「按 EventGroup 分类」形同虚设 —— "
        "而且不会有任何测试变红。"
    )
    assert callable(fc.classify_group)


# ─────────────────────────────────────────────────────────────
# T79-02: 基底必须复用 classify()，不得另造一套严重度矩阵
# ─────────────────────────────────────────────────────────────

def test_t79_02_单服务单告警时与classify结果同构(fc_graph_stub):
    """组里只有一个服务时，判定应与 classify() 一致（没有组级信息可加）。

    这条守的是"不要另造一套严重度矩阵" —— 两套矩阵必然漂移。
    """
    from core import fault_classifier as fc

    with fc_graph_stub(priority="Tier2", capabilities=[], services=["a"]):
        single = fc.classify("petsite", {})
        grouped = fc.classify_group(_group("petsite", [_Alert("petsite", "P2")]))

    assert grouped["severity"] == single["severity"]
    assert grouped["strategy"] == single["strategy"]
    # 组级字段是额外的，不影响基底字段
    for k in ("affected_capabilities", "tier0_impact_count", "service_info"):
        assert grouped[k] == single[k], f"{k} 应与 classify() 一致"


# ─────────────────────────────────────────────────────────────
# T79-03 / 04: 单调不降 —— 两个方向都要测
# ─────────────────────────────────────────────────────────────

def test_t79_03_组内自报更严重时应采用更严重的(fc_graph_stub):
    """图谱推 P2、但组内告警自报 P0 → 取 P0。

    两者视角不同：classify() 算的是图谱拓扑推断，告警自报的是监控源判定。
    都可能对，所以取更严重的那个。
    """
    from core import fault_classifier as fc

    with fc_graph_stub(priority="Tier2", capabilities=[], services=[]):
        base = fc.classify("petsite", {})
        assert base["severity"] == "P2", "前提：图谱应推出 P2"

        g = _group("petsite", [_Alert("petsite", "P0")])
        out = fc.classify_group(g)

    assert out["severity"] == "P0", "组内自报 P0 必须被采纳"
    assert out["strategy"] == "Diagnose-First", "strategy 必须跟着 severity 走"
    assert out["group_severity_source"] == "alert_reported"


def test_t79_04_图谱推更严重时不得被组内自报降级(fc_graph_stub):
    """图谱推 P0、组内告警自报 P2 → 必须**保持 P0**。

    这是单调不降的关键方向。若拓扑关联把无关的低severity告警并进一组，
    绝不能因此把真 P0 冲淡 —— 那是比漏报更危险的失效。
    """
    from core import fault_classifier as fc

    # Tier0 服务 + 2 个 Tier0 能力受损 → 基底 P0
    caps = [{"priority": "Tier0"}, {"priority": "Tier0"}]
    with fc_graph_stub(priority="Tier0", capabilities=caps, services=[]):
        base = fc.classify("petsite", {})
        assert base["severity"] == "P0", "前提：图谱应推出 P0"

        g = _group("petsite", [_Alert("petsite", "P2"), _Alert("petfood", "P2")])
        out = fc.classify_group(g)

    assert out["severity"] == "P0", (
        "组内告警自报 P2 不得把图谱推断的 P0 降级 —— 组信息只升不降"
    )
    assert out["group_severity_source"] == "graph"


# ─────────────────────────────────────────────────────────────
# T79-05: 影响面修正，但刻意不做 P1 → P0
# ─────────────────────────────────────────────────────────────

def test_t79_05_多服务同时告警把P2升到P1(fc_graph_stub):
    """组内 ≥3 个服务同时告警 → 影响面广，P2 升 P1。"""
    from core import fault_classifier as fc

    with fc_graph_stub(priority="Tier2", capabilities=[], services=[]):
        alerts = [_Alert("petsite", "P2"), _Alert("petfood", "P2"), _Alert("petsearch", "P2")]
        out = fc.classify_group(_group("petsite", alerts))

    assert out["severity"] == "P1", "3 个服务同时告警应从 P2 升到 P1"
    assert out["group_service_count"] == 3
    assert out["group_severity_source"] == "group_breadth"


def test_t79_06_告警数多不得自动升到P0(fc_graph_stub):
    """即使组内很多服务告警，P1 也不得自动升 P0。

    P0 必须由图谱拓扑证据（Tier0 服务 + 多个 Tier0 能力受损）决定，
    不能由「告警条数多」决定 —— 否则一次波及面广但无关键业务的抖动
    就会连发 P0，制造告警疲劳，而告警疲劳最终导致真 P0 被忽略。
    """
    from core import fault_classifier as fc

    # Tier0 服务但只影响 1 个 Tier0 能力 → 基底 P1
    with fc_graph_stub(priority="Tier0", capabilities=[{"priority": "Tier0"}], services=[]):
        base = fc.classify("petsite", {})
        assert base["severity"] == "P1", "前提：图谱应推出 P1"

        alerts = [_Alert(f"svc{i}", "P1") for i in range(8)]
        out = fc.classify_group(_group("petsite", alerts))

    assert out["severity"] == "P1", (
        f"8 个服务告警也不得把 P1 自动升成 P0（实得 {out['severity']}）。"
        "P0 应由图谱拓扑证据决定，不由告警条数决定。"
    )


# ─────────────────────────────────────────────────────────────
# T79-07: 组级元信息必须带出，供下游与审计使用
# ─────────────────────────────────────────────────────────────

def test_t79_07_组级元信息必须完整带出(fc_graph_stub):
    """correlator 算出的组级信息必须进入 classification。

    背景：`evidence_alerts` 在补这个函数之前**全仓 0 处消费** ——
    correlator 费力关联出的组级数据算完就扔。这条门禁保证它至少
    进入分类结果，可被下游与审计读到。
    """
    from core import fault_classifier as fc

    with fc_graph_stub(priority="Tier2", capabilities=[], services=["x"]):
        g = _group(
            "petsite",
            [_Alert("petsite", "P2"), _Alert("petfood", "P2")],
            correlation_type="topology",
            confidence=0.83,
        )
        out = fc.classify_group(g)

    assert out["correlation_type"] == "topology"
    assert out["correlation_confidence"] == pytest.approx(0.83)
    assert out["group_alert_count"] == 2
    assert out["group_service_count"] == 2
    assert out["group_id"] == g.group_id
    # 组内服务必须并入 affected_services
    assert "petfood" in out["affected_services"], (
        "组内告警涉及的服务必须并入 affected_services，否则下游看不到真实影响面"
    )


def test_t79_08_空组不得抛异常(fc_graph_stub):
    """退化输入（无告警的组）必须给出可用结果，不能抛。

    handler 对这三个调用都包了 except 降级 —— 那层兜底会掩盖异常，
    所以这里直接要求函数自己扛住退化输入。
    """
    from core import fault_classifier as fc
    from core.topology_correlator import EventGroup

    with fc_graph_stub(priority="Tier2", capabilities=[], services=[]):
        out = fc.classify_group(EventGroup())

    assert out["severity"] in ("P0", "P1", "P2")
    assert out["group_alert_count"] == 0
    assert out["group_service_count"] == 0

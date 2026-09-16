"""
test_37_observer_metrics.py —— T-210：runner 必须采集**观测方**（调用侧）指标。

为什么这组测试存在：
验证边 `A -[X]-> B` 必须在 **B** 注入、观测 **A**。runner 原先只采集
`exp.target_service`（注入目标）自己的指标 —— 「打断 B 之后 B 是否退化」
近乎恒真，**根本没有检验任何边**。这也解释了历史上 72 个实验全部 `passed`、
零失败：判定门槛没有分辨力。

最要紧的一条是 o05/o06：**零流量必须判 inconclusive，绝不能判 refuted。**
`metrics.collect()` 无数据时 fallback `success_rate=100.0 / total_requests=0`
—— 零流量和健康在指标上完全一样。判 refuted 会删掉真实存在的边，
比留着未验证的边有害得多。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "chaos" / "code", ROOT / "infra" / "lambda" / "shared" / "python"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _snap(success_rate: float, total_requests: int, ts: int = 0, p99: float = 100.0):
    from runner.experiment import MetricsSnapshot
    return MetricsSnapshot(
        timestamp=ts, success_rate=success_rate,
        latency_p99_ms=p99, total_requests=total_requests,
    )


def _stub_experiment(observers=()):
    """最小 Experiment，仅供 ExperimentResult.__post_init__ 取 target_service。"""
    from runner.experiment import Experiment, FaultSpec, ObservationTarget
    return Experiment(
        name="stub", description="", target_service="target-svc",
        target_namespace="ns", target_tier="Tier1",
        fault=FaultSpec(type="pod_kill", mode="all", value="100", duration="1m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[ObservationTarget.parse(o) for o in observers],
    )


def _result():
    """构造一个最小 ExperimentResult，不触发任何 IO。"""
    from runner.result import ExperimentResult
    return ExperimentResult(experiment=_stub_experiment())


# ─── 规格解析 ────────────────────────────────────────────────────────────────

def test_o01_observation_target_parses_three_forms():
    """'svc' / 'ns/svc' / dict 三种写法都要能解析。"""
    from runner.experiment import ObservationTarget

    a = ObservationTarget.parse("petsite")
    assert a.service == "petsite" and a.namespace is None

    b = ObservationTarget.parse("petadoptions/gateway-service")
    assert b.service == "gateway-service" and b.namespace == "petadoptions"

    c = ObservationTarget.parse(
        {"service": "order-service", "namespace": "ns1",
         "edge_label": "DependsOn", "min_baseline_requests": 42})
    assert (c.service, c.namespace, c.edge_label, c.min_baseline_requests) == \
        ("order-service", "ns1", "DependsOn", 42)


def test_o02_default_edge_label_is_calls():
    from runner.experiment import ObservationTarget
    assert ObservationTarget.parse("x").edge_label == "Calls"
    assert ObservationTarget.parse("x", "AccessesData").edge_label == "AccessesData"


def test_o03_experiment_defaults_to_no_observers():
    """未声明观测方时必须是空列表 —— 即退化为旧行为，而不是报错。"""
    from runner.experiment import Experiment, FaultSpec
    exp = Experiment(
        name="n", description="d", target_service="svc", target_namespace="ns",
        target_tier="Tier1",
        fault=FaultSpec(type="pod_kill", mode="all", value="100", duration="1m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
    )
    assert exp.observation_targets == []


# ─── 观测方证据 ──────────────────────────────────────────────────────────────

def test_o04_degradation_is_computed_from_observer_not_target():
    """退化率必须来自观测方自己的基线与最低值。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    r.record_observer_snapshot("A", _snap(60.0, 480, 1))
    r.record_observer_snapshot("A", _snap(45.0, 470, 2))
    assert r.observer_min_success_rate["A"] == 45.0
    assert r.observer_degradation_rate("A") == pytest.approx(54.0)


def test_o05_zero_traffic_observer_is_not_usable():
    """零流量观测方必须 usable=False —— 这是不得判 refuted 的前置。"""
    r = _result()
    # DeepFlow 不可达时的 fallback：成功率 100 但请求数 0
    r.record_observer_baseline("A", _snap(100.0, 0))
    r.record_observer_snapshot("A", _snap(100.0, 0, 1))
    assert r.observer_has_real_traffic("A") is False
    assert r.observer_evidence()["A"]["usable"] is False


def test_o06_zero_traffic_classifies_inconclusive_never_refuted():
    """
    最关键的一条：零流量下判定必须是 inconclusive。
    若这条失败，说明系统会把没有流量的真实边判成「不存在」。
    """
    from graph_confidence import classify_intervention, STATUS_INCONCLUSIVE, STATUS_REFUTED
    status, reason = classify_intervention(0, 0, 0.0)
    assert status == STATUS_INCONCLUSIVE
    assert status != STATUS_REFUTED
    assert "流量" in reason


def test_o07_missing_baseline_returns_none_not_zero():
    """
    缺基线时退化率必须是 None 而不是 0.0 ——
    0.0 会被下游读成「完全没退化」从而判 refuted。
    """
    r = _result()
    r.record_observer_snapshot("A", _snap(50.0, 100, 1))
    assert r.observer_degradation_rate("A") is None


def test_o08_missing_injected_samples_returns_none():
    """有基线但注入期无采样，同样不能给出数字。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    assert r.observer_degradation_rate("A") is None


def test_o09_observer_evidence_is_per_service():
    """多个观测方各自独立，不能互相污染。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    r.record_observer_snapshot("A", _snap(40.0, 480, 1))
    r.record_observer_baseline("B", _snap(99.0, 600))
    r.record_observer_snapshot("B", _snap(98.5, 590, 1))
    ev = r.observer_evidence()
    assert ev["A"]["degradation_rate"] == pytest.approx(59.0)
    assert ev["B"]["degradation_rate"] == pytest.approx(0.5)
    assert ev["A"]["usable"] and ev["B"]["usable"]


# ─── runner 接线 ─────────────────────────────────────────────────────────────

def test_o10_runner_references_edge_verification():
    """
    runner 必须真的引用 edge_verification —— 这是 DoD-3 的第一项检查。
    模块存在但没人调用，就是 T-210 之前的状态。
    """
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    assert "edge_verification" in src, "runner.py 未引用 edge_verification"
    assert "_verify_edges" in src, "runner.py 缺少 _verify_edges 接线"
    assert "observation_targets" in src, "runner.py 未读取 observation_targets"


def test_o11_runner_collects_observer_in_both_phases():
    """Phase1 要有观测方基线，Phase3 要有观测方采样 —— 少任一半都算不出退化率。"""
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    assert "_collect_observer_baselines" in src
    assert "_collect_observer_snapshots" in src
    # 基线必须在 Phase1 里调用
    p1 = src.index("_phase1_steady_state_before")
    p2 = src.index("def _phase2_inject")
    assert "_collect_observer_baselines" in src[p1:p2], "Phase1 未采集观测方基线"


def test_o12_no_cardinality_on_edge_properties():
    """
    边属性禁用 property(single, ...) —— Neptune 返回
    400 UnsupportedOperationException。这条写回历史上 100% 失败过 21 次。
    只扫 g.E() 上下文，不误伤顶点写入，也不误伤文档字符串。
    """
    import re
    bad = []
    for p in (ROOT / "chaos" / "code").rglob("*.py"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"""g\.E\(\)[^"']*?property\(\s*single""", t, re.S):
            bad.append(f"{p}:{t[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"边属性上仍有 property(single, ...): {bad}"


# ─── SLI 口径（2026-08-31 实测缺陷）─────────────────────────────────────────

def test_o13_sli_query_excludes_dns():
    """
    SLI 查询必须只统计应用层协议。

    实测缺陷：原实现只按 `request_domain LIKE '%svc%'` 过滤，于是同一服务名的
    **DNS 查询**也被计入成功率。K8s 默认 ndots=5 会把 FQDN 逐个拼上搜索域
    再查一遍，产生大量预期内的 NXDOMAIN（response_status=4 / response_code=3），
    被当成服务故障：

        list-adoptions   混合口径 31.37%  ->  仅 HTTP 100.00%
        search-service   混合口径 69.16%  ->  仅 HTTP 100.00%

    后果有两层，第二层更要紧：稳态检查 >= 95% 永远过不了（实验在 preflight 就失败）；
    且噪声底盘随 DNS 行为波动，一次真实的 20pp HTTP 退化会被淹没或伪造出来 ——
    与不变量 7 同类：「坏掉」和「正常」在指标上分不开。
    """
    from runner.metrics import DeepFlowMetrics
    f = DeepFlowMetrics._proto_filter()
    assert "l7_protocol_str" in f, "SLI 未按协议过滤"
    assert "'HTTP'" in f
    assert "DNS" not in f, "DNS 绝不能进 SLI 白名单"


def test_o14_collect_sql_carries_proto_filter():
    """过滤必须真的进到 collect() 的 SQL 里，不能只定义了常量没人用。"""
    src = (ROOT / "chaos" / "code" / "runner" / "metrics.py").read_text(encoding="utf-8")
    i = src.index("def collect(")
    j = src.index("def collect_steady(")
    body = src[i:j]
    assert "_proto_filter()" in body, "collect() 的 SQL 未应用协议过滤"


def test_o15_app_protocol_allowlist_is_by_name_not_number():
    """
    白名单用协议名而非数字枚举：实测本环境 HTTP=20 / DNS=120，
    但数字是 DeepFlow 内部实现，升级可能变。
    """
    from runner.metrics import DeepFlowMetrics
    assert all(isinstance(p, str) and not p.isdigit()
               for p in DeepFlowMetrics.APP_PROTOCOLS)


# ─── 清理路径（2026-08-31 真实注入暴露）───────────────────────────────────────

def test_o16_cleanup_calls_delete_with_keywords_only():
    """
    清理路径调用 injector.delete 必须全部用关键字传参。

    `ChaosMCPClient.delete(self, chaos_type, name, namespace)` 的**第一个**位置参数
    是 `chaos_type` 而不是 `name`。原实现按位置传实验名、又用关键字传 chaos_type，
    撞成 `TypeError: got multiple values for argument 'chaos_type'`。

    这个 bug 一直没被发现，是因为两处调用都在**熔断/异常清理路径**上 ——
    72 个历史实验全部 result='passed'，清理路径从未被执行过。
    与「写回 100% 失败 21 次」是同一个元模式：失败路径从不被走到。

    2026-08-31 真实注入实测后果（这不是理论风险）：
      stop condition 在 27.4% 触发 -> 清理抛异常 -> HTTPChaos CRD 留在集群继续生效
      -> 强删仍在生效的 CRD 会把 tproxy 拦截残留在目标 Pod 的网络命名空间
      -> 容器重启清不掉，两个被命中的 Pod 进 CrashLoopBackOff，只能删 Pod 重建。
    """
    import re
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    calls = re.findall(r"injector\.delete\((.*?)\)", src, re.S)
    assert calls, "runner.py 里找不到 injector.delete 调用"
    for c in calls:
        args = [a.strip() for a in c.split(",") if a.strip()]
        for a in args:
            assert "=" in a, (
                f"injector.delete 有位置参数 {a!r} —— 第一个位置参数是 chaos_type "
                f"不是 name，必须全部用关键字传参"
            )


def test_o17_delete_signature_order_is_chaos_type_first():
    """
    钉住被调方签名顺序。若将来有人把 delete 的参数顺序改成 (name, chaos_type)，
    这条测试会失败，提醒同步改所有调用点 —— 而不是让它在只有熔断时才走的
    路径上静默炸掉。
    """
    import inspect, sys as _sys
    sys.path.insert(0, str(ROOT / "chaos" / "code"))
    from runner.chaos_mcp import ChaosMCPClient
    params = list(inspect.signature(ChaosMCPClient.delete).parameters)
    assert params[:3] == ["self", "chaos_type", "name"], (
        f"delete 签名变了：{params} —— 请同步 runner.py 的两处调用点")


def test_o18_phase4_deletes_crd_on_normal_completion():
    """
    正常完成路径也必须删 Chaos Mesh CRD。

    原 docstring 写着「Chaos Mesh duration 字段负责到期删除 CR」——**假设是错的**。
    2026-08-31 实测：duration 到期后故障停止生效但 CRD 对象仍存在，
    而 runner 只在熔断/异常路径删。后果是一个 PASSED 的实验结束后
    httpchaos 仍有 1 条，两个被注入过的 Pod 随后从 2/2 退回 1/2
    （tproxy 仍挂在 netns），而 Phase 5 在这之前采样、显示 100% 通过 ——
    污染被完全掩盖，并成为下一次实验的稳态基线污染源。
    """
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    i = src.index("def _phase4_recover")
    j = src.index("def _phase5_steady_state_after")
    body = src[i:j]
    assert "injector.delete(" in body, "Phase4 未在正常路径删除 CRD"
    assert "chaos_type=" in body, "Phase4 的 delete 必须关键字传参"
    # 删不掉要显式报错，不能静默
    assert "logger.error" in body, "Phase4 删除 CRD 失败时必须显式报错"


# ── 采集失败不得伪造谷值（2026-08-31 二次实测缺陷）──────────────────────────

def test_o90_failed_collection_excluded_from_min_requests():
    """`ok=False` 的采样点只入列表、不参与 min。

    `metrics.collect()` 查询异常时 fallback 成 (100%, 0 requests)。把它算进
    `observer_min_requests`，一次 ClickHouse 抖动就让谷值变 0 —— 实测把
    petsite 的基线 322 → 谷值 0 算成「吞吐塌陷 100%」，合成退化率 100pp，
    足以把一条边**误判成 confirmed**。
    """
    from runner.experiment import MetricsSnapshot

    r = _result()
    r.record_observer_baseline('petsite', MetricsSnapshot(
        timestamp=0, success_rate=100.0, latency_p99_ms=10.0, total_requests=322))

    # 两个真实采样点 + 一个采集失败的采样点
    r.record_observer_snapshot('petsite', MetricsSnapshot(
        timestamp=1, success_rate=100.0, latency_p99_ms=10.0, total_requests=300))
    r.record_observer_snapshot('petsite', MetricsSnapshot(
        timestamp=2, success_rate=100.0, latency_p99_ms=0.0, total_requests=0, ok=False))
    r.record_observer_snapshot('petsite', MetricsSnapshot(
        timestamp=3, success_rate=100.0, latency_p99_ms=10.0, total_requests=310))

    assert len(r.observer_snapshots['petsite']) == 3, "失败的采样点仍应入列表（留痕）"
    assert r.observer_min_requests['petsite'] == 300, "谷值不得被失败采样点污染"
    # 真实情况是"健康"：吞吐几乎没掉，合成退化率应接近 0 而不是 100
    drop = r.observer_throughput_drop_pct('petsite')
    assert drop is not None and drop < 10.0, f"吞吐塌陷应接近 0，实得 {drop}"


def test_o91_all_collections_failed_yields_no_verdict_input():
    """全部采样点都采集失败时，min 一个都不写 —— 宁可判不了，不可编一个谷值。"""
    from runner.experiment import MetricsSnapshot

    r = _result()
    r.record_observer_baseline('petsite', MetricsSnapshot(
        timestamp=0, success_rate=100.0, latency_p99_ms=10.0, total_requests=322))
    for i in range(3):
        r.record_observer_snapshot('petsite', MetricsSnapshot(
            timestamp=i, success_rate=100.0, latency_p99_ms=0.0,
            total_requests=0, ok=False))

    assert 'petsite' not in r.observer_min_requests
    assert 'petsite' not in r.observer_min_success_rate
    assert r.observer_throughput_drop_pct('petsite') is None
    assert r.observer_degradation_rate('petsite') is None

"""
test_21_e2e_pipeline.py — Sprint 7 ETL→Neptune→Query 端到端测试

Tests: S7-01 ~ S7-07

所有测试均标记 @pytest.mark.neptune（需真实 Neptune 连接）。
"""
import dataclasses
import logging
import os
import sys
import time
import uuid

import pytest

logger = logging.getLogger(__name__)

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"

# ── S7-01 ────────────────────────────────────────────────────────────────────

# Node types expected to exist in Neptune after ETL runs
_EXPECTED_NODE_LABELS = [
    "EC2Instance",
    "EKSCluster",
    "Pod",
    "Microservice",
    "RDSCluster",
    "DynamoDBTable",
    "AvailabilityZone",
    "VPC",
    "Subnet",
]


@pytest.mark.neptune
def test_s7_01_all_node_types_exist_after_etl(neptune_rca):
    """S7-01: ETL AWS 写入后 Graph Explorer 可查到所有节点类型（每种至少 1 个）。

    验证 neptune_rca client 能查到每种预期节点标签，确认 ETL pipeline 写入完整。
    连接失败时返回清晰的 AssertionError（非 AttributeError）。
    """
    missing: list[str] = []

    for label in _EXPECTED_NODE_LABELS:
        try:
            rows = neptune_rca.results(
                f"MATCH (n:{label}) RETURN count(n) AS cnt LIMIT 1"
            )
        except Exception as exc:
            pytest.fail(
                f"S7-01: Neptune query failed for label '{label}'. "
                f"Check endpoint connectivity. Error: {exc}"
            )

        cnt = rows[0].get("cnt", 0) if rows else 0
        if cnt == 0:
            missing.append(label)

    assert not missing, (
        f"S7-01: 以下节点类型在 Neptune 中没有数据（ETL 可能未完整写入）: "
        f"{missing}"
    )


# ── S7-02 ────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s7_02_node_detail_properties_non_empty(neptune_rca):
    """S7-02: 节点详情查询返回正确属性（非 None，非空字符串）。

    抽查 Microservice 和 EC2Instance 节点的关键属性，
    确认 ETL 写入属性值，而非 null/空字符串占位符。
    """
    # Check Microservice: name, recovery_priority
    svc_rows = neptune_rca.results(
        "MATCH (m:Microservice) RETURN m.name AS name, "
        "m.recovery_priority AS priority LIMIT 5"
    )
    if not svc_rows:
        pytest.skip("S7-02: No Microservice nodes found; run ETL first")

    for row in svc_rows:
        name = row.get("name")
        assert name is not None and name != "", (
            f"S7-02: Microservice.name 不应为 None 或空字符串，实际: {name!r}"
        )
        priority = row.get("priority")
        assert priority is not None and priority != "", (
            f"S7-02: Microservice(name={name}).recovery_priority 不应为 None 或空，"
            f"实际: {priority!r}"
        )

    # Check EC2Instance: instance_id, state
    ec2_rows = neptune_rca.results(
        "MATCH (e:EC2Instance) RETURN e.instance_id AS iid, e.state AS state LIMIT 5"
    )
    if not ec2_rows:
        pytest.skip("S7-02: No EC2Instance nodes found; run ETL first")

    for row in ec2_rows:
        iid = row.get("iid")
        assert iid is not None and iid != "", (
            f"S7-02: EC2Instance.instance_id 不应为 None 或空，实际: {iid!r}"
        )
        state = row.get("state")
        assert state is not None and state != "", (
            f"S7-02: EC2Instance(id={iid}).state 不应为 None 或空，实际: {state!r}"
        )


# ── S7-03 ────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s7_03_deepflow_etl_calls_edges_exist(neptune_rca):
    """S7-03: ETL DeepFlow 写入后 Calls 边可查到。

    验证 DeepFlow ETL 已将微服务调用关系写入 Neptune（至少 1 条 Calls 边）。
    """
    try:
        rows = neptune_rca.results(
            "MATCH (a:Microservice)-[r:Calls]->(b:Microservice) "
            "RETURN a.name AS caller, b.name AS callee, "
            "type(r) AS rel LIMIT 10"
        )
    except Exception as exc:
        pytest.fail(
            f"S7-03: Neptune Calls edge query failed. "
            f"Check endpoint connectivity. Error: {exc}"
        )

    assert len(rows) > 0, (
        "S7-03: Neptune 中未找到 Calls 边（Microservice→Microservice）。"
        " 请确认 ETL DeepFlow pipeline 已成功运行并写入调用拓扑。"
    )

    # Sanity: all returned rows have non-empty caller/callee
    for row in rows:
        assert row.get("caller") and row.get("callee"), (
            f"S7-03: Calls 边端点不应为空，实际: {row}"
        )

    print(f"\nS7-03: 找到 {len(rows)} 条 Calls 边，示例: {rows[0]}")


# ── S7-04 ────────────────────────────────────────────────────────────────────

# Unique test experiment ID to avoid polluting production data
_TEST_EXP_ID = f"test-auto-chaos-s7-{uuid.uuid4().hex[:8]}"
_TEST_SVC = "petsite"


@pytest.fixture(scope="module")
def chaos_experiment_written(neptune_rca):
    """写入一条测试用 ChaosExperiment 节点 + TestedBy 边（模块级，跑一次）。

    conftest.py 已将 chaos/code 加入 sys.path，直接 import neptune_sync。
    不要将 chaos/code/runner 加入 sys.path（会使 runner.py 被当作顶级模块，
    破坏包内相对 import）。
    """
    # chaos/code is already in sys.path from conftest; just import directly
    from neptune_sync import write_experiment

    experiment = {
        "experiment_id": _TEST_EXP_ID,
        "target_service": _TEST_SVC,
        "fault_type": "cpu-stress",
        "result": "passed",
        "recovery_time_sec": 42,
        "degradation_rate": 0.15,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        write_experiment(experiment)
    except Exception as exc:
        pytest.fail(
            f"S7-04 setup: write_experiment failed. "
            f"Check Neptune connectivity and chaos neptune_sync code. Error: {exc}"
        )

    yield experiment


@pytest.mark.neptune
def test_s7_04_chaos_testedby_edge_written(neptune_rca, chaos_experiment_written):
    """S7-04: Chaos 实验完成后 TestedBy 边可查到且属性完整（experiment_id/fault_type/result 非 None）。

    fixture chaos_experiment_written 先通过 neptune_sync.write_experiment() 写入测试数据，
    本测试验证数据已落库且关键属性有值。
    """
    exp_id = chaos_experiment_written["experiment_id"]

    rows = neptune_rca.results(
        "MATCH (svc:Microservice)-[:TestedBy]->(exp:ChaosExperiment "
        "{experiment_id: $exp_id}) "
        "RETURN svc.name AS service, exp.experiment_id AS exp_id, "
        "exp.fault_type AS fault_type, exp.result AS result, "
        "exp.recovery_time_sec AS recovery_time",
        {"exp_id": exp_id},
    )

    assert len(rows) > 0, (
        f"S7-04: TestedBy 边未找到（experiment_id={exp_id}）。"
        " 确认 neptune_sync.write_experiment() 已正确写入。"
    )

    row = rows[0]
    for field_name in ("exp_id", "fault_type", "result"):
        val = row.get(field_name)
        assert val is not None and val != "", (
            f"S7-04: ChaosExperiment.{field_name} 不应为 None 或空，"
            f"experiment_id={exp_id}，实际: {val!r}"
        )

    assert row.get("service") == _TEST_SVC, (
        f"S7-04: TestedBy 起点服务应为 '{_TEST_SVC}'，实际: {row.get('service')!r}"
    )

    print(f"\nS7-04: TestedBy 验证通过 — {row}")


# ── S7-05 ────────────────────────────────────────────────────────────────────

_TEST_INC_ID = f"test-auto-inc-s7-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def incident_written(neptune_rca):
    """写入一条测试用 Incident 节点 + TriggeredBy 边（模块级，跑一次）。"""
    from actions.incident_writer import write_incident

    classification = {
        "affected_service": "petsite",
        "severity": "P1",
    }
    rca_result = {
        "top_candidate": {
            "service": "petsearch",
            "confidence": 0.75,
        },
        "all_candidates": [
            {"service": "petsearch", "confidence": 0.75},
        ],
    }
    report_text = (
        "RCA 报告：petsite 服务出现 5xx 错误，根因定位到 petsearch 服务 CPU 使用率过高。"
    )

    try:
        incident_id = write_incident(classification, rca_result, resolution="重启 petsearch Pod", report_text=report_text)
    except Exception as exc:
        pytest.fail(
            f"S7-05 setup: write_incident failed. "
            f"Check Neptune connectivity and incident_writer code. Error: {exc}"
        )

    yield incident_id


@pytest.mark.neptune
def test_s7_05_incident_written_and_rca_structured(neptune_rca, incident_written):
    """S7-05: Incident 写入后可触发根因分析返回结构化结果。

    验证 write_incident() 产生的 Incident 节点有 root_cause/severity/status 字段，
    且 TriggeredBy 边正确连接到 affected_service。
    """
    incident_id = incident_written

    rows = neptune_rca.results(
        "MATCH (inc:Incident {id: $id})-[:TriggeredBy]->(svc) "
        "RETURN inc.id AS id, inc.severity AS severity, "
        "inc.root_cause AS root_cause, inc.status AS status, "
        "svc.name AS triggered_by",
        {"id": incident_id},
    )

    assert len(rows) > 0, (
        f"S7-05: 未找到 Incident 节点或 TriggeredBy 边（id={incident_id}）。"
        " 确认 incident_writer.write_incident() 已正确执行。"
    )

    row = rows[0]

    # Structural checks
    assert row.get("id") == incident_id, (
        f"S7-05: Incident.id 不匹配，期望 {incident_id!r}，实际 {row.get('id')!r}"
    )
    assert row.get("severity") in ("P0", "P1", "P2", "P3"), (
        f"S7-05: Incident.severity 应为 P0/P1/P2/P3，实际: {row.get('severity')!r}"
    )
    assert row.get("root_cause") is not None and row.get("root_cause") != "", (
        f"S7-05: Incident.root_cause 不应为空，实际: {row.get('root_cause')!r}"
    )
    assert row.get("status") is not None, (
        f"S7-05: Incident.status 不应为 None，实际: {row.get('status')!r}"
    )
    assert row.get("triggered_by") == "petsite", (
        f"S7-05: TriggeredBy 目标应为 'petsite'，实际: {row.get('triggered_by')!r}"
    )

    print(f"\nS7-05: Incident 结构化结果验证通过 — {row}")


# ── S7-06 ────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s7_06_dr_plan_generated_from_graph_data(neptune_rca):
    """S7-06: DR Plan 生成器基于当前图数据生成有效计划（非空，含 steps 字段）。

    使用 PlanGenerator 对当前 Neptune 图执行 az scope plan，
    验证生成的 DRPlan：
    - plan_id 非空
    - phases 列表非空
    - 至少一个 phase 含 steps 列表（非空）
    """
    dr_dir = os.path.join(PROJECT_ROOT, "dr-plan-generator")
    if dr_dir not in sys.path:
        sys.path.insert(0, dr_dir)

    try:
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder
        from registry.registry_loader import get_registry
        import graph.neptune_client as _gnc
    except ImportError as exc:
        pytest.fail(f"S7-06: Import failed — {exc}")

    # dr-plan-generator/config.py evaluates NEPTUNE_ENDPOINT at import time,
    # but conftest sets the env var *after* building the unified config, so
    # the module-level variable may be "". Patch it explicitly here.
    _gnc.NEPTUNE_ENDPOINT = os.environ.get(
        "NEPTUNE_ENDPOINT",
        "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
    )

    try:
        registry = get_registry()
        analyzer = GraphAnalyzer(registry=registry)
        builder = StepBuilder()
        generator = PlanGenerator(analyzer, builder)

        plan = generator.generate_plan(
            scope="az",
            source="ap-northeast-1a",
            target="ap-northeast-1c",
        )
    except Exception as exc:
        pytest.fail(
            f"S7-06: DR plan generation failed. "
            f"Check Neptune connectivity and graph_analyzer. Error: {exc}"
        )

    assert plan is not None, "S7-06: generate_plan() 返回 None"
    assert plan.plan_id, f"S7-06: DRPlan.plan_id 不应为空，实际: {plan.plan_id!r}"
    assert len(plan.phases) > 0, (
        f"S7-06: DRPlan.phases 不应为空（AZ scope plan 应至少有 pre-flight 阶段）"
    )

    all_steps = [step for phase in plan.phases for step in phase.steps]
    assert len(all_steps) > 0, (
        f"S7-06: 所有 phases 中应至少有 1 个 step，实际 phases={[p.phase_id for p in plan.phases]}"
    )

    # Validate step structure
    step = all_steps[0]
    assert hasattr(step, "step_id") and step.step_id, (
        f"S7-06: DRStep.step_id 不应为空"
    )
    assert hasattr(step, "action") and step.action, (
        f"S7-06: DRStep.action 不应为空"
    )

    print(
        f"\nS7-06: plan_id={plan.plan_id}, phases={len(plan.phases)}, "
        f"total_steps={len(all_steps)}, rto={plan.estimated_rto}min"
    )


# ── S7-07 ────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s7_07_idempotent_write_node_count_stable(neptune_rca):
    """S7-07: 多轮幂等写入后节点数量不膨胀（前后查询节点数一致）。

    写入相同的 ChaosExperiment 节点两次（使用 MERGE），
    验证第二次写入后 Neptune 中该节点数量未增加（MERGE 保证幂等性）。
    """
    exp_id = f"test-auto-idempotent-{uuid.uuid4().hex[:8]}"
    svc = "petsite"

    # chaos/code is already in sys.path from conftest — import directly.
    # Do NOT add chaos/code/runner to sys.path; that shadows the runner package
    # with runner.py and breaks relative imports inside the package.
    try:
        from neptune_sync import write_experiment
    except ImportError as exc:
        pytest.fail(f"S7-07: Import neptune_sync failed — {exc}")

    experiment = {
        "experiment_id": exp_id,
        "target_service": svc,
        "fault_type": "latency-injection",
        "result": "passed",
        "recovery_time_sec": 10,
        "degradation_rate": 0.05,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Count before
    def _count_exp() -> int:
        rows = neptune_rca.results(
            "MATCH (exp:ChaosExperiment {experiment_id: $exp_id}) "
            "RETURN count(exp) AS cnt",
            {"exp_id": exp_id},
        )
        return rows[0].get("cnt", 0) if rows else 0

    assert _count_exp() == 0, f"S7-07: 测试前节点应不存在（experiment_id={exp_id}）"

    # First write
    try:
        write_experiment(experiment)
    except Exception as exc:
        pytest.fail(f"S7-07: 第一次 write_experiment 失败: {exc}")

    count_after_first = _count_exp()
    assert count_after_first == 1, (
        f"S7-07: 第一次写入后应有 1 个节点，实际: {count_after_first}"
    )

    # Second write (same experiment_id → MERGE → no new node)
    try:
        write_experiment({**experiment, "result": "failed"})  # different result
    except Exception as exc:
        pytest.fail(f"S7-07: 第二次 write_experiment 失败: {exc}")

    count_after_second = _count_exp()
    assert count_after_second == 1, (
        f"S7-07: 幂等写入后节点数应仍为 1（MERGE 不应新建节点），实际: {count_after_second}"
    )

    # Verify MERGE updated the result property (ON MATCH SET)
    rows = neptune_rca.results(
        "MATCH (exp:ChaosExperiment {experiment_id: $exp_id}) "
        "RETURN exp.result AS result",
        {"exp_id": exp_id},
    )
    assert rows and rows[0].get("result") == "failed", (
        f"S7-07: 第二次 MERGE 后 result 应更新为 'failed'，实际: {rows}"
    )

    print(
        f"\nS7-07: 幂等写入验证通过 — 两次写入后节点数={count_after_second}，"
        f"result 已更新为 'failed'"
    )

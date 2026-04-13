"""
composite_runner.py — 组合实验执行引擎

复用 ExperimentRunner 的 5-Phase 架构，重写 Phase 2（注入）和 Phase 4（恢复）
以支持多 action 跨后端的组合实验。

Phase 0: Pre-flight      → 每个 action 分别做 preflight
Phase 1: Steady State     → 复用现有逻辑
Phase 2: Fault Injection  → ActionScheduler 编排注入
Phase 3: Observation      → 复用现有观测 + abort_all 熔断
Phase 4: Recovery         → 等待所有 action 结束，检查恢复
Phase 5: Steady State     → 复用稳态验证 + 报告
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .experiment import (
    CompositeExperiment, FaultAction, Experiment, parse_duration,
)
from .action_scheduler import ActionScheduler, ActionState
from .result import ExperimentResult
from .metrics import DeepFlowMetrics
from .rca import RCATrigger
from .report import Reporter
from .graph_feedback import GraphFeedback
from .chaos_mcp import ChaosMCPClient
from .fis_backend import FISClient
from .observability import get_logger, ChaosMetrics

logger = logging.getLogger(__name__)
slog = get_logger("composite-runner")


class AbortException(Exception):
    """Stop Condition 触发，安全熔断"""


class PrefightFailure(Exception):
    """Pre-flight 检查失败"""


@dataclass
class CompositeExperimentResult(ExperimentResult):
    """扩展 ExperimentResult，记录每个 action 的状态"""
    action_states: dict = field(default_factory=dict)  # action_id → {status, chaos_name, inject_time, error}


class CompositeRunner:
    """组合实验执行引擎"""

    OBSERVE_INTERVAL = 10
    RECOVERY_POLL_INTERVAL = 15
    RECOVERY_TIMEOUT = 300
    STEADY_SAMPLES = 3

    def __init__(self, dry_run: bool = False, tags: dict = None):
        self.dry_run = dry_run
        self.tags = tags or {}
        self.metrics = DeepFlowMetrics()
        self.cm_client = ChaosMCPClient()      # Chaos Mesh
        self.fis_client = FISClient()           # FIS
        self.rca = RCATrigger()
        self.reporter = Reporter()
        self.cw_metrics = ChaosMetrics()
        self.scheduler = ActionScheduler()

    def run(self, experiment: CompositeExperiment) -> CompositeExperimentResult:
        result = CompositeExperimentResult(experiment=experiment)
        result.start_time = datetime.now(timezone.utc)
        result.min_success_rate = 100.0

        try:
            self._phase0_preflight(experiment, result)
            self._phase1_steady_state_before(experiment, result)
            self._phase2_inject(experiment, result)
            self._phase3_observe(experiment, result)
            self._phase4_recover(experiment, result)
            self._phase5_steady_state_after(experiment, result)

        except AbortException as e:
            result.status = "ABORTED"
            result.abort_reason = str(e)
            logger.error(f"🛑 组合实验熔断: {e}")
            self._emergency_cleanup()

        except PrefightFailure as e:
            result.status = "ABORTED"
            result.abort_reason = f"Pre-flight 失败: {e}"
            logger.error(f"🛑 Pre-flight 失败: {e}")

        except Exception as e:
            result.status = "ERROR"
            result.abort_reason = f"未预期异常: {e}"
            logger.error(f"💥 组合实验异常: {e}\n{traceback.format_exc()}")
            self._emergency_cleanup()

        finally:
            result.end_time = result.end_time or datetime.now(timezone.utc)
            # 保存 action 状态到结果
            result.action_states = {
                aid: {
                    "status": s.status,
                    "chaos_name": s.chaos_name,
                    "inject_time": s.inject_time.isoformat() if s.inject_time else "",
                    "error": s.error,
                }
                for aid, s in self.scheduler.states.items()
            }
            self._save_and_report(result)

        return result

    # ── Phase 0: Pre-flight ──────────────────────────────────────────────

    def _phase0_preflight(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        logger.info(f"🔍 Phase 0: Pre-flight — {len(exp.actions)} actions")
        slog.info("phase_started", phase=0, experiment=exp.name, backend="composite")

        backends_needed = {a.backend for a in exp.actions}

        # FIS preflight
        if "fis" in backends_needed:
            if not self.fis_client.preflight_check():
                raise PrefightFailure("FIS 服务不可用")
            logger.info("  ✅ FIS preflight 通过")

        # ChaosMesh preflight：检查残留实验 + Pod 健康
        if "chaosmesh" in backends_needed:
            active = self.cm_client.list_experiments()
            if active:
                names = [e.get("name", "?") for e in active if isinstance(e, dict)]
                raise PrefightFailure(f"检测到 {len(active)} 个残留 CM 实验: {names}")

            # 检查每个 ChaosMesh action 的目标 Pod
            for action in exp.actions:
                if action.backend != "chaosmesh":
                    continue
                svc = action.target_service or exp.target_service
                ns = action.target_namespace or exp.target_namespace
                pods = self.cm_client.check_pods(svc, ns)
                if pods["total"] == 0:
                    raise PrefightFailure(f"服务 {svc} 无 Running Pods")
                if pods["running"] < pods["total"]:
                    not_ok = [p["pod"] for p in pods["not_running"]]
                    raise PrefightFailure(f"服务 {svc} 有 Pod 未就绪: {not_ok}")
                logger.info(f"  ✅ {svc}: {pods['total']} pods ready")

        slog.info("phase_completed", phase=0, experiment=exp.name)

    # ── Phase 1: Steady State Before ─────────────────────────────────────

    def _phase1_steady_state_before(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        logger.info("📊 Phase 1: Steady State Before")
        slog.info("phase_started", phase=1, experiment=exp.name)

        snap = self.metrics.collect_steady(
            service=exp.target_service,
            namespace=exp.target_namespace,
            window_seconds=60,
            samples=self.STEADY_SAMPLES,
            interval=10,
        )
        result.steady_state_before = snap
        logger.info(f"稳态基线: success_rate={snap.success_rate:.1f}%, p99={snap.latency_p99_ms:.0f}ms")

        for check in exp.steady_state_before:
            if not check.is_satisfied(snap):
                raise PrefightFailure(f"注入前稳态检查失败: {check.describe(snap)}")
            logger.info(f"  ✅ {check.describe(snap)}")

    # ── Phase 2: Fault Injection（核心差异） ───────────────────────────────

    def _phase2_inject(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        logger.info(f"💥 Phase 2: Composite Injection — {len(exp.actions)} actions")
        slog.info("phase_started", phase=2, experiment=exp.name, action_count=len(exp.actions))

        def inject_fn(action: FaultAction, dry_run: bool) -> tuple[str, str]:
            """注入回调：根据 backend 调用对应客户端"""
            if dry_run:
                logger.info(f"  ⚡ [dry-run] {action.id}: {action.type} on {action.backend}")
                return f"dry-run-{action.id}", ""

            svc = action.target_service or exp.target_service
            ns = action.target_namespace or exp.target_namespace

            if action.backend == "fis":
                # 构造临时 Experiment 给 FISClient
                from .experiment import FaultSpec, Experiment as SingleExp
                tmp_fault = FaultSpec(
                    type=action.type,
                    mode=action.params.get("mode", "all"),
                    value=str(action.params.get("value", "100")),
                    duration=action.params.get("duration", "2m"),
                    extra_params=action.params.get("extra_params"),
                )
                tmp_exp = SingleExp(
                    name=f"{exp.name}-{action.id}",
                    description=f"Composite action: {action.id}",
                    target_service=svc,
                    target_namespace=ns,
                    target_tier=exp.target_tier,
                    fault=tmp_fault,
                    steady_state_before=[],
                    steady_state_after=[],
                    stop_conditions=exp.stop_conditions,
                    backend="fis",
                )
                fis_result = self.fis_client.inject(tmp_exp)
                return fis_result["experiment_id"], fis_result.get("template_id", "")

            else:
                # ChaosMesh
                p = action.params
                mcp_result = self.cm_client.inject(
                    fault_type=action.type,
                    service=svc,
                    namespace=ns,
                    duration=p.get("duration", "2m"),
                    mode=p.get("mode", "all"),
                    value=str(p.get("value", "100")),
                    latency=p.get("latency"),
                    loss=p.get("loss"),
                    corrupt=p.get("corrupt"),
                    container_names=p.get("container_names"),
                    workers=p.get("workers"),
                    load=p.get("load"),
                    size=p.get("size"),
                    time_offset=p.get("time_offset"),
                    direction=p.get("direction"),
                    external_targets=p.get("external_targets"),
                )
                chaos_name = self.cm_client.extract_experiment_name(mcp_result, action.type)
                return chaos_name, ""

        # 通过 Scheduler 编排执行
        self.scheduler.execute(experiment=exp, inject_fn=inject_fn, dry_run=self.dry_run)

        # 记录首个注入时间
        for state in self.scheduler.states.values():
            if state.inject_time:
                if result.inject_time is None or state.inject_time < result.inject_time:
                    result.inject_time = state.inject_time

        # 汇总 chaos_experiment_name（用第一个 active 的）
        for state in self.scheduler.states.values():
            if state.chaos_name and state.chaos_name != "dry-run":
                result.chaos_experiment_name = state.chaos_name
                break

        active_count = sum(1 for s in self.scheduler.states.values() if s.status == "active")
        error_count = sum(1 for s in self.scheduler.states.values() if s.status == "error")
        logger.info(f"  注入完成: {active_count} active, {error_count} error")

    # ── Phase 3: Observation ─────────────────────────────────────────────

    def _phase3_observe(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        """观测 + Stop Conditions 检查"""
        # 计算观测窗口 = max(所有 action 的 duration) + 30s buffer
        max_dur = 0
        for action in exp.actions:
            dur_str = action.params.get("duration", "2m")
            try:
                max_dur = max(max_dur, parse_duration(str(dur_str)))
            except ValueError:
                pass
        observe_window = max_dur + 30

        logger.info(f"👁  Phase 3: Observation（{observe_window}s 窗口）")

        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过观测")
            return

        end_ts = time.time() + observe_window
        rca_triggered = False

        while time.time() < end_ts:
            snap = self.metrics.collect(
                service=exp.target_service,
                namespace=exp.target_namespace,
                window_seconds=60,
            )
            result.record_snapshot(snap)

            elapsed = round(time.time() - result.inject_time.timestamp(), 0) if result.inject_time else 0
            logger.info(
                f"  T+{elapsed:.0f}s | success={snap.success_rate:.1f}% "
                f"p99={snap.latency_p99_ms:.0f}ms"
            )

            # Stop Conditions
            for cond in exp.stop_conditions:
                if cond.is_triggered(snap):
                    msg = cond.describe(snap)
                    logger.error(f"🚨 Stop Condition 触发: {msg}")
                    self._emergency_cleanup()
                    raise AbortException(msg)

            # RCA
            if (exp.rca.enabled
                    and not rca_triggered
                    and result.inject_time
                    and result.elapsed_since_injection() >= parse_duration(exp.rca.trigger_after)):
                fault_types = [a.type for a in exp.actions]
                logger.info(f"🧠 触发 RCA: {exp.target_service} (故障类型: {fault_types})")
                inject_ts = result.inject_time.isoformat()
                rca_result = self.rca.trigger(exp.target_service, ",".join(fault_types), inject_ts)
                result.rca_result = rca_result
                rca_triggered = True

            time.sleep(self.OBSERVE_INTERVAL)

    # ── Phase 4: Recovery ────────────────────────────────────────────────

    def _phase4_recover(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        logger.info(f"♻️  Phase 4: Recovery — 等待所有 action 恢复")

        if self.dry_run:
            result.recovery_seconds = 0.0
            return

        recover_start = time.time()

        # 等待 FIS 实验完成
        for state in self.scheduler.states.values():
            if state.action.backend == "fis" and state.chaos_name:
                logger.info(f"  等待 FIS 实验: {state.chaos_name}")
                final = self.fis_client.wait_for_completion(
                    state.chaos_name, timeout=self.RECOVERY_TIMEOUT, poll_interval=15
                )
                logger.info(f"  FIS {state.chaos_name}: {final}")
                if state.fis_template_id:
                    self.fis_client.delete_template(state.fis_template_id)

        # 等待 Pod 恢复（检查所有涉及的 ChaosMesh 服务）
        services_to_check = set()
        for action in exp.actions:
            if action.backend == "chaosmesh":
                svc = action.target_service or exp.target_service
                ns = action.target_namespace or exp.target_namespace
                services_to_check.add((svc, ns))

        deadline = recover_start + self.RECOVERY_TIMEOUT
        for svc, ns in services_to_check:
            while time.time() < deadline:
                pods = self.cm_client.check_pods(svc, ns)
                if pods["total"] > 0 and pods["running"] == pods["total"]:
                    logger.info(f"  ✅ {svc}: {pods['running']}/{pods['total']} pods ready")
                    break
                logger.info(f"  {svc}: {pods['running']}/{pods['total']} running, waiting...")
                time.sleep(self.RECOVERY_POLL_INTERVAL)

        result.recovery_seconds = round(time.time() - recover_start, 1)
        logger.info(f"✅ Recovery 完成，耗时 {result.recovery_seconds}s")

    # ── Phase 5: Steady State After ──────────────────────────────────────

    def _phase5_steady_state_after(self, exp: CompositeExperiment, result: CompositeExperimentResult):
        logger.info(f"🏁 Phase 5: Steady State After")

        if self.dry_run:
            result.status = "PASSED"
            result.end_time = datetime.now(timezone.utc)
            return

        snap = self.metrics.collect_steady(
            service=exp.target_service,
            namespace=exp.target_namespace,
            window_seconds=300,
            samples=self.STEADY_SAMPLES,
            interval=15,
        )
        result.steady_state_after = snap

        all_passed = True
        for check in exp.steady_state_after:
            passed = check.is_satisfied(snap)
            if not passed:
                all_passed = False
            icon = "✅" if passed else "❌"
            desc = check.describe(snap)
            result.steady_state_after_checks.append({"passed": passed, "desc": f"{icon} {desc}"})
            logger.info(f"  {icon} {desc}")

        result.status = "PASSED" if all_passed else "FAILED"
        result.end_time = datetime.now(timezone.utc)

        slog.info("experiment_completed", experiment=exp.name, status=result.status,
                  duration_seconds=result.duration_seconds,
                  action_count=len(exp.actions))

        # Neptune 图谱反馈
        if exp.graph_feedback.enabled:
            try:
                GraphFeedback().write_back(result)
            except Exception as e:
                logger.warning(f"Neptune 反馈失败（非致命）: {e}")

    # ── 辅助方法 ──────────────────────────────────────────────────────────

    def _emergency_cleanup(self):
        """紧急清理所有活跃 action"""
        def cleanup_fn(state: ActionState):
            if state.action.backend == "fis":
                self.fis_client.stop(state.chaos_name)
                if state.fis_template_id:
                    self.fis_client.delete_template(state.fis_template_id)
            else:
                delete_type = self.cm_client.FAULT_TO_DELETE_TYPE.get(
                    state.action.type, state.action.type
                )
                ns = state.action.target_namespace or "default"
                self.cm_client.delete(state.chaos_name, chaos_type=delete_type, namespace=ns)

        self.scheduler.abort_all(cleanup_fn=cleanup_fn, timeout=30)

    def _save_and_report(self, result: CompositeExperimentResult):
        """生成报告并写 DynamoDB"""
        try:
            self.reporter.save_report(result)
        except Exception as e:
            logger.error(f"报告生成失败: {e}")
        try:
            if not self.dry_run:
                self.reporter.save_to_dynamodb(result)
        except Exception as e:
            logger.error(f"DynamoDB 写入失败: {e}")

        # 打印结果摘要
        print(f"\n{'='*60}")
        print(f"组合实验结果: {result.status}")
        print(f"实验 ID:  {result.experiment_id}")
        print(f"Actions:  {len(result.action_states)}")
        for aid, astate in result.action_states.items():
            icon = {"active": "✅", "cleaned": "🛑", "error": "❌"}.get(astate["status"], "❓")
            print(f"  {icon} {aid}: {astate['status']} {astate.get('error', '')}")
        if result.report_path:
            print(f"报告:     {result.report_path}")
        if result.abort_reason:
            print(f"原因:     {result.abort_reason}")
        print(f"{'='*60}\n")

        self.cw_metrics.publish_experiment_metrics(result)

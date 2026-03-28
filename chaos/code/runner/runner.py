"""
runner.py - 5 Phase 混沌实验执行引擎

Phase 0: Pre-flight Check      环境检查
Phase 1: Steady State Before   注入前稳态基线
Phase 2: Fault Injection       故障注入
Phase 3: Observation           观测 + Guardrails（Stop Conditions）
Phase 4: Fault Recovery        等待故障到期自动恢复，确认 Pod 健康
Phase 5: Steady State After    恢复后稳态验证 + 报告生成
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from .experiment import Experiment, MetricsSnapshot, parse_duration
from .result import ExperimentResult
from .metrics import DeepFlowMetrics
from .rca import RCATrigger
from .report import Reporter
from .graph_feedback import GraphFeedback
from .chaos_mcp import ChaosMCPClient
from .fis_backend import FISClient
from .observability import get_logger, ChaosMetrics

logger = logging.getLogger(__name__)
slog = get_logger("experiment-runner")


# ─── 异常 ───────────────────────────────────────────────────────────────────

class AbortException(Exception):
    """Stop Condition 触发，安全熔断"""


class PrefightFailure(Exception):
    """Pre-flight 检查失败，不应注入"""


# ─── 主执行引擎 ───────────────────────────────────────────────────────────────

class ExperimentRunner:
    """5 Phase 混沌实验执行引擎"""

    OBSERVE_INTERVAL = 10       # Phase3 观测间隔（秒）
    RECOVERY_POLL_INTERVAL = 15 # Phase4 恢复轮询间隔（秒）
    RECOVERY_TIMEOUT = 300      # Phase4 最长等待恢复时间（秒）
    STEADY_SAMPLES = 3          # Phase1/5 稳态采样次数

    def __init__(self, dry_run: bool = False, tags: dict = None):
        self.dry_run   = dry_run
        self.tags      = tags or {}
        self.metrics   = DeepFlowMetrics()
        self.injector  = ChaosMCPClient()     # Chaos Mesh 后端
        self.fis       = FISClient()          # FIS 后端
        self.rca       = RCATrigger()
        self.reporter  = Reporter()
        self.cw_metrics = ChaosMetrics()

    def run(self, experiment: Experiment) -> ExperimentResult:
        result = ExperimentResult(experiment=experiment)
        result.start_time = datetime.now(timezone.utc)
        result.min_success_rate = 100.0

        try:
            self._run_phase(result, "phase0", self._phase0_preflight, experiment, result)
            self._run_phase(result, "phase1", self._phase1_steady_state_before, experiment, result)
            self._run_phase(result, "phase2", self._phase2_inject, experiment, result)
            self._run_phase(result, "phase3", self._phase3_observe, experiment, result)
            self._run_phase(result, "phase4", self._phase4_recover, experiment, result)
            self._run_phase(result, "phase5", self._phase5_steady_state_after, experiment, result)

        except AbortException as e:
            result.status = "ABORTED"
            result.abort_reason = str(e)
            logger.error(f"🛑 实验熔断: {e}")
            self._emergency_cleanup(result.chaos_experiment_name, experiment.fault.type, experiment.backend)

        except PrefightFailure as e:
            result.status = "ABORTED"
            result.abort_reason = f"Pre-flight 失败: {e}"
            logger.error(f"🛑 Pre-flight 失败: {e}")

        except Exception as e:
            result.status = "ERROR"
            result.abort_reason = f"未预期异常: {e}"
            logger.error(f"💥 实验异常: {e}\n{traceback.format_exc()}")
            self._emergency_cleanup(result.chaos_experiment_name, experiment.fault.type, experiment.backend)

        finally:
            result.end_time = result.end_time or datetime.now(timezone.utc)
            self._save_and_report(result)

        return result

    # ─── Phase timing helper ─────────────────────────────────────────────────

    def _run_phase(self, result, phase_name, fn, *args):
        t0 = time.time()
        try:
            fn(*args)
        finally:
            self.cw_metrics.publish_phase_timing(result.experiment_id, phase_name, time.time() - t0)

    # ─── Phase 0：Pre-flight ──────────────────────────────────────────────────

    def _phase0_preflight(self, exp: Experiment, result: ExperimentResult):
        slog.info("phase_started", phase=0, experiment=exp.name, backend=exp.backend)
        logger.info(f"🔍 Phase 0: Pre-flight Check (backend={exp.backend})")

        from .target_resolver import TargetResolver
        resolver = TargetResolver(tags=self.tags)

        if exp.backend == "fis":
            # 确保 ARN 已解析（load_experiment 可能已解析；这里确保最新，并写入审计）
            resolver.resolve_experiment(exp)
            if not self.fis.preflight_check():
                raise PrefightFailure("FIS 服务不可用（检查 IAM 权限和区域配置）")
            slog.info("target_resolved", service=exp.target_service, backend="fis", source="resolver")
            logger.info("✅ FIS Pre-flight 通过")
            return

        # Chaos Mesh 后端：解析 Pod 目标写入审计记录，然后检查残留实验 + Pod 健康
        cm_target = resolver.resolve_chaosmesh_target(exp.target_service, exp.target_namespace)
        slog.info("target_resolved", service=exp.target_service, backend="chaosmesh",
                  pods=len(cm_target.get("pods", [])))

        active = self.injector.list_experiments()
        if active:
            names = [e.get("name", "?") for e in active if isinstance(e, dict)]
            raise PrefightFailure(f"检测到 {len(active)} 个残留实验: {names}，请先清理")

        # 检查 Pods 是否健康
        pods = self.injector.check_pods(exp.target_service, exp.target_namespace)
        if pods["total"] == 0:
            raise PrefightFailure(f"服务 {exp.target_service} 无 Running Pods（检查 label app={exp.target_service}）")
        if pods["running"] < pods["total"]:
            not_ok = [p["pod"] for p in pods["not_running"]]
            raise PrefightFailure(f"服务 {exp.target_service} 有 Pod 未就绪: {not_ok}")

        logger.info(f"✅ Pre-flight 通过: {pods['total']} pods ready")
        slog.info("phase_completed", phase=0, experiment=exp.name)

    # ─── Phase 1：Steady State Before ────────────────────────────────────────

    def _phase1_steady_state_before(self, exp: Experiment, result: ExperimentResult):
        slog.info("phase_started", phase=1, experiment=exp.name)
        logger.info("📊 Phase 1: Steady State Before")

        snap = self.metrics.collect_steady(
            service=exp.target_service,
            namespace=exp.target_namespace,
            window_seconds=60,
            samples=self.STEADY_SAMPLES,
            interval=10,
        )
        result.steady_state_before = snap
        logger.info(f"稳态基线: success_rate={snap.success_rate:.1f}%, p99={snap.latency_p99_ms:.0f}ms")

        # 验证稳态检查条件
        for check in exp.steady_state_before:
            if not check.is_satisfied(snap):
                raise PrefightFailure(
                    f"注入前稳态检查失败: {check.describe(snap)}"
                )
            logger.info(f"✅ 稳态检查通过: {check.describe(snap)}")

    # ─── Phase 2：Fault Injection ─────────────────────────────────────────────

    def _phase2_inject(self, exp: Experiment, result: ExperimentResult):
        slog.info("phase_started", phase=2, experiment=exp.name, fault_type=exp.fault.type)
        logger.info(f"💥 Phase 2: Fault Injection — {exp.fault.type} on {exp.target_service} (backend={exp.backend})")

        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过实际注入")
            result.chaos_experiment_name = "dry-run-placeholder"
            result.inject_time = datetime.now(timezone.utc)
            return

        if exp.backend == "fis":
            # FIS 后端
            fis_result = self.fis.inject(exp)
            result.chaos_experiment_name = fis_result["experiment_id"]
            result.fis_template_id = fis_result.get("template_id", "")
            result.inject_time = datetime.now(timezone.utc)
            slog.info("fault_injected", experiment=exp.name, experiment_id=fis_result["experiment_id"],
                      fault_type=exp.fault.type, backend="fis")
            logger.info(f"✅ FIS 注入完成: experiment={fis_result['experiment_id']}, template={fis_result.get('template_id', '')}")
        else:
            # Chaos Mesh 后端
            ft = exp.fault
            mcp_result = self.injector.inject(
                fault_type=ft.type,
                service=exp.target_service,
                namespace=exp.target_namespace,
                duration=ft.duration,
                mode=ft.mode,
                value=ft.value,
                latency=ft.latency,
                loss=ft.loss,
                corrupt=ft.corrupt,
                container_names=ft.container_names,
                workers=ft.workers,
                load=ft.load,
                size=ft.size,
                time_offset=ft.time_offset,
                direction=ft.direction,
                external_targets=ft.external_targets,
            )
            exp_name = self.injector.extract_experiment_name(mcp_result, ft.type)
            result.chaos_experiment_name = exp_name
            result.inject_time = datetime.now(timezone.utc)
            slog.info("fault_injected", experiment=exp.name, chaos_name=exp_name,
                      fault_type=ft.type, duration=ft.duration, backend="chaosmesh")
            logger.info(f"✅ 注入完成: {exp_name}，持续 {ft.duration}")

    # ─── Phase 3：Observation + Guardrails ───────────────────────────────────

    def _phase3_observe(self, exp: Experiment, result: ExperimentResult):
        logger.info(f"👁  Phase 3: Observation（{exp.fault.duration}，每 {self.OBSERVE_INTERVAL}s 采样）")

        duration_secs = parse_duration(exp.fault.duration)
        end_ts = time.time() + duration_secs
        rca_triggered = False

        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过观测等待")
            return

        while time.time() < end_ts:
            snap = self.metrics.collect(
                service=exp.target_service,
                namespace=exp.target_namespace,
                window_seconds=60,
            )
            result.record_snapshot(snap)

            elapsed = round(time.time() - result.inject_time.timestamp(), 0)
            logger.info(
                f"  T+{elapsed:.0f}s | success={snap.success_rate:.1f}% "
                f"p99={snap.latency_p99_ms:.0f}ms total={snap.total_requests}"
            )

            # Stop Conditions 检查
            for cond in exp.stop_conditions:
                if cond.is_triggered(snap):
                    msg = cond.describe(snap)
                    slog.error("stop_condition_triggered", experiment=exp.name,
                               condition=msg, success_rate=snap.success_rate,
                               latency_p99=snap.latency_p99_ms)
                    logger.error(f"🚨 Stop Condition 触发: {msg}")
                    # 立刻熔断
                    if exp.backend == "fis":
                        self.fis.stop(result.chaos_experiment_name)
                    else:
                        delete_type = self.injector.FAULT_TO_DELETE_TYPE.get(exp.fault.type, exp.fault.type)
                        self.injector.delete(result.chaos_experiment_name, chaos_type=delete_type, namespace=exp.target_namespace)
                    result.chaos_experiment_name = ""   # 避免 emergency_cleanup 重复删
                    raise AbortException(msg)

            # RCA 触发（仅一次，故障注入后 trigger_after 秒）
            if (exp.rca.enabled
                    and not rca_triggered
                    and result.elapsed_since_injection() >= parse_duration(exp.rca.trigger_after)):
                self._trigger_rca(exp, result)
                rca_triggered = True

            time.sleep(self.OBSERVE_INTERVAL)

        logger.info(f"✅ Phase 3 结束，Chaos Mesh 实验到期自动恢复")

    def _trigger_rca(self, exp: Experiment, result: ExperimentResult):
        logger.info(f"🧠 触发 RCA 分析: {exp.target_service}")
        inject_ts = result.inject_time.isoformat() if result.inject_time else ""
        rca_result = self.rca.trigger(exp.target_service, exp.fault.type, inject_ts)
        result.rca_result = rca_result

        if rca_result.status == "error":
            logger.warning(f"⚠️ RCA 触发但失败: {rca_result.error_message}")
        elif rca_result.status == "success":
            if exp.rca.expected_root_cause:
                result.rca_match = self.rca.verify(rca_result, exp.rca.expected_root_cause)
                icon = "✅" if result.rca_match else "❌"
                logger.info(
                    f"RCA 结果: root_cause={rca_result.root_cause!r} "
                    f"confidence={rca_result.confidence:.0%} "
                    f"match={icon}"
                )
            else:
                logger.info(
                    f"RCA 结果: root_cause={rca_result.root_cause!r} "
                    f"confidence={rca_result.confidence:.0%} "
                    f"(无期望根因，跳过匹配)"
                )

    # ─── Phase 4：Fault Recovery ─────────────────────────────────────────────

    def _phase4_recover(self, exp: Experiment, result: ExperimentResult):
        """
        Phase 4: 等待故障自动恢复，确认 Pods 恢复健康

        Chaos Mesh duration 字段负责到期删除 CR，故障自动消除。
        Phase 4 的职责：
          1. 等待所有 Pods 回到 Running/Ready 状态
          2. 记录恢复耗时
          3. 超时则告警（但不 abort，让 Phase 5 决定是否通过）
        """
        logger.info(f"♻️  Phase 4: Fault Recovery — 等待 {exp.target_service} 恢复 (backend={exp.backend})")

        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过恢复等待")
            result.recovery_seconds = 0.0
            return

        recover_start = time.time()

        # FIS 后端：先等 FIS 实验自然完成
        if exp.backend == "fis" and result.chaos_experiment_name:
            logger.info(f"等待 FIS 实验完成: {result.chaos_experiment_name}")
            final_state = self.fis.wait_for_completion(
                result.chaos_experiment_name,
                timeout=self.RECOVERY_TIMEOUT,
                poll_interval=15,
            )
            logger.info(f"FIS 实验最终状态: {final_state}")
            # 清理模板
            if hasattr(result, 'fis_template_id') and result.fis_template_id:
                self.fis.delete_template(result.fis_template_id)

        deadline = recover_start + self.RECOVERY_TIMEOUT

        # 等待所有 Pods 变为 Running + Ready。
        # FIS 后端（如 Aurora failover / Lambda 延迟）不会直接操作 K8s Pod，
        # 但目标微服务的 Pod 健康状态仍是"应用层恢复"的最终判断依据，
        # 因此 FIS 路径同样调用 check_pods() 验证 EKS 工作负载恢复。
        while time.time() < deadline:
            pods = self.injector.check_pods(exp.target_service, exp.target_namespace)
            total   = pods["total"]
            running = pods["running"]
            not_ok  = pods["not_running"]

            logger.info(f"  Pod 状态: {running}/{total} running, not_ready={[p['pod'] for p in not_ok]}")

            if total > 0 and running == total:
                elapsed = round(time.time() - recover_start, 1)
                result.recovery_seconds = elapsed
                slog.info("fault_recovered", experiment=exp.name, recovery_seconds=elapsed,
                          pods_running=running, pods_total=total)
                logger.info(f"✅ 所有 Pod 已恢复 ({running}/{total})，耗时 {elapsed}s")
                return

            time.sleep(self.RECOVERY_POLL_INTERVAL)

        # 超时
        pods = self.injector.check_pods(exp.target_service, exp.target_namespace)
        elapsed = round(time.time() - recover_start, 1)
        result.recovery_seconds = elapsed
        logger.warning(
            f"⚠️  Phase 4 超时 ({self.RECOVERY_TIMEOUT}s)，"
            f"Pod 状态: {pods['running']}/{pods['total']} running。"
            f"继续 Phase 5 验证指标..."
        )

    # ─── Phase 5：Steady State After ─────────────────────────────────────────

    def _phase5_steady_state_after(self, exp: Experiment, result: ExperimentResult):
        """
        Phase 5: 验证稳态恢复 + 生成报告

        1. 稳态指标检查（多次采样取均值）
        2. 判定实验最终状态（PASSED / FAILED）
        3. 写入报告 + DynamoDB
        4. (可选) Neptune 图谱反馈
        """
        logger.info(f"🏁 Phase 5: Steady State After — 验证 {exp.target_service} 恢复稳态")

        if self.dry_run:
            result.status = "PASSED"
            result.end_time = datetime.now(timezone.utc)
            logger.info("⚡ [dry-run] 跳过稳态验证")
            return

        # 稳态验证（窗口 5min，采样 3 次）
        snap = self.metrics.collect_steady(
            service=exp.target_service,
            namespace=exp.target_namespace,
            window_seconds=300,
            samples=self.STEADY_SAMPLES,
            interval=15,
        )
        result.steady_state_after = snap
        logger.info(f"恢复后稳态: success_rate={snap.success_rate:.1f}%, p99={snap.latency_p99_ms:.0f}ms")

        # 逐项验证 steady_state.after 条件
        all_passed = True
        for check in exp.steady_state_after:
            passed = check.is_satisfied(snap)
            if not passed:
                all_passed = False
            icon = "✅" if passed else "❌"
            desc = check.describe(snap)
            result.steady_state_after_checks.append({
                "passed": passed,
                "desc":   f"{icon} {desc}",
            })
            logger.info(f"  {icon} 稳态检查: {desc}")

        # 判定最终状态
        result.status = "PASSED" if all_passed else "FAILED"
        result.end_time = datetime.now(timezone.utc)

        slog.info("experiment_completed", experiment=exp.name, status=result.status,
                  duration_seconds=result.duration_seconds,
                  recovery_seconds=result.recovery_seconds,
                  min_success_rate=result.min_success_rate)

        icon = "✅ PASSED" if all_passed else "❌ FAILED"
        logger.info(
            f"{icon} | 实验 {result.experiment_id} 完成 "
            f"| 耗时 {result.duration_seconds:.0f}s "
            f"| 恢复 {result.recovery_seconds:.0f}s "
            f"| 最低成功率 {result.min_success_rate:.1f}%"
        )

        # Neptune 图谱反馈
        if exp.graph_feedback.enabled:
            try:
                GraphFeedback().write_back(result)
            except Exception as e:
                logger.warning(f"Neptune 图谱反馈失败（非致命）: {e}")

    # ─── 报告 & 清理 ──────────────────────────────────────────────────────────

    def _save_and_report(self, result: ExperimentResult):
        """无论成功失败都生成报告并写 DynamoDB"""
        try:
            self.reporter.save_report(result)
        except Exception as e:
            logger.error(f"报告生成失败: {e}")
        try:
            if not self.dry_run:
                self.reporter.save_to_dynamodb(result)
        except Exception as e:
            logger.error(f"DynamoDB 写入失败: {e}")

        print(f"\n{'='*60}")
        print(f"实验结果: {result.status}")
        print(f"实验 ID:  {result.experiment_id}")
        if result.report_path:
            print(f"报告:     {result.report_path}")
        if result.abort_reason:
            print(f"原因:     {result.abort_reason}")
        print(f"{'='*60}\n")

        self.cw_metrics.publish_experiment_metrics(result)

    def _emergency_cleanup(self, experiment_name: str, fault_type: str = "",
                           backend: str = "chaosmesh"):
        """紧急清理：删除实验，避免故障持续"""
        if experiment_name and not self.dry_run:
            logger.warning(f"🧹 紧急清理: {experiment_name} (backend={backend})")
            if backend == "fis":
                self.fis.stop(experiment_name)
            else:
                chaos_type = self.injector.FAULT_TO_DELETE_TYPE.get(fault_type, fault_type)
                self.injector.delete(experiment_name, chaos_type=chaos_type)

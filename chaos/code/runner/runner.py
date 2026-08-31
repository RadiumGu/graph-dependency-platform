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
import os
import signal
import sys
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
        # 忽略 SIGPIPE（父进程管道断开不影响实验运行）
        if hasattr(signal, "SIGPIPE"):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    def run(self, experiment: Experiment) -> ExperimentResult:
        # 组合实验委托给 CompositeRunner
        from .experiment import CompositeExperiment
        if isinstance(experiment, CompositeExperiment):
            from .composite_runner import CompositeRunner
            cr = CompositeRunner(dry_run=self.dry_run, tags=self.tags)
            return cr.run(experiment)

        result = ExperimentResult(experiment=experiment)
        result.start_time = datetime.now(timezone.utc)
        result.min_success_rate = 100.0
        log_collector = None

        try:
            self._run_phase(result, "phase0", self._phase0_preflight, experiment, result)
            self._run_phase(result, "phase1", self._phase1_steady_state_before, experiment, result)
            self._run_phase(result, "phase2", self._phase2_inject, experiment, result)

            # Phase 2 完成后启动后台日志采集（best-effort，non-fatal）
            if not self.dry_run:
                try:
                    from .log_collector import PodLogCollector
                    log_collector = PodLogCollector(
                        service=experiment.target_service,
                        namespace=experiment.target_namespace,
                        since="1m",
                    )
                    log_collector.start_background()
                except Exception as e:
                    logger.warning(f"日志采集启动失败（非致命）: {e}")

            self._run_phase(result, "phase3", self._phase3_observe, experiment, result)
            self._run_phase(result, "phase4", self._phase4_recover, experiment, result)

            # Phase 4 结束后停止日志采集
            if log_collector is not None:
                try:
                    log_result = log_collector.stop_and_collect()
                    result.log_collection = log_result
                except Exception as e:
                    logger.warning(f"日志采集停止失败（非致命）: {e}")

            self._run_phase(result, "phase5", self._phase5_steady_state_after, experiment, result)

        except AbortException as e:
            result.status = "ABORTED"
            result.abort_reason = str(e)
            logger.error(f"🛑 实验熔断: {e}")
            if log_collector is not None:
                try:
                    log_result = log_collector.stop_and_collect()
                    result.log_collection = log_result
                except Exception:
                    pass
            self._emergency_cleanup(result.chaos_experiment_name, experiment.fault.type, experiment.backend)

        except PrefightFailure as e:
            result.status = "ABORTED"
            result.abort_reason = f"Pre-flight 失败: {e}"
            logger.error(f"🛑 Pre-flight 失败: {e}")

        except BrokenPipeError:
            # 父进程管道断开（如 Slack agent 超时），实验本身已完成
            # 状态以实际运行结果为准，不强制改为 ERROR
            logger.warning("⚠️  stdout 管道断开（父进程已退出），实验结果保持当前状态")
            try:
                sys.stdout = open(os.devnull, "w")
                sys.stderr = open(os.devnull, "w")
            except Exception:
                pass

        except Exception as e:
            result.status = "ERROR"
            result.abort_reason = f"未预期异常: {e}"
            logger.error(f"💥 实验异常: {e}\n{traceback.format_exc()}")
            if log_collector is not None:
                try:
                    log_result = log_collector.stop_and_collect()
                    result.log_collection = log_result
                except Exception:
                    pass
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

        # ── PolicyGuard pre-execution check ──
        self._run_policy_guard(exp, result)

        from .target_resolver import TargetResolver
        resolver = TargetResolver(tags=self.tags)

        if exp.backend in ("fis", "fis-scenario"):
            # 确保 ARN 已解析（load_experiment 可能已解析；这里确保最新，并写入审计）
            if exp.backend == "fis":
                resolver.resolve_experiment(exp)
            if not self.fis.preflight_check():
                raise PrefightFailure("FIS 服务不可用（检查 IAM 权限和区域配置）")
            slog.info("target_resolved", service=exp.target_service, backend=exp.backend, source="resolver")
            logger.info(f"✅ FIS Pre-flight 通过 (backend={exp.backend})")
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
        # T-214h：存下基线，Phase 5 用 restarts 差值判断注入是否打伤了 Pod。
        # 必须在这里存 —— Phase 0 是唯一确定「注入还没发生」的时点。
        result.target_pods_before = pods
        logger.info(f"   Pod 重启基线: restarts={pods.get('restarts')} "
                    f"({pods.get('per_pod_restarts')})")
        slog.info("phase_completed", phase=0, experiment=exp.name)

    # ─── PolicyGuard ─────────────────────────────────────────────────────────

    def _run_policy_guard(self, exp: Experiment, result: ExperimentResult):
        """Pre-execution policy check. Deny → abort experiment (fail-closed)."""
        try:
            from policy.factory import make_policy_guard
        except ImportError:
            logger.warning("PolicyGuard not available — skipping")
            return

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        experiment_dict = {
            "name": exp.name,
            "fault_type": exp.fault.type if hasattr(exp, 'fault') and hasattr(exp.fault, 'type') else "unknown",
            "target_namespace": exp.target_namespace,
            "target_service": exp.target_service,
            "duration_sec": int(exp.fault.duration.rstrip('smh').split('.')[0]) if hasattr(exp, 'fault') and hasattr(exp.fault, 'duration') else 0,
            "blast_radius": getattr(exp, "blast_radius", "service"),
        }
        context_dict = {
            "current_time": now,
            "environment": os.environ.get("ENVIRONMENT", "staging"),
            "recent_incidents": [],
            "recent_experiments": [],
        }

        guard = make_policy_guard()
        verdict = guard.evaluate(experiment_dict, context_dict)

        slog.info("policy_guard_result",
                  experiment=exp.name,
                  decision=verdict["decision"],
                  matched_rules=verdict.get("matched_rules", []),
                  latency_ms=verdict.get("latency_ms", 0),
                  engine=verdict.get("engine", "unknown"))

        result.policy_guard = verdict  # attach for reporting

        if verdict["decision"] == "deny":
            rules = ", ".join(verdict.get("matched_rules", []))
            raise PrefightFailure(
                f"PolicyGuard DENIED: {verdict.get('reasoning', 'no reason')[:200]} "
                f"[rules: {rules}]"
            )

        logger.info(f"✅ PolicyGuard: ALLOW (confidence={verdict.get('confidence', 0):.2f})")

    # ─── Phase 1：Steady State Before ────────────────────────────────────────

    def _phase1_steady_state_before(self, exp: Experiment, result: ExperimentResult):
        slog.info("phase_started", phase=1, experiment=exp.name)
        logger.info("📊 Phase 1: Steady State Before")

        snap = self.metrics.collect_steady(
            service=self._target_metrics_name(exp),
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

        # ── 观测方基线（T-210）─────────────────────────────────────────────
        # 验证边 A -[X]-> B 必须在 B 注入、观测 A。上面采的是注入目标自己，
        # 「打断 B 之后 B 是否退化」近乎恒真，不构成任何边的证据。
        self._collect_observer_baselines(exp, result)

    def _collect_observer_baselines(self, exp: Experiment, result: ExperimentResult):
        """
        为每个观测方采一份基线。

        刻意**不**因观测方无流量而让实验失败：那是数据质量问题，
        应由 edge_verification 判成 inconclusive，而不是把实验判死。
        判 refuted 才是有害的（会删掉真实边），判 inconclusive 只是没结论。
        """
        observers = getattr(exp, "observation_targets", None) or []
        if not observers:
            logger.info("ℹ️  未声明观测方（observation_targets 为空）—— "
                        "本实验不能用于边验证，只能做稳态回归")
            return

        for obs in observers:
            ns = obs.namespace or exp.target_namespace
            try:
                osnap = self.metrics.collect_steady(
                    service=obs.service,
                    namespace=ns,
                    window_seconds=60,
                    samples=self.STEADY_SAMPLES,
                    interval=10,
                )
            except Exception as e:
                logger.warning(f"⚠️  观测方 {obs.service} 基线采集失败: {e!r} —— 该边只能判 inconclusive")
                continue

            result.record_observer_baseline(obs.service, osnap)
            enough = (osnap.total_requests or 0) >= obs.min_baseline_requests
            icon = "✅" if enough else "⚠️"
            logger.info(
                f"{icon} 观测方基线 {obs.service} ({obs.edge_label}): "
                f"success={osnap.success_rate:.1f}% total={osnap.total_requests} "
                f"(下限 {obs.min_baseline_requests})"
            )
            if not enough:
                # 这一条是假阴性防线：metrics.collect() 无数据时 fallback
                # success_rate=100.0 / total_requests=0 —— 零流量和健康完全一样。
                logger.warning(
                    f"   观测方 {obs.service} 基线流量不足 —— "
                    f"边 {obs.service} -[{obs.edge_label}]-> {exp.target_service} "
                    f"只能判 inconclusive，**不得**判 refuted"
                )

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
            # FIS 后端（单 action）
            fis_result = self.fis.inject(exp)
            result.chaos_experiment_name = fis_result["experiment_id"]
            result.fis_template_id = fis_result.get("template_id", "")
            result.inject_time = datetime.now(timezone.utc)
            slog.info("fault_injected", experiment=exp.name, experiment_id=fis_result["experiment_id"],
                      fault_type=exp.fault.type, backend="fis")
            logger.info(f"✅ FIS 注入完成: experiment={fis_result['experiment_id']}, template={fis_result.get('template_id', '')}")
        elif exp.backend == "fis-scenario":
            # FIS Scenario Library（多 action 复合场景）
            fis_result = self.fis.inject_scenario(exp)
            result.chaos_experiment_name = fis_result["experiment_id"]
            result.fis_template_id = fis_result.get("template_id", "")
            result.inject_time = datetime.now(timezone.utc)
            slog.info("fault_injected", experiment=exp.name, experiment_id=fis_result["experiment_id"],
                      fault_type=exp.fault.type, backend="fis-scenario")
            logger.info(f"✅ FIS scenario 注入完成: experiment={fis_result['experiment_id']}, template={fis_result.get('template_id', '')}")
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
                service=self._target_metrics_name(exp),
                namespace=exp.target_namespace,
                window_seconds=60,
            )
            result.record_snapshot(snap)

            # ── 观测方采样（T-210）——这才是边的证据 ────────────────────────
            self._collect_observer_snapshots(exp, result)

            elapsed = round(time.time() - result.inject_time.timestamp(), 0)
            logger.info(
                f"  T+{elapsed:.0f}s | success={snap.success_rate:.1f}% "
                f"p99={snap.latency_p99_ms:.0f}ms total={snap.total_requests}"
            )

            # Stop Conditions 检查（T-214b：按护栏对象分别求值）
            for cond, subject, subj_snap in self._stop_condition_subjects(exp, result, snap):
                if cond.is_triggered(subj_snap):
                    msg = f"{subject}: {cond.describe(subj_snap)}"
                    slog.error("stop_condition_triggered", experiment=exp.name,
                               condition=msg, success_rate=snap.success_rate,
                               latency_p99=snap.latency_p99_ms)
                    logger.error(f"🚨 Stop Condition 触发: {msg}")
                    # 立刻熔断
                    if exp.backend in ("fis", "fis-scenario"):
                        self.fis.stop(result.chaos_experiment_name)
                    else:
                        delete_type = self.injector.FAULT_TO_DELETE_TYPE.get(exp.fault.type, exp.fault.type)
                        # 全部用关键字传参。ChaosMCPClient.delete(chaos_type, name, namespace)
                        # 的第一个位置参数是 chaos_type 而不是 name —— 原先按位置传实验名、
                        # 再用关键字传 chaos_type，会撞成
                        # "got multiple values for argument 'chaos_type'"。
                        # 2026-08-31 实测后果：stop condition 触发时清理直接抛异常，
                        # HTTPChaos CRD 留在集群里继续生效，且强删仍在生效的 CRD 会把
                        # tproxy 拦截残留在目标 Pod 的网络命名空间里 —— 容器重启清不掉，
                        # 两个被命中的 Pod 进入 CrashLoopBackOff，只能删 Pod 重建。
                        self.injector.delete(
                            chaos_type=delete_type,
                            name=result.chaos_experiment_name,
                            namespace=exp.target_namespace,
                        )
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
        self._log_observer_evidence(exp, result)

    def _stop_condition_subjects(self, exp: Experiment, result: ExperimentResult,
                                 target_snap: MetricsSnapshot):
        """
        产出 (条件, 主体名, 该主体的最新快照) 三元组。

        T-214b：边验证实验里注入目标**本来就该失败**，所以护栏必须能挂在观测方上。
        `target='injection'` 保持既有行为（注入目标侧只留极低地板防注入失控）；
        `any_observer` / `observer:<svc>` 看调用方，那才是真正要防的附带损害。

        观测方还没有采样点时**跳过**该条件 —— 不能拿缺失当触发，
        否则实验一开始就会被自己的护栏打断（与不变量 7 同向：缺数据不等于坏了）。
        """
        for cond in exp.stop_conditions:
            if cond.applies_to_injection():
                yield cond, f"注入目标 {exp.target_service}", target_snap
                continue
            scope = cond.observer_scope()
            if scope is None:
                # target 写了无法识别的值：按注入目标处理并告警，不静默丢弃条件
                logger.warning(
                    f"⚠️ stop_condition target={cond.target!r} 无法识别，"
                    f"按 injection 处理（合法值：injection / any_observer / observer:<svc>）")
                yield cond, f"注入目标 {exp.target_service}", target_snap
                continue
            names = ([o.service for o in (getattr(exp, "observation_targets", None) or [])]
                     if scope == "*" else [scope])
            for svc in names:
                snaps = result.observer_snapshots.get(svc) or []
                if not snaps:
                    continue          # 无采样点：跳过，不当触发
                yield cond, f"观测方 {svc}", snaps[-1]

    def _collect_observer_snapshots(self, exp: Experiment, result: ExperimentResult):
        """注入期为每个观测方采一个点。单个观测方失败不影响其余，也不中断实验。"""
        for obs in (getattr(exp, "observation_targets", None) or []):
            ns = obs.namespace or exp.target_namespace
            try:
                osnap = self.metrics.collect(
                    service=obs.service, namespace=ns, window_seconds=60,
                )
            except Exception as e:
                logger.warning(f"⚠️  观测方 {obs.service} 采样失败: {e!r}")
                continue
            result.record_observer_snapshot(obs.service, osnap)

    def _log_observer_evidence(self, exp: Experiment, result: ExperimentResult):
        """
        打印逐条边的证据。这是把「实验跑完了」变成「某条边被检验了」的地方。

        注意这里只**呈现**证据，不做判定 —— 阈值与 confirmed/refuted/inconclusive
        的划分全部在 graph_contract 的 edge_verification 里（单一声明，避免漂移）。
        """
        ev = result.observer_evidence()
        if not ev:
            return
        logger.info("🔎 观测方证据（边验证输入）：")
        for obs in (getattr(exp, "observation_targets", None) or []):
            e = ev.get(obs.service)
            if not e:
                logger.info(f"   {obs.service} -[{obs.edge_label}]-> {exp.target_service}: 无数据 → inconclusive")
                continue
            deg = e["degradation_rate"]
            thr = e.get("throughput_drop_pct")
            eff = e.get("effective_degradation")
            fmt = lambda v: "n/a" if v is None else f"{v:.2f}"
            logger.info(
                f"   {obs.service} -[{obs.edge_label}]-> {exp.target_service}: "
                f"成功率退化={fmt(deg)}pp 吞吐塌陷={fmt(thr)}% 合成={fmt(eff)} "
                f"基线请求={e['baseline_total_requests']} 谷值请求={e.get('min_requests')} "
                f"采样={e['samples']} usable={e['usable']}"
            )

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

    def _verify_edges(self, exp: Experiment, result: ExperimentResult):
        """
        用本次注入的**观测方**证据，对指向注入目标的依赖边逐条判定并写回图谱。

        为什么必须在这里而不是在 graph_feedback 里：
        `graph_feedback._update_calls_edges` 写的是注入目标的聚合评分，
        而边验证需要「哪个观测方对应哪条边」的一一对应。原实现把同一个判定
        写给注入目标的所有出入边，等于伪造验证证据。

        判定阈值全部在 profiles/graph_contract.yaml 的 `edge_verification`
        （与 ETL 写入门禁共用一份声明），这里只喂数据、不定阈值。
        """
        observers = getattr(exp, "observation_targets", None) or []
        if not observers:
            return
        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过边验证写回")
            return

        try:
            from .edge_verification import (
                candidate_edges, verify_edge, write_verdict, resolve_graph_name,
            )
        except Exception as e:
            logger.warning(f"边验证模块不可用（非致命）: {e!r}")
            return

        try:
            cands = candidate_edges(exp.target_service)
        except Exception as e:
            logger.warning(f"候选边查询失败（非致命）: {e!r}")
            return

        ev = result.observer_evidence()
        by_observer = {c.get('observer'): c for c in cands if c.get('observer')}
        written = 0

        for obs in observers:
            # 观测方也必须解析成图谱规范名：实验里写的是 K8s 服务名
            # （list-adoptions），图谱里是 petlistadoptions。少了这一步，
            # by_observer 查不到、日志说「无候选边」，而边其实在图里。
            obs_graph_name = resolve_graph_name(obs.service)
            cand = by_observer.get(obs_graph_name)
            if cand is None:
                logger.warning(
                    f"   图谱中无 {obs_graph_name} -> {exp.target_service} 的候选边，跳过"
                    f"（观测方 K8s 名 {obs.service}，候选边源端点有："
                    f"{sorted(by_observer)}）")
                continue

            e = ev.get(obs.service) or {}
            base_req = e.get('baseline_total_requests') or 0
            # 用**合成**退化率：成功率下降与吞吐塌陷取 max。
            # abort 类故障不产生 response 行，成功率对它是盲的（实测注入目标
            # 成功率全程 100% 而请求量 -97%），只喂成功率会把生效的注入判成没影响。
            deg = e.get('effective_degradation')
            # 注入期请求量取各采样窗口的**最大值**而非求和：每个快照本身是一个
            # 60s 窗口计数，求和会因窗口重叠而虚高。取 max 得到与基线同量纲的
            # 代表性窗口量，且在真的零流量时仍然是 0 —— 偏向「判不了」而不是
            # 「判边不存在」，与不变量 7 同向。
            # 只取**采集成功**的采样点（ok=True）。失败的采样点是 (100%, 0 requests)
            # 的 fallback，混进来会污染请求量判据。
            snaps = [s for s in result.observer_snapshots.get(obs.service, [])
                     if getattr(s, 'ok', True)]
            inj_req = max((s.total_requests or 0) for s in snaps) if snaps else 0

            try:
                verdict = verify_edge(
                    edge=cand,
                    observer_baseline_requests=int(base_req),
                    observer_injected_requests=int(inj_req),
                    observer_degradation_pct=float(deg if deg is not None else 0.0),
                    experiment_id=result.experiment_id,
                    # 证据通道：纯吞吐证据不足以单独判 confirmed，见
                    # graph_confidence.classify_intervention 的 docstring
                    evidence_channel=result.observer_evidence_channel(obs.service),
                )
            except Exception as ex:
                logger.warning(f"边判定失败 {obs.service}: {ex!r}")
                continue

            logger.info(
                f"🧪 {obs.service} -[{verdict.get('label')}]-> {exp.target_service}: "
                f"{verdict['status']} (置信度 {verdict['confidence']:.3f}) — {verdict['reason']}"
            )
            try:
                if write_verdict(verdict):
                    written += 1
            except Exception as ex:
                # 这条路径历史上 100% 静默失败过（property(single,...) 在边属性上非法，
                # 异常被 except 吞成 logger.error）。所以失败必须计数并显式呈现。
                logger.error(f"❌ 边判定写回失败 {obs.service}: {ex!r}")

        logger.info(f"🧪 边验证写回：{written}/{len(observers)} 条成功")
        if written == 0 and observers:
            logger.error(
                "❌ 声明了观测方但零条写回成功 —— 这正是历史上被静默吞掉 21 次的症状，"
                "不要当成「没有边可写」放过"
            )

    # ─── Phase 4：Fault Recovery ─────────────────────────────────────────────

    def _phase4_recover(self, exp: Experiment, result: ExperimentResult):
        """
        Phase 4: 等待故障自动恢复，确认 Pods 恢复健康

        ⚠️ 原 docstring 写着「Chaos Mesh duration 字段负责到期删除 CR，故障自动消除」——
        **这个假设是错的**（2026-08-31 实测）。duration 到期后故障停止生效，
        但 **CRD 对象仍然存在**，而 runner 只在熔断/异常路径删 CRD，
        正常完成路径从不删。实测后果：一个 PASSED 的实验结束后 `httpchaos` 仍有 1 条，
        两个被注入过的 Pod 随后又从 2/2 退回 1/2（tproxy 仍挂在 netns 上），
        而 Phase 5 在这之前采样、显示 100% 通过 —— 污染被完全掩盖，
        并且会成为下一次实验的稳态基线污染源。

        Phase 4 的职责：
          1. **显式删除 Chaos Mesh CRD**（不依赖「到期自动清理」这个错假设）
          2. 等待所有 Pods 回到 Running/Ready 状态
          3. 记录恢复耗时
          4. 超时则告警（但不 abort，让 Phase 5 决定是否通过）
        """
        logger.info(f"♻️  Phase 4: Fault Recovery — 等待 {exp.target_service} 恢复 (backend={exp.backend})")

        if self.dry_run:
            logger.info("⚡ [dry-run] 跳过恢复等待")
            result.recovery_seconds = 0.0
            return

        recover_start = time.time()

        # ── 正常完成路径也必须删 CRD（2026-08-31 实测缺陷）────────────────────
        if exp.backend == "chaosmesh" and result.chaos_experiment_name:
            try:
                delete_type = self.injector.FAULT_TO_DELETE_TYPE.get(
                    exp.fault.type, exp.fault.type)
                self.injector.delete(
                    chaos_type=delete_type,
                    name=result.chaos_experiment_name,
                    namespace=exp.target_namespace,
                )
                logger.info(f"🧹 已删除 Chaos Mesh CRD: {result.chaos_experiment_name}")
                result.chaos_experiment_name = ""   # 避免 emergency_cleanup 重复删
            except Exception as e:
                # 删不掉必须显式报错：残留会污染下一次实验的稳态基线，
                # 而 Phase 5 采样在残留生效之前，看不出问题。
                logger.error(
                    f"❌ 删除 Chaos Mesh CRD 失败: {e!r} —— "
                    f"残留 CRD 会让 tproxy 继续挂在 Pod netns 上并污染后续实验，"
                    f"请手工 kubectl delete 并遍历全部 CRD 类型确认归零"
                )

        # FIS 后端：先等 FIS 实验自然完成
        if exp.backend in ("fis", "fis-scenario") and result.chaos_experiment_name:
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
        # FIS infra 实验（Lambda / EBS / subnet）可能没有直接对应的 K8s Pods，
        # 先检查是否有 Pods 需要恢复；如果 total==0 则跳过 Pod 恢复循环。
        initial_pods = self.injector.check_pods(exp.target_service, exp.target_namespace)
        if initial_pods["total"] == 0:
            elapsed = round(time.time() - recover_start, 1)
            result.recovery_seconds = elapsed
            logger.info(f"✅ 无 K8s Pods 需要恢复（FIS infra 实验），耗时 {elapsed}s")
            return

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


    def _target_metrics_name(self, exp: Experiment) -> str:
        """注入目标在 **DeepFlow SLI 口径**下的名字。

        这里有三套命名空间，都源自 profiles/petsite.yaml 的同一张 services 表，
        但取值不同，混用会静默出错（2026-08-31 实测）：

            kubectl label selector   app=pethistory-deployment   ← 注入用
            DeepFlow request_domain  %pethistory%                ← SLI 用
            图谱 Microservice.name    pethistory                  ← 候选边用

        实测 `metrics.collect('pethistory-deployment')` 返回 **0 请求**，
        而 `collect('pethistory')` 返回 26 请求。0 请求会走 fallback 返回
        `success_rate=100.0`，于是稳态门 `>= 95%` 被一个**假的 100%** 通过 ——
        与「零流量和健康在指标上分不开」是同一个缺陷家族。

        解析用的是 T-214e 引入的同一张别名表，不另立映射。
        """
        try:
            from .edge_verification import resolve_graph_name
            resolved = resolve_graph_name(exp.target_service)
        except Exception:
            return exp.target_service
        return resolved or exp.target_service

    def _check_target_pod_health(self, exp: Experiment, result: ExperimentResult) -> bool:
        """T-214h：注入目标的 Pod 是否被这次实验打伤。返回 False 即判 FAILED。

        两条判据，缺一不可：

        1. **readiness** —— 现在还有 Pod 不 Ready，说明损伤仍在持续。
        2. **restarts 差值** —— Phase 0 基线到现在容器重启数增加了。
           这一条才是主判据：readiness 有滞后（实测 Phase 4 报 2/2 之后
           2.5 分钟才退化），而重启就发生在注入期间，此刻已经计入。

        差值判据在基线缺失时**不判 FAILED 而是告警**：`check_pods` 失败会返回
        `restarts=None`，拿 None 当 0 会让判据静默通过；而拿它当损伤又会把
        「没测到」误报成「打伤了」。两者都不对，所以显式区分第三种情况。
        """
        pods = self.injector.check_pods(exp.target_service, exp.target_namespace)
        result.target_pods_after = pods
        before = result.target_pods_before or {}
        r_before, r_after = before.get("restarts"), pods.get("restarts")
        ok = True

        # 判据 1：readiness
        if pods.get("total", 0) == 0:
            msg = (f"❌ 目标服务 {exp.target_service} 查不到任何 Pod"
                   f"（label app={exp.target_service}）")
            result.pod_damage.append(msg)
            logger.error(f"  {msg}")
            ok = False
        elif pods["running"] < pods["total"]:
            bad = [f"{p['pod']}(phase={p['phase']} ready={p['ready']} restarts={p.get('restarts')})"
                   for p in pods["not_running"]]
            msg = (f"❌ 注入后仍有 Pod 未就绪 {pods['running']}/{pods['total']}: {bad}。"
                   f"处置：kubectl delete pod -n {exp.target_namespace} "
                   f"-l app={exp.target_service} 让 ReplicaSet 重建 —— "
                   f"abort 的 tproxy 残留在 Pod netns 里，容器重启清不掉。")
            result.pod_damage.append(msg)
            logger.error(f"  {msg}")
            ok = False
        else:
            logger.info(f"  ✅ Pod 检查: {pods['running']}/{pods['total']} ready")

        # 判据 2：重启差值（主判据）
        if r_before is None or r_after is None:
            logger.warning(
                "  ⚠️ Pod 重启基线或现值缺失（before=%s after=%s），跳过重启差值判据。"
                "这不是通过，是没测到 —— 请人工核对 kubectl get pods 的 RESTARTS 列。",
                r_before, r_after)
            result.pod_damage.append(
                f"⚠️ 重启差值未能判定（before={r_before} after={r_after}），需人工核对")
        elif r_after > r_before:
            delta = r_after - r_before
            msg = (f"❌ 注入导致容器重启 +{delta} 次（{r_before} → {r_after}，"
                   f"逐 Pod: {pods.get('per_pod_restarts')}）。"
                   f"这是 abort 打断 liveness 探针的已知代价：kubelet 杀掉容器重启，"
                   f"而 tproxy 残留在 Pod netns（属 sandbox 不属容器）故重启清不掉。"
                   f"处置：删 Pod 重建。不修就会把污染带进下一轮实验的基线。")
            result.pod_damage.append(msg)
            logger.error(f"  {msg}")
            ok = False
        else:
            logger.info(f"  ✅ Pod 重启数未增加（{r_before} → {r_after}）")

        return ok

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
            service=self._target_metrics_name(exp),
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

        # ── T-214h：Pod readiness + 重启差值 ────────────────────────────────
        # 为什么 SLI 全绿也不够：实测 2026-08-31 两轮 abort 注入，SLI 报 100%、
        # 稳态检查全过、实验判 PASSED，但被注入的两个 Pod 都进了重启循环、
        # 持续 1/2 Ready，只能人工删 Pod 重建。SLI 之所以看不见，是因为 HPA
        # 新拉的干净 Pod 在撑着服务 —— **服务健康 ≠ 实验没造成损伤**。
        # 一个报 PASSED 却留下坏 Pod 的实验，会让下一轮实验的基线带着污染开始。
        if exp.backend not in ("fis", "fis-scenario"):
            all_passed = self._check_target_pod_health(exp, result) and all_passed

        # 判定最终状态
        result.status = "PASSED" if all_passed else "FAILED"
        result.end_time = datetime.now(timezone.utc)

        # 数据完备性检查：PASSED 但数据不足 → 降级为 INCONCLUSIVE
        if result.status == "PASSED" and not result.is_conclusive():
            result.status = "INCONCLUSIVE"
            logger.warning(
                f"⚠️  实验 {result.experiment_id} 稳态检查全部通过但数据不完备 "
                f"(data_quality={result.data_quality})，降级为 INCONCLUSIVE"
            )

        slog.info("experiment_completed", experiment=exp.name, status=result.status,
                  duration_seconds=result.duration_seconds,
                  recovery_seconds=result.recovery_seconds,
                  min_success_rate=result.min_success_rate)

        icon = "✅ PASSED" if result.status == "PASSED" else ("⚠️ INCONCLUSIVE" if result.status == "INCONCLUSIVE" else "❌ FAILED")
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

        # 逐条依赖边验证（T-210 / DoD-3）：把观测方证据落回图谱
        self._verify_edges(exp, result)

        # Phase A2: 同步实验记录到 Neptune ChaosExperiment 节点
        try:
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
            from neptune_sync import write_experiment
            write_experiment({
                'experiment_id': result.experiment_id,
                'target_service': exp.target_service,
                'fault_type': exp.fault.type,
                'result': result.status.lower(),
                'recovery_time_sec': int(result.recovery_seconds or 0),
                'degradation_rate': round(result.degradation_rate() / 100.0, 4),
                'data_quality': result.data_quality,
                'timestamp': result.end_time.isoformat() if result.end_time else '',
            })
            logger.info(f"Experiment {result.experiment_id} synced to Neptune")
        except Exception as e:
            logger.warning(f"Neptune sync failed (non-fatal): {e}")

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

        self._safe_print(f"\n{'='*60}")
        self._safe_print(f"实验结果: {result.status}")
        self._safe_print(f"实验 ID:  {result.experiment_id}")
        if result.report_path:
            self._safe_print(f"报告:     {result.report_path}")
        if result.abort_reason:
            self._safe_print(f"原因:     {result.abort_reason}")
        if getattr(result, 'log_collection', None) is not None:
            self._safe_print(f"日志采集: {result.log_collection.summary()}")
            if result.log_collection.error_summary:
                self._safe_print(f"错误分类: {result.log_collection.error_summary}")
        self._safe_print(f"{'='*60}\n")

        self.cw_metrics.publish_experiment_metrics(result)

    @staticmethod
    def _safe_print(msg: str) -> None:
        """print wrapper — 管道断开时静默忽略"""
        try:
            print(msg, flush=True)
        except (BrokenPipeError, OSError):
            pass

    def _emergency_cleanup(self, experiment_name: str, fault_type: str = "",
                           backend: str = "chaosmesh"):
        """紧急清理：删除实验，避免故障持续"""
        if experiment_name and not self.dry_run:
            logger.warning(f"🧹 紧急清理: {experiment_name} (backend={backend})")
            if backend in ("fis", "fis-scenario"):
                self.fis.stop(experiment_name)
            else:
                chaos_type = self.injector.FAULT_TO_DELETE_TYPE.get(fault_type, fault_type)
                # 同 stop-condition 路径：必须关键字传参，见那里的注释
                self.injector.delete(chaos_type=chaos_type, name=experiment_name)

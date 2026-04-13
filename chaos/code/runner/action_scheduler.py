"""
action_scheduler.py — 组合实验时序编排调度器

解析 FaultAction.start 字段语法，生成 ExecutionPlan，
通过 ThreadPoolExecutor + threading.Event 实现并行/串行/延迟调度。

start 字段语法:
  immediate              → 立即启动（默认）
  after:<action_id>      → 指定 action 启动后立即启动
  after:<action_id>:<delay> → 指定 action 启动后延迟启动（如 after:net-delay:30s）
  after_complete:<action_id> → 指定 action 执行结束后再启动
  delay:<duration>       → 实验开始后延迟启动（如 delay:60s）
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .experiment import (
    CompositeExperiment, FaultAction, Schedule, Wave, parse_duration,
)

logger = logging.getLogger(__name__)


# ─── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class ActionState:
    """单个 action 的运行时状态"""
    action: FaultAction
    started: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    chaos_name: str = ""           # ChaosMesh 实验名 or FIS experiment_id
    fis_template_id: str = ""      # FIS 模板 ID（清理用）
    status: str = "pending"        # pending / injecting / active / cleaned / error
    error: str = ""
    inject_time: Optional[datetime] = None


@dataclass
class StartSpec:
    """解析后的 start 字段"""
    mode: str          # "immediate" | "after" | "after_complete" | "delay"
    depends_on: str = ""   # 依赖的 action_id（after / after_complete）
    delay_seconds: float = 0.0


def parse_start_spec(raw: str) -> StartSpec:
    """
    解析 start 字段语法。

    >>> parse_start_spec("immediate")
    StartSpec(mode='immediate')
    >>> parse_start_spec("after:net-delay:30s")
    StartSpec(mode='after', depends_on='net-delay', delay_seconds=30.0)
    >>> parse_start_spec("delay:60s")
    StartSpec(mode='delay', delay_seconds=60.0)
    """
    raw = raw.strip()

    if raw == "immediate" or not raw:
        return StartSpec(mode="immediate")

    if raw.startswith("after_complete:"):
        parts = raw.split(":", 2)
        dep_id = parts[1]
        delay = 0.0
        if len(parts) == 3:
            delay = float(parse_duration(parts[2]))
        return StartSpec(mode="after_complete", depends_on=dep_id, delay_seconds=delay)

    if raw.startswith("after:"):
        parts = raw.split(":", 2)
        dep_id = parts[1]
        delay = 0.0
        if len(parts) == 3:
            delay = float(parse_duration(parts[2]))
        return StartSpec(mode="after", depends_on=dep_id, delay_seconds=delay)

    if raw.startswith("delay:"):
        delay_str = raw.split(":", 1)[1]
        return StartSpec(mode="delay", delay_seconds=float(parse_duration(delay_str)))

    raise ValueError(f"无法解析 start 字段: {raw!r}")


# ─── 类型定义 ──────────────────────────────────────────────────────────────────

# inject_fn(action, dry_run) → (chaos_name, fis_template_id)
InjectFn = Callable[[FaultAction, bool], tuple[str, str]]


# ─── 调度器 ─────────────────────────────────────────────────────────────────────

class ActionScheduler:
    """
    组合实验时序调度器。

    负责按照 start 字段或 schedule.waves 的定义，
    编排多个 FaultAction 的注入时序。
    """

    def __init__(self):
        self.states: dict[str, ActionState] = {}
        self._abort_flag = threading.Event()

    def execute(
        self,
        experiment: CompositeExperiment,
        inject_fn: InjectFn,
        dry_run: bool = False,
    ) -> dict[str, ActionState]:
        """
        编排执行所有 action。

        Args:
            experiment: 组合实验
            inject_fn: 注入回调函数，签名 (action, dry_run) → (chaos_name, fis_template_id)
            dry_run: 是否 dry-run

        Returns:
            action_id → ActionState 字典
        """
        # 初始化状态
        self.states = {
            a.id: ActionState(action=a)
            for a in experiment.actions
        }
        self._abort_flag.clear()

        if experiment.schedule and experiment.schedule.waves:
            self._execute_waves(experiment.schedule, inject_fn, dry_run)
        else:
            self._execute_by_start(experiment.actions, inject_fn, dry_run)

        return self.states

    def abort_all(
        self,
        cleanup_fn: Callable[[ActionState], None],
        timeout: float = 30.0,
    ) -> None:
        """
        统一熔断：标记 abort 并清理所有活跃 action。

        Args:
            cleanup_fn: 清理回调，签名 (state) → None
            timeout: 清理总超时秒数
        """
        self._abort_flag.set()
        logger.warning(f"🛑 abort_all: 清理 {len(self.states)} 个 action")

        deadline = time.time() + timeout
        for aid, state in self.states.items():
            if state.status in ("active", "injecting"):
                try:
                    cleanup_fn(state)
                    state.status = "cleaned"
                    logger.info(f"  ✅ 清理完成: {aid}")
                except Exception as e:
                    state.status = "error"
                    state.error = str(e)
                    logger.error(f"  ❌ 清理失败: {aid}: {e}")

                if time.time() > deadline:
                    logger.warning("abort_all 超时，剩余 action 可能未清理")
                    break

    # ── Wave 模式 ──────────────────────────────────────────────────────────

    def _execute_waves(
        self, schedule: Schedule, inject_fn: InjectFn, dry_run: bool
    ) -> None:
        """按 wave 执行：同 wave 并行，wave 间串行"""
        for i, wave in enumerate(schedule.waves):
            if self._abort_flag.is_set():
                break

            logger.info(f"🌊 Wave {i+1}: {wave.name} ({len(wave.action_ids)} actions)")

            # wave 间延迟
            if i > 0 and wave.delay_after_previous != "0s":
                delay = parse_duration(wave.delay_after_previous)
                logger.info(f"  ⏳ wave 间等待 {wave.delay_after_previous}")
                if not dry_run:
                    self._interruptible_sleep(delay)

            # 并行执行 wave 内的 actions
            actions = [
                self.states[aid].action
                for aid in wave.action_ids
                if aid in self.states
            ]
            self._inject_parallel(actions, inject_fn, dry_run)

    # ── Start 字段模式 ─────────────────────────────────────────────────────

    def _execute_by_start(
        self, actions: list[FaultAction], inject_fn: InjectFn, dry_run: bool
    ) -> None:
        """按各 action 的 start 字段调度"""
        # 分离 immediate 和有依赖的 action
        immediate = []
        deferred = []

        for action in actions:
            spec = parse_start_spec(action.start)
            if spec.mode == "immediate":
                immediate.append(action)
            else:
                deferred.append((action, spec))

        # 先并行执行所有 immediate
        if immediate:
            logger.info(f"⚡ 立即执行: {[a.id for a in immediate]}")
            self._inject_parallel(immediate, inject_fn, dry_run)

        # 然后处理有依赖的 action
        if deferred:
            with ThreadPoolExecutor(max_workers=len(deferred)) as pool:
                futures = []
                for action, spec in deferred:
                    f = pool.submit(
                        self._wait_and_inject, action, spec, inject_fn, dry_run
                    )
                    futures.append(f)
                # 等待所有 deferred 完成
                for f in futures:
                    f.result()

    def _wait_and_inject(
        self,
        action: FaultAction,
        spec: StartSpec,
        inject_fn: InjectFn,
        dry_run: bool,
    ) -> None:
        """等待依赖条件满足后注入"""
        if self._abort_flag.is_set():
            return

        if spec.mode == "delay":
            logger.info(f"  ⏳ {action.id}: 延迟 {spec.delay_seconds}s")
            if not dry_run:
                self._interruptible_sleep(spec.delay_seconds)

        elif spec.mode == "after":
            dep_id = spec.depends_on
            if dep_id in self.states:
                logger.info(f"  ⏳ {action.id}: 等待 {dep_id} 启动")
                self.states[dep_id].started.wait(timeout=300)
                if spec.delay_seconds > 0:
                    logger.info(f"  ⏳ {action.id}: {dep_id} 已启动，再延迟 {spec.delay_seconds}s")
                    if not dry_run:
                        self._interruptible_sleep(spec.delay_seconds)
            else:
                logger.warning(f"  ⚠️ {action.id}: 依赖 {dep_id} 不存在，立即执行")

        elif spec.mode == "after_complete":
            dep_id = spec.depends_on
            if dep_id in self.states:
                logger.info(f"  ⏳ {action.id}: 等待 {dep_id} 完成")
                self.states[dep_id].completed.wait(timeout=600)
                if spec.delay_seconds > 0 and not dry_run:
                    self._interruptible_sleep(spec.delay_seconds)
            else:
                logger.warning(f"  ⚠️ {action.id}: 依赖 {dep_id} 不存在，立即执行")

        if not self._abort_flag.is_set():
            self._inject_one(action, inject_fn, dry_run)

    # ── 注入执行 ──────────────────────────────────────────────────────────

    def _inject_parallel(
        self, actions: list[FaultAction], inject_fn: InjectFn, dry_run: bool
    ) -> None:
        """并行注入多个 action"""
        if len(actions) == 1:
            self._inject_one(actions[0], inject_fn, dry_run)
            return

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = {
                pool.submit(self._inject_one, a, inject_fn, dry_run): a
                for a in actions
            }
            for f in futures:
                f.result()

    def _inject_one(
        self, action: FaultAction, inject_fn: InjectFn, dry_run: bool
    ) -> None:
        """注入单个 action 并更新状态"""
        state = self.states[action.id]

        if self._abort_flag.is_set():
            state.status = "cleaned"
            return

        try:
            state.status = "injecting"
            logger.info(f"  💥 注入: {action.id} ({action.type} via {action.backend})")

            chaos_name, template_id = inject_fn(action, dry_run)

            state.chaos_name = chaos_name
            state.fis_template_id = template_id
            state.status = "active"
            state.inject_time = datetime.now(timezone.utc)
            state.started.set()

            logger.info(f"  ✅ {action.id} 注入完成: {chaos_name}")

            # 如果是 dry-run 或 action 有 duration，设置延迟后标记 completed
            duration = action.params.get("duration")
            if dry_run:
                state.completed.set()
            elif duration:
                dur_secs = parse_duration(str(duration))
                threading.Timer(dur_secs, state.completed.set).start()

        except Exception as e:
            state.status = "error"
            state.error = str(e)
            state.started.set()   # 即使失败也要通知依赖方
            state.completed.set()
            logger.error(f"  ❌ {action.id} 注入失败: {e}")

    # ── 工具方法 ──────────────────────────────────────────────────────────

    def _interruptible_sleep(self, seconds: float, interval: float = 1.0) -> None:
        """可中断的 sleep，abort_flag 触发时立即返回"""
        end = time.time() + seconds
        while time.time() < end:
            if self._abort_flag.is_set():
                return
            time.sleep(min(interval, end - time.time()))

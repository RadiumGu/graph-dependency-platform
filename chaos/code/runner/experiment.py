"""
experiment.py - Experiment 数据模型（YAML 解析 + 验证）
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional
import yaml


# ─── 解析工具 ────────────────────────────────────────────────────────────────

def parse_duration(s: str) -> int:
    """'2m' → 120, '30s' → 30, '10m' → 600"""
    m = re.fullmatch(r'(\d+)(s|m|h)', s.strip())
    if not m:
        raise ValueError(f"无法解析 duration: {s!r}")
    v, unit = int(m.group(1)), m.group(2)
    return v * {'s': 1, 'm': 60, 'h': 3600}[unit]


def parse_threshold(expr: str):
    """
    '>= 99%' → (op, 99.0)
    '< 5000ms' → (op, 5000.0)
    '> 5000ms' → (op, 5000.0)
    返回 (callable, float)
    """
    import operator
    m = re.fullmatch(r'([><=!]+)\s*([\d.]+)\s*(%|ms)?', expr.strip())
    if not m:
        raise ValueError(f"无法解析 threshold: {expr!r}")
    op_str, val_str = m.group(1), m.group(2)
    val = float(val_str)
    ops = {
        '>=': operator.ge, '>': operator.gt,
        '<=': operator.le, '<': operator.lt,
        '==': operator.eq, '!=': operator.ne,
    }
    op = ops.get(op_str)
    if op is None:
        raise ValueError(f"未知运算符: {op_str!r}")
    return op, val


# ─── 数据类 ──────────────────────────────────────────────────────────────────

@dataclass
class MetricsSnapshot:
    timestamp: int
    success_rate: float       # %，0-100
    latency_p99_ms: float     # ms
    total_requests: int = 0

    def get(self, metric: str) -> float:
        return {
            'success_rate': self.success_rate,
            'latency_p99':  self.latency_p99_ms,
            'error_rate':   round(100 - self.success_rate, 2),
        }[metric]


@dataclass
class SteadyStateCheck:
    metric: str          # "success_rate" | "latency_p99" | "error_rate"
    threshold: str       # ">= 99%" | "< 5000ms"
    window: str = "1m"

    def is_satisfied(self, snapshot: MetricsSnapshot) -> bool:
        op, threshold = parse_threshold(self.threshold)
        value = snapshot.get(self.metric)
        return op(value, threshold)

    def describe(self, snapshot: MetricsSnapshot) -> str:
        value = snapshot.get(self.metric)
        return f"{self.metric}={value:.1f} (要求 {self.threshold})"


@dataclass
class StopCondition:
    metric: str
    threshold: str       # "< 50%" | "> 5000ms"
    window: str = "30s"
    action: str = "abort"
    cloudwatch_alarm_arn: Optional[str] = None  # FIS 原生 Stop Condition

    def is_triggered(self, snapshot: MetricsSnapshot) -> bool:
        op, threshold = parse_threshold(self.threshold)
        value = snapshot.get(self.metric)
        return op(value, threshold)

    def describe(self, snapshot: MetricsSnapshot) -> str:
        value = snapshot.get(self.metric)
        return f"{self.metric}={value:.1f} 满足停止条件 {self.threshold}"


@dataclass
class FaultSpec:
    type: str            # "pod_kill" | "network_delay" | "fis_lambda_delay" | ...
    mode: str            # "fixed-percent" | "all" | "one"
    value: str           # "50"
    duration: str        # "2m"
    latency: Optional[str] = None
    loss: Optional[str] = None
    corrupt: Optional[str] = None
    container_names: Optional[list] = None
    workers: Optional[int] = None
    load: Optional[int] = None
    size: Optional[str] = None
    time_offset: Optional[str] = None
    direction: Optional[str] = None
    external_targets: Optional[list] = None
    action: Optional[str] = None         # http_chaos action
    port: Optional[int] = None           # http_chaos port
    delay: Optional[int] = None          # http_chaos delay (ms)
    exclude_paths: Optional[list] = None # http_chaos 排除路径（预留字段）
    # 注意：Chaos Mesh 2.8.x 中 exclude_paths 对 delay action 无效，
    # iptables 拦截发生在 HTTP 层解析之前，path 过滤不生效。
    # 当前通过将 delay 值控制在 probe timeout 以下来规避 CrashLoopBackOff。
    extra_params: Optional[dict] = None  # FIS 专属参数（function_arn, cluster_arn 等）


@dataclass
class RcaSpec:
    enabled: bool = False
    trigger_after: str = "30s"
    expected_root_cause: Optional[str] = None


@dataclass
class GraphFeedbackSpec:
    enabled: bool = True
    edges: list = field(default_factory=lambda: ["Calls"])


@dataclass
class Experiment:
    name: str
    description: str
    target_service: str
    target_namespace: str
    target_tier: str
    fault: FaultSpec
    steady_state_before: list[SteadyStateCheck]
    steady_state_after: list[SteadyStateCheck]
    stop_conditions: list[StopCondition]
    rca: RcaSpec = field(default_factory=RcaSpec)
    graph_feedback: GraphFeedbackSpec = field(default_factory=GraphFeedbackSpec)
    backend: str = "chaosmesh"        # "chaosmesh" | "fis"
    enabled: bool = True              # False → 跳过执行（YAML 中 enabled: false）
    max_duration: str = "10m"
    save_to_bedrock_kb: bool = False
    yaml_source: str = ""


# ─── YAML 加载 ───────────────────────────────────────────────────────────────

def load_experiment(path: str) -> Experiment:
    with open(path, 'r') as f:
        d = yaml.safe_load(f)

    target = d.get('target', {})
    fault_d = d.get('fault', {})
    ss = d.get('steady_state', {})
    rca_d = d.get('rca', {})
    gf_d = d.get('graph_feedback', {})
    opts = d.get('options', {})

    fault = FaultSpec(
        type=fault_d['type'],
        mode=fault_d.get('mode', 'all'),
        value=str(fault_d.get('value', '100')),
        duration=fault_d.get('duration', '2m'),
        latency=fault_d.get('latency'),
        loss=fault_d.get('loss'),
        corrupt=fault_d.get('corrupt'),
        container_names=fault_d.get('container_names'),
        workers=fault_d.get('workers'),
        load=fault_d.get('load'),
        size=fault_d.get('size'),
        time_offset=fault_d.get('time_offset'),
        direction=fault_d.get('direction'),
        external_targets=fault_d.get('external_targets'),
        action=fault_d.get('action'),
        port=fault_d.get('port'),
        delay=fault_d.get('delay'),
        exclude_paths=fault_d.get('exclude_paths'),
        extra_params=fault_d.get('extra_params'),
    )

    def parse_checks(items) -> list[SteadyStateCheck]:
        return [SteadyStateCheck(
            metric=c['metric'],
            threshold=c['threshold'],
            window=c.get('window', '1m'),
        ) for c in (items or [])]

    def _expand_arn(arn: str) -> str:
        """展开 ARN 中的 ${REGION} / ${ACCOUNT_ID} 占位符"""
        if not arn:
            return arn
        from .config import REGION, ACCOUNT_ID
        return arn.replace("${REGION}", REGION).replace("${ACCOUNT_ID}", ACCOUNT_ID)

    def parse_stops(items) -> list[StopCondition]:
        return [StopCondition(
            metric=c['metric'],
            threshold=c['threshold'],
            window=c.get('window', '30s'),
            action=c.get('action', 'abort'),
            cloudwatch_alarm_arn=_expand_arn(c.get('cloudwatch_alarm_arn') or ""),
        ) for c in (items or [])]

    exp = Experiment(
        name=d['name'],
        description=d.get('description', ''),
        target_service=target.get('service', ''),
        target_namespace=target.get('namespace', 'default'),
        target_tier=target.get('tier', 'Tier1'),
        fault=fault,
        steady_state_before=parse_checks(ss.get('before', [])),
        steady_state_after=parse_checks(ss.get('after', [])),
        stop_conditions=parse_stops(d.get('stop_conditions', [])),
        rca=RcaSpec(
            enabled=rca_d.get('enabled', False),
            trigger_after=rca_d.get('trigger_after', '30s'),
            expected_root_cause=rca_d.get('expected_root_cause'),
        ),
        graph_feedback=GraphFeedbackSpec(
            enabled=gf_d.get('enabled', True),
            edges=gf_d.get('edges', ['Calls']),
        ),
        backend=d.get('backend', 'chaosmesh'),
        enabled=d.get('enabled', True),
        max_duration=opts.get('max_duration', '10m'),
        save_to_bedrock_kb=opts.get('save_to_bedrock_kb', False),
        yaml_source=path,
    )

    # FIS 实验：运行时解析 ARN（service_name + resource_type → 真实 ARN）
    # fis-scenario 不走 TargetResolver（多 action 模板用 tag + AZ filter，ARN 在 YAML extra_params 中直接指定）
    if exp.backend == "fis" and exp.enabled:
        try:
            from .target_resolver import TargetResolver
            TargetResolver().resolve_experiment(exp)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                f"实验 {exp.name}: ARN 解析跳过（{e}）"
            )

    # fis-scenario: 展开 extra_params 中的 ARN 占位符
    if exp.backend == "fis-scenario" and exp.fault.extra_params:
        for key, val in exp.fault.extra_params.items():
            if isinstance(val, str) and "${" in val:
                exp.fault.extra_params[key] = _expand_arn(val)

    return exp

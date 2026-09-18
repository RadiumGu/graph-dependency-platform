"""
experiment.py - Experiment 数据模型（YAML 解析 + 验证）
支持单 action 实验（fault 字段）和组合实验（faults 字段）两种格式。
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
    # 采集是否成功。**False 表示这个采样点没有数据，不表示"指标为 0"**。
    #
    # 2026-08-31 实测缺陷：`metrics.collect()` 在 ClickHouse 查询异常时
    # fallback 成 `success_rate=100.0 / total_requests=0`，与「真的零流量」
    # 以及「真的健康」三者在结构上无法区分。后果落在吞吐通道上：
    # `observer_min_requests` 取各采样点最小值，一次查询抖动就把谷值压成 0，
    # 于是 322 → 0 被算成「吞吐塌陷 100%」，合成退化率 100pp，
    # 一条边会被**误判 confirmed**。
    #
    # 成功率通道当初专门防过这件事（min 只在有值时更新、判定前先查请求量下限），
    # 吞吐通道是后加的，没有跟上同一条纪律 —— 这正是「不变量要写成代码而不是
    # 写在注释里」的又一例。
    ok: bool = True

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
    # 护栏**看谁**（T-214b）。默认 injection 保持既有行为。
    #   "injection"        —— 注入目标自己
    #   "any_observer"     —— 任一观测方触发即熔断
    #   "observer:<svc>"   —— 指定某个观测方
    #
    # 为什么必须能选：边验证实验里**注入目标本来就该失败** —— abort 掉 B 的流量，
    # B 的成功率必然掉到接近 0，于是基于 B 的护栏必然在 Phase 5 之前触发，
    # `_verify_edges` 永远跑不到（2026-08-31 首次真实注入即如此：T+71s 掉到 27.4%
    # 触发 <40% 熔断，实验以 ERROR 收场、零判定）。
    # 要防的是**附带损害**（调用方崩了），不是预期效果。注入目标侧只保留一个
    # 极低的地板，防注入手段本身失控。
    target: str = "injection"

    def is_triggered(self, snapshot: MetricsSnapshot) -> bool:
        op, threshold = parse_threshold(self.threshold)
        value = snapshot.get(self.metric)
        return op(value, threshold)

    def describe(self, snapshot: MetricsSnapshot) -> str:
        value = snapshot.get(self.metric)
        return f"{self.metric}={value:.1f} 满足停止条件 {self.threshold} [{self.target}]"

    # ── 护栏对象判定 ────────────────────────────────────────────────────────
    def applies_to_injection(self) -> bool:
        return self.target == "injection"

    def observer_scope(self) -> Optional[str]:
        """返回 '*'（任一观测方）、具体服务名，或 None（不看观测方）。"""
        if self.target == "any_observer":
            return "*"
        if self.target.startswith("observer:"):
            svc = self.target.split(":", 1)[1].strip()
            return svc or None
        return None


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


# ─── 组合实验数据类 ────────────────────────────────────────────────────────────

@dataclass
class FaultAction:
    """组合实验中的单个故障动作，对应 faults[] 下的一个条目"""
    id: str                               # action 唯一标识，YAML 中用于 start 依赖引用
    type: str                             # fault_catalog.yaml 中的 type
    backend: str                          # "chaosmesh" | "fis"
    params: dict                          # mode, value, duration, latency, extra_params 等
    target_service: Optional[str] = None  # 可选，覆盖顶层 target.service
    target_namespace: Optional[str] = None  # 可选，覆盖顶层 target.namespace
    start: str = "immediate"             # 启动时序语法，见 parse_start_spec()


@dataclass
class Wave:
    """schedule.waves 中的一个执行阶段，同 wave 内并行，wave 间串行"""
    name: str
    action_ids: list[str]
    delay_after_previous: str = "0s"     # 上一个 wave 完成后的等待时间


@dataclass
class Schedule:
    """显式 wave 编排（可选）；若存在则忽略各 action 的 start 字段"""
    waves: list[Wave] = field(default_factory=list)


@dataclass
class ObservationTarget:
    """
    观测方 —— 一条依赖边的**调用侧**。

    为什么需要这个字段（T-210，north_star DoD-3）：
    验证边 `A -[X]-> B` 必须在 **B** 注入、观测 **A**。而 runner 原先只采集
    `exp.target_service`（注入目标）自己的指标 —— 「打断 B 之后 B 是否退化」
    近乎恒真，**根本没有检验任何边**。这也解释了历史上 72 个实验全部 `passed`、
    零失败：判定门槛没有分辨力。

    命名沿用参考仓库 chaos-engineering-on-aws 的 assessment-output-spec.md §2.5
    三角色标注：Injection Target（= exp.target_service）/ Observation Target（本类）/
    Impact Target。这个三元组本质就是一条边的断言「在 X 注入，预期在 Y 观测到影响」。
    """
    service: str
    namespace: Optional[str] = None      # 省略则继承 exp.target_namespace
    edge_label: str = "Calls"            # 这条断言对应的边类型
    # 观测方在基线期必须达到的最小请求量。低于此值只能判 inconclusive，
    # 绝不能判 refuted —— metrics.collect() 无数据时 fallback
    # success_rate=100.0 / total_requests=0，**零流量和健康完全一样**
    # （north_star §4 不变量 7）。
    min_baseline_requests: int = 10

    @classmethod
    def parse(cls, item, default_edge: str = "Calls") -> "ObservationTarget":
        """接受 'svc'、'ns/svc' 或 dict 三种写法。"""
        if isinstance(item, dict):
            return cls(
                service=item.get('service', ''),
                namespace=item.get('namespace'),
                edge_label=item.get('edge_label', default_edge),
                min_baseline_requests=int(item.get('min_baseline_requests', 10)),
            )
        s = str(item)
        if '/' in s:
            ns, svc = s.split('/', 1)
            return cls(service=svc, namespace=ns, edge_label=default_edge)
        return cls(service=s, edge_label=default_edge)


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
    backend: str = "chaosmesh"        # "chaosmesh" | "fis" | "composite"
    enabled: bool = True              # False → 跳过执行（YAML 中 enabled: false）
    max_duration: str = "10m"
    save_to_bedrock_kb: bool = False
    yaml_source: str = ""
    # 观测方列表（T-210）。空列表 = 退化为旧行为（只采注入目标自己），
    # 此时该实验**不能**用于边验证 —— edge_verification 会因缺观测方数据判 inconclusive。
    # 图谱里被注入方的节点名。**默认等于 target_service**，只在两者必然不同时才写。
    #
    # 为什么需要它（2026-08-31 16:56 实测）：Chaos Mesh 路径下 target_service
    # 兼任两职 —— kubectl 的 label selector（选哪些 Pod 注入）与图谱节点名
    # （candidate_edges 查谁的入边）。对「在 B 注入、观测 A」这种拓扑两者一致，
    # 但对「切断 A 到外部服务 X 的路径、观测 A」这种**边切断**拓扑必然不同：
    # 注入选择器要选 A 的 Pod，而候选边要查 X 的入边。
    # 实测写 target.service: ssm 会让 preflight 报「服务 ssm 无 Running Pods」。
    target_graph_node: str = ""
    observation_targets: list[ObservationTarget] = field(default_factory=list)


@dataclass
class CompositeExperiment(Experiment):
    """
    组合实验：继承 Experiment，扩展多 action 字段。
    fault 字段继承自 Experiment，组合实验中为兼容性虚拟值（type="composite"）。
    实际注入逻辑使用 actions 列表。
    """
    actions: list[FaultAction] = field(default_factory=list)
    schedule: Optional[Schedule] = None


# ─── 模块级辅助函数 ──────────────────────────────────────────────────────────

def _expand_arn(arn: str) -> str:
    """展开 ARN 中的 ${REGION} / ${ACCOUNT_ID} 占位符"""
    if not arn:
        return arn
    from .config import REGION, ACCOUNT_ID
    return arn.replace("${REGION}", REGION).replace("${ACCOUNT_ID}", ACCOUNT_ID)


def _parse_checks(items) -> list[SteadyStateCheck]:
    """解析 steady_state.before/after 列表"""
    return [SteadyStateCheck(
        metric=c['metric'],
        threshold=c['threshold'],
        window=c.get('window', '1m'),
    ) for c in (items or [])]


def _parse_stops(items) -> list[StopCondition]:
    """解析 stop_conditions 列表"""
    return [StopCondition(
        metric=c['metric'],
        threshold=c['threshold'],
        window=c.get('window', '30s'),
        action=c.get('action', 'abort'),
        cloudwatch_alarm_arn=_expand_arn(c.get('cloudwatch_alarm_arn') or ""),
        target=(c.get('target') or 'injection').strip(),
    ) for c in (items or [])]


# ─── YAML 加载 ───────────────────────────────────────────────────────────────

def load_experiment(path: str, duration_override: Optional[str] = None) -> "Experiment | CompositeExperiment":
    """
    加载实验 YAML。

    Args:
        path: YAML 文件路径
        duration_override: 若指定，覆盖实验中所有 action 的 duration（CLI --duration 参数）

    Returns:
        CompositeExperiment（有 faults 复数字段或 backend=composite）或普通 Experiment
    """
    with open(path, 'r') as f:
        d = yaml.safe_load(f)

    # 检测组合实验格式（有 faults 复数字段，或 backend == "composite"）
    if 'faults' in d or d.get('backend') == 'composite':
        return _load_composite_experiment(d, path, duration_override)

    target = d.get('target', {})
    fault_d = d.get('fault', {})
    ss = d.get('steady_state', {})
    rca_d = d.get('rca', {})
    gf_d = d.get('graph_feedback', {})
    opts = d.get('options', {})

    # 观测方（T-210）：两种写法都接受
    #   target.observers: [petsite, "petadoptions/gateway-service"]
    #   observation_targets: [{service: petsite, edge_label: Calls, min_baseline_requests: 20}]
    # 默认边类型取 graph_feedback.edges 的第一项，与写回的边类型保持一致。
    _default_edge = (gf_d.get('edges') or ['Calls'])[0]
    _obs_raw = d.get('observation_targets') or target.get('observers') or []
    observation_targets = [ObservationTarget.parse(o, _default_edge) for o in _obs_raw]
    observation_targets = [o for o in observation_targets if o.service]

    # CLI --duration 覆盖 YAML 中的 fault.duration
    fault_duration = duration_override or fault_d.get('duration', '2m')

    fault = FaultSpec(
        type=fault_d['type'],
        mode=fault_d.get('mode', 'all'),
        value=str(fault_d.get('value', '100')),
        duration=fault_duration,
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

    exp = Experiment(
        name=d['name'],
        description=d.get('description', ''),
        target_service=target.get('service', ''),
        target_namespace=target.get('namespace', 'default'),
        target_graph_node=target.get('graph_node', '') or '',
        target_tier=target.get('tier', 'Tier1'),
        fault=fault,
        steady_state_before=_parse_checks(ss.get('before', [])),
        steady_state_after=_parse_checks(ss.get('after', [])),
        stop_conditions=_parse_stops(d.get('stop_conditions', [])),
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
        observation_targets=observation_targets,
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


def _load_composite_experiment(
    d: dict, path: str, duration_override: Optional[str] = None
) -> CompositeExperiment:
    """
    解析组合实验 YAML（有 faults 复数字段）。

    Args:
        d: 已解析的 YAML dict
        path: 原始文件路径（用于 yaml_source）
        duration_override: 若指定，覆盖所有 action 的 duration
    """
    target = d.get('target', {})
    ss = d.get('steady_state', {})
    rca_d = d.get('rca', {})
    gf_d = d.get('graph_feedback', {})
    opts = d.get('options', {})

    # 解析 actions
    actions: list[FaultAction] = []
    for fa in d.get('faults', []):
        params = dict(fa.get('params', {}))  # 拷贝，避免修改原始数据
        if duration_override:
            params['duration'] = duration_override
        actions.append(FaultAction(
            id=fa['id'],
            type=fa['type'],
            backend=fa.get('backend', 'chaosmesh'),
            params=params,
            target_service=fa.get('target_service'),
            target_namespace=fa.get('target_namespace'),
            start=fa.get('start', 'immediate'),
        ))

    # 解析 schedule.waves（可选）
    schedule: Optional[Schedule] = None
    sched_d = d.get('schedule', {})
    if sched_d and sched_d.get('waves'):
        waves = [
            Wave(
                name=w.get('name', f'wave-{i}'),
                action_ids=w.get('actions', []),
                delay_after_previous=str(w.get('delay_after_previous', '0s')),
            )
            for i, w in enumerate(sched_d['waves'])
        ]
        schedule = Schedule(waves=waves)

    # 计算兼容性虚拟 FaultSpec 的 duration：取所有 action duration 的最大值
    max_dur = opts.get('max_duration', '10m')
    for action in actions:
        dur = action.params.get('duration')
        if dur:
            try:
                if parse_duration(str(dur)) > parse_duration(max_dur):
                    max_dur = str(dur)
            except ValueError:
                pass

    # 兼容性虚拟 FaultSpec（用于 ExperimentResult.experiment_id 生成等场景）
    dummy_fault = FaultSpec(
        type='composite',
        mode='all',
        value='100',
        duration=max_dur,
    )

    return CompositeExperiment(
        name=d['name'],
        description=d.get('description', ''),
        target_service=target.get('service', ''),
        target_namespace=target.get('namespace', 'default'),
        target_graph_node=target.get('graph_node', '') or '',
        target_tier=target.get('tier', 'Tier1'),
        fault=dummy_fault,
        steady_state_before=_parse_checks(ss.get('before', [])),
        steady_state_after=_parse_checks(ss.get('after', [])),
        stop_conditions=_parse_stops(d.get('stop_conditions', [])),
        rca=RcaSpec(
            enabled=rca_d.get('enabled', False),
            trigger_after=rca_d.get('trigger_after', '30s'),
            expected_root_cause=rca_d.get('expected_root_cause'),
        ),
        graph_feedback=GraphFeedbackSpec(
            enabled=gf_d.get('enabled', True),
            edges=gf_d.get('edges', ['Calls']),
        ),
        backend='composite',
        enabled=d.get('enabled', True),
        max_duration=opts.get('max_duration', '10m'),
        save_to_bedrock_kb=opts.get('save_to_bedrock_kb', False),
        yaml_source=path,
        actions=actions,
        schedule=schedule,
    )

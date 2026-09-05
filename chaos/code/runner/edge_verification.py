"""edge_verification.py —— 用故障注入逐条验证图谱里的依赖边。

## 为什么需要这个模块

原有的 `graph_feedback.py` 想做这件事，但三处结构性问题让它从未真正生效：

1. **写入 100% 失败且被静默吞掉。** 边属性用了 `property(single, ...)`，Neptune
   对边属性拒绝基数说明 —— 实测返回
   `400 UnsupportedOperationException: "Cardinality specification may not be
   used with Edge properties."`，异常被 `except` 吞成 `logger.error`。
   活图谱实测：21 个用 Calls 类故障的实验跑完后，19 条 Calls 边上 `chaos_*`
   属性**全部为 0**。
2. **方向错误。** 原查询把同一判定写给注入目标的**所有出边和入边**。在 B 注入
   只能检验「谁依赖 B」，对「B 依赖谁」毫无信息 —— 写上去是伪造证据。
3. **观测对象错误。** `degradation_rate()` 采的是注入目标**自己**的指标。
   「打断 B 之后 B 是否退化」近乎恒真，根本没有检验任何边。

## 本模块的判据

验证一条边 `A -[dep]-> B` 的唯一正确做法：**在 B 注入，观测 A**。
参考 `assessment-output-spec.md` 的三角色划分（Injection / Observation /
Impact Target），一条边天然就是「在 X 注入，预期在 Y 观测到影响」的断言。

判定与置信度全部委托 `graph_confidence`（判据在 profiles/graph_contract.yaml
的 `edge_verification`，与 ETL 写入门禁共用一份声明）。

## 已知边界

- 依赖 DeepFlow 有真实流量。观测方流量不足时判 `inconclusive` 而**不判 refuted**
  —— `metrics.collect()` 无数据时 fallback `success_rate=100.0/total=0`，
  零流量与健康在指标上无法区分。
- 中间带退化（5%~20%）判 `inconclusive`。重试/熔断/缓存会让真实依赖只表现出
  轻微退化，判 refuted 会删掉真实边。
- 本模块**只标注，从不删边**。`refuted` 是给人看的证据，不是删除指令。
"""
from __future__ import annotations

import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAYER = os.path.abspath(os.path.join(_HERE, '..', '..', '..',
                                      'infra', 'lambda', 'shared', 'python'))
if _LAYER not in sys.path:
    sys.path.insert(0, _LAYER)

from graph_confidence import (
    classify_dependency_strength,  # noqa: E402
    STATUS_CONFIRMED,
    STATUS_INCONCLUSIVE,
    STATUS_REFUTED,
    STATUS_UNTESTED,
    classify_intervention,
    confidence,
    is_stale,
)

from .neptune_client import query_gremlin_parsed  # noqa: E402

logger = logging.getLogger(__name__)

VERIFIER = 'chaos-runner'

# 契约声明的依赖边类型。硬编码会与契约漂移，所以从契约读。
try:
    from graph_contract import dependency_edge_labels
    DEPENDENCY_LABELS = tuple(sorted(dependency_edge_labels()))
except Exception:  # 契约不可用时退回已知集合，并明确告警
    DEPENDENCY_LABELS = ('AccessesData', 'Calls', 'DependsOn')
    logger.warning("graph_contract 不可用，依赖边类型退回硬编码 %s", DEPENDENCY_LABELS)

# 观测源的属性前缀 —— 边上已存在按源区分的痕迹，不必新增字段。
# 实测一条 Calls 边同时带 xray_* / nfm_* / calls+error_rate 三组。
_OBSERVER_MARKERS = {
    'xray': ('xray_call_count', 'xray_last_seen'),
    'nfm': ('nfm_flow_count', 'nfm_last_seen'),
    'deepflow': ('calls', 'error_rate'),
}
_STATIC_SOURCES = ('aws-etl', 'cfn-etl')


# ── K8s 服务名 → 图谱规范名 ──────────────────────────────────────────────────
# 2026-08-31 首次拿到判定的那次注入实测缺陷：`candidate_edges` 直接拿实验里的
# **K8s 服务名**去匹配图谱节点名，而图谱里 Microservice 用的是**规范名**：
#
#     实验说 search-service   图谱里叫 petsearch
#     实验说 list-adoptions   图谱里叫 petlistadoptions
#     实验说 petsite          图谱里也叫 petsite（只有这个碰巧一致）
#
# 后果：`g.V().has('name','search-service')` 匹到的是**同名的 Deployment 与
# K8sService 节点**（本图 12 组名字跨标签重复，这是其中一组），它们没有 Calls
# 入边，于是候选边为空、日志只说「图谱中无候选边」、写回 0/2 —— 而
# `petsite -[Calls]-> petsearch` 与 `petlistadoptions -[Calls]-> petsearch`
# 两条边**明明都在图里**。
#
# 这与 183 条错源边是**同一个根因家族**：按名字匹配、不带标签、而名字跨标签重复。
# 解析表的唯一来源是 profiles/petsite.yaml 的 services 段（k8s_deployment /
# k8s_label / neptune_name），与 ETL 用的 service_mappings.json 同源，
# 不在此另立一份硬编码映射。
_NAME_RESOLVER = None
# 已打过日志的名字。Phase 3 每 10s 采样一次都会走 _target_metrics_name，
# 不去重的话一次 3 分钟实验会刷 18 行同样的解析日志，把真正的信号淹掉。
_LOGGED_RESOLUTIONS: set[str] = set()


def _resolve_graph_name(name: str) -> str:
    """把 K8s 服务名/别名解析成图谱里的规范名；解析不出就原样返回。

    刻意**不**在解析失败时抛错：一个不在 profile 里的服务仍然应该能跑实验，
    只是候选边查询会按原名去找（退回旧行为），比整个实验失败好。
    """
    global _NAME_RESOLVER
    if _NAME_RESOLVER is None:
        try:
            import sys as _sys
            _repo = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))
            if _repo not in _sys.path:
                _sys.path.insert(0, _repo)
            from profiles.profile_loader import EnvironmentProfile
            # 注意类名是 ServiceRegistry。dr-plan-generator 里另有一个同名不同物的
            # ServiceTypeRegistry（管服务**类型**，不管名字解析），别弄混。
            from shared.service_registry import ServiceRegistry
            services = EnvironmentProfile().get('services', {}) or {}
            _NAME_RESOLVER = ServiceRegistry(services)
            logger.info("服务名解析表已加载：%d 个服务", len(services))
        except Exception as e:
            logger.warning(
                "服务名解析表加载失败（%r）——候选边查询将按原名匹配，"
                "K8s 名与图谱规范名不一致的服务会查不到候选边", e)
            _NAME_RESOLVER = False   # 用 False 标记「试过且失败」，不重复尝试
    if not _NAME_RESOLVER:
        return name
    try:
        resolved = _NAME_RESOLVER.resolve(name)
    except Exception:
        return name
    if resolved != name:
        if name not in _LOGGED_RESOLUTIONS:
            _LOGGED_RESOLUTIONS.add(name)
            logger.info("服务名解析：%s → %s（图谱规范名）", name, resolved)
    return resolved


def resolve_graph_name(name: str) -> str:
    """公开入口 —— runner 解析观测方名字时用同一张表，避免两侧各自实现。"""
    return _resolve_graph_name(name)


def _label_list() -> str:
    return ','.join("'%s'" % x for x in DEPENDENCY_LABELS)


def evidence_from_props(props: dict) -> tuple[int, int, int, int]:
    """从边的现有属性推导证据计数。

    Returns:
        (static_sources, observing_sources, prior_confirmed, prior_refuted)

    静态证据来自 `dependency_kind == 'static'` 或 `source` 属于静态 ETL；
    观测证据按属性前缀计数（xray_* / nfm_* / deepflow 的 calls+error_rate）。
    先验的干预次数从 `verify_confirm_count` / `verify_refute_count` 读，
    没有则视为 0 —— 首次验证时这两个字段本来就不存在。
    """
    static = 0
    if props.get('dependency_kind') == 'static':
        static += 1
    if props.get('source') in _STATIC_SOURCES:
        static += 1
    static = min(static, 2)  # 同一条边最多算两个静态源（aws + cfn）

    observing = sum(
        1 for markers in _OBSERVER_MARKERS.values()
        if any(m in props for m in markers)
    )
    return (static, observing,
            int(props.get('verify_confirm_count') or 0),
            int(props.get('verify_refute_count') or 0))


def candidate_edges(injection_target: str) -> list[dict]:
    """列出「在 injection_target 注入」能够检验的边 —— 即它的**入边**。

    出边刻意不返回：在 B 注入不会告诉你 B 依赖谁。

    `injection_target` 传进来的是**实验里的 K8s 服务名**，这里先解析成图谱规范名
    （见 `_resolve_graph_name` 的注释：不解析会匹到同名的 Deployment/K8sService
    节点，候选边恒为空，而边其实就在图里）。

    返回的 `observer` 也是**图谱里的名字**；调用方按观测方的 K8s 名去索引之前
    必须做同样的解析，否则对不上。
    """
    graph_name = _resolve_graph_name(injection_target)
    q = (
        "g.V().has('name','%s').inE(%s).as('e')"
        ".project('eid','label','observer','props')"
        ".by(__.select('e').id())"
        ".by(__.select('e').label())"
        ".by(__.select('e').outV().values('name'))"
        ".by(__.select('e').valueMap())"
        ".fold()" % (graph_name, _label_list())
    )
    try:
        rows = query_gremlin_parsed(q)
    except Exception as e:
        logger.error("查询 %s 的候选边失败: %s", graph_name, e)
        return []
    out = []
    for r in _flatten(rows):
        if isinstance(r, dict) and r.get('eid'):
            out.append(r)
    if not out:
        # 空结果必须响：它可能是「真没有边」，也可能是名字对不上（历史上就是后者），
        # 而两者在日志里长得一样时，后者会被当成前者放过。
        logger.warning(
            "%s（图谱名 %s）没有任何依赖入边。若确信图里有边，先核对名字："
            "图谱 Microservice 用规范名，实验里用的是 K8s 服务名。",
            injection_target, graph_name)
    return out


def _flatten(rows):
    while isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], list):
        rows = rows[0]
    return rows if isinstance(rows, list) else []


def verify_edge(
    edge: dict,
    observer_baseline_requests: int,
    observer_injected_requests: int,
    observer_degradation_pct: float,
    experiment_id: str,
    now_epoch: int | None = None,
    evidence_channel: str = 'both',
    injection_confirmed: bool | None = None,
    edge_baseline_calls: int | None = None,
    observer_total_calls: int | None = None,
) -> dict:
    """对一条候选边做判定并算出新置信度。**纯计算，不写图。**

    分离计算与写入是刻意的：判定逻辑可以被单测穷举，不需要 Neptune。
    """
    now = now_epoch or int(time.time())
    # 独立观测源数必须**先**算出来再判定：它同时是两道门禁的输入 ——
    # 存在性侧「有独立证据的边不得判 refuted」，强度侧「soft 需要独立证据」。
    st, obs, conf_n, ref_n = evidence_from_props(edge.get('props') or {})

    status, reason = classify_intervention(
        observer_baseline_requests, observer_injected_requests,
        observer_degradation_pct, evidence_channel=evidence_channel,
        injection_confirmed=injection_confirmed,
        independent_observing_sources=obs,
        edge_baseline_calls=edge_baseline_calls,
        observer_total_calls=observer_total_calls)

    dep_class, dep_reason = classify_dependency_strength(
        observer_degradation_pct, evidence_channel=evidence_channel,
        injection_confirmed=injection_confirmed,
        independent_observing_sources=obs,
        observer_baseline_requests=observer_baseline_requests,
        observer_injected_requests=observer_injected_requests)
    if status == STATUS_CONFIRMED:
        conf_n += 1
    elif status == STATUS_REFUTED:
        ref_n += 1

    return {
        'edge_id': edge['eid'],
        'label': edge.get('label'),
        'observer': edge.get('observer'),
        'status': status,
        'reason': reason,
        'confidence': confidence(st, obs, conf_n, ref_n),
        'degradation_pct': round(observer_degradation_pct, 2),
        'evidence_channel': evidence_channel,
        'injection_confirmed': injection_confirmed,
        'edge_baseline_calls': edge_baseline_calls,
        'observer_total_calls': observer_total_calls,
        'dependency_class': dep_class,
        'dependency_class_reason': dep_reason,
        'observing_sources': obs,
        'confirm_count': conf_n,
        'refute_count': ref_n,
        'verified_at': now,
        'experiment_id': experiment_id,
    }


def write_verdict(v: dict) -> bool:
    """把判定写回边。

    **不使用 property(single, ...)** —— Neptune 对边属性拒绝基数说明
    （实测 400 UnsupportedOperationException）。边属性天生单值，直接 property()。
    按 edge id 精确定位，避免原实现「一次写给所有出入边」的伪造。
    """
    esc = str(v['reason']).replace("'", "").replace('\\', '')[:300]
    # 分级理由单独落盘：它记录的是「为什么能/不能分级」，与存在性的 reason 不同。
    # 不分级（None）时写 'unclassified' 而不是留空 —— 属性缺失与「判过但分不了级」
    # 在查询上无法区分，这与节点过期那次踩的是同一个坑。
    dep_esc = str(v.get('dependency_class_reason') or '').replace("'", "").replace('\\', '')[:300]
    q = (
        "g.E('%s')"
        ".property('verify_status', '%s')"
        ".property('verify_confidence', %s)"
        ".property('verify_last', %d)"
        ".property('verify_by', '%s')"
        ".property('verify_experiment', '%s')"
        ".property('verify_degradation', %s)"
        ".property('verify_reason', '%s')"
        ".property('verify_confirm_count', %d)"
        ".property('verify_refute_count', %d)"
        ".property('verify_evidence_channel', '%s')"
        ".property('verify_dependency_class', '%s')"
        ".property('verify_dependency_class_reason', '%s')"
        ".property('verify_observing_sources', %d)"
        % (v['edge_id'], v['status'], v['confidence'], v['verified_at'],
           VERIFIER, v['experiment_id'], v['degradation_pct'], esc,
           v['confirm_count'], v['refute_count'],
           v.get('evidence_channel', 'unknown'),
           v.get('dependency_class') or 'unclassified', dep_esc,
           int(v.get('observing_sources') or 0))
    )
    try:
        query_gremlin_parsed(q)
        logger.info("边 %s (%s->%s) 判定 %s  置信度 %s",
                    v['label'], v['observer'], '?', v['status'], v['confidence'])
        return True
    except Exception as e:
        logger.error("写回边判定失败 edge=%s: %s", v['edge_id'], e)
        return False


def coverage(now_epoch: int | None = None) -> dict:
    """边级验证覆盖度 —— 哪些依赖边从未被任何实验验证过。

    这是原有实现完全缺失的视角：learning 的覆盖分析是「按服务 × 5 个故障域」，
    回答不了「图里哪条边还没有证据」。而后者才是依赖图谱正确性的直接度量。
    """
    now = now_epoch or int(time.time())
    q = ("g.E().hasLabel(%s).project('eid','label','status','last')"
         ".by(__.id()).by(__.label())"
         ".by(__.coalesce(__.values('verify_status'), __.constant('%s')))"
         ".by(__.coalesce(__.values('verify_last'), __.constant(0)))"
         ".fold()" % (_label_list(), STATUS_UNTESTED))
    try:
        rows = _flatten(query_gremlin_parsed(q))
    except Exception as e:
        logger.error("覆盖度查询失败: %s", e)
        return {}

    by_status: dict[str, int] = {}
    stale = 0
    per_label: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = r.get('status') or STATUS_UNTESTED
        by_status[st] = by_status.get(st, 0) + 1
        lb = r.get('label') or '?'
        per_label.setdefault(lb, {}).setdefault(st, 0)
        per_label[lb][st] += 1
        if st != STATUS_UNTESTED and is_stale(int(r.get('last') or 0), now):
            stale += 1

    total = sum(by_status.values())
    verified = by_status.get(STATUS_CONFIRMED, 0) + by_status.get(STATUS_REFUTED, 0)
    return {
        'total_dependency_edges': total,
        'by_status': by_status,
        'per_label': per_label,
        'stale_verifications': stale,
        'verified_ratio': round(verified / total, 4) if total else 0.0,
    }


def _observer_marker_probe() -> str:
    """生成「现场数一遍这条边有几个独立观测源」的 Gremlin 片段。

    与 `evidence_from_props` 用的是**同一张** `_OBSERVER_MARKERS` 表 ——
    两处各写一份判据必然漂移，而漂移方向是选靶排序与判定用不同的证据口径。
    每个源出一个 `__.has(...)` 分支，union 后 count 即命中的源数。
    """
    branches = []
    for markers in _OBSERVER_MARKERS.values():
        # 一个源只要任一标记属性存在就算命中，与 evidence_from_props 的 any() 一致
        inner = ','.join(f"__.has('{m}')" for m in markers)
        branches.append(f"__.coalesce(__.or({inner}).limit(1), __.not(__.identity()))")
    return ','.join(branches)


def select_targets_for_verification(limit: int = 10) -> list[dict]:
    """挑出最该验证的边，用于驱动定期实验的选边。

    优先级依据：

    1. `drift_status == 'declared_not_observed'` —— 声明了却从未被观测到。
       这类边要么是死代码路径，要么观测是瞎的，两种情况都必须查清。
       活图谱实测有 22 条。
    2. `verify_status == 'untested'` 且置信度低 —— 完全没有干预证据。
    3. 验证已过期（默认 30 天）—— 拓扑会演进，旧证据会失效。

    这是「用图查询驱动实验选择」，而不是让 LLM 凭拓扑描述猜该测什么 ——
    后者是参考实现的做法，也是它最弱的一环。
    """
    q = ("g.E().hasLabel(%s)"
         ".project('eid','label','src','dst','drift','status','last','conf','obs')"
         ".by(__.id()).by(__.label())"
         ".by(__.outV().values('name')).by(__.inV().values('name'))"
         ".by(__.coalesce(__.values('drift_status'), __.constant('')))"
         ".by(__.coalesce(__.values('verify_status'), __.constant('%s')))"
         ".by(__.coalesce(__.values('verify_last'), __.constant(0)))"
         ".by(__.coalesce(__.values('verify_confidence'), __.constant(-1.0)))"
         # 独立观测源数：优先用判定时落盘的 verify_observing_sources，
         # 缺失（历史边或走了无门禁路径写入的）则从边上的观测源标记现场数一遍 ——
         # 不能默认 0，那会让老边在信息增益排序里被误判成「最不确定」而抢占队首。
         ".by(__.coalesce(__.values('verify_observing_sources'),"
         "     __.union(%s).count()))"
         ".fold()" % (_label_list(), STATUS_UNTESTED, _observer_marker_probe()))
    try:
        rows = _flatten(query_gremlin_parsed(q))
    except Exception as e:
        logger.error("选边查询失败: %s", e)
        return []

    now = int(time.time())
    scored = []
    for r in rows:
        if not isinstance(r, dict) or not r.get('eid'):
            continue
        pri = 3
        why = '常规复验'
        if r.get('drift') == 'declared_not_observed':
            pri, why = 0, '已声明但从未被观测 —— 死代码路径或观测盲区'
        elif r.get('status') == STATUS_UNTESTED:
            pri, why = 1, '从未有干预证据'
        elif is_stale(int(r.get('last') or 0), now):
            pri, why = 2, '验证已过期（拓扑可能已演进）'
        elif r.get('status') == STATUS_INCONCLUSIVE:
            pri, why = 2, '上次未能下结论，需重试'
        scored.append((pri, dict(r, priority=pri, why=why)))

    # ── 同优先级内按信息增益排序（2026-09-05 引入）─────────────────────────
    #
    # 原实现只有粗粒度的四档优先级，同档内是查询返回顺序（即任意顺序）。
    # 而「下一次注入该选哪条边」本质是**实验设计**问题：应当优先注入
    # **当前最不确定**的那条边，因为它的一次注入带来的信息量最大。
    #
    # 调研（arXiv:2209.04744 等主动学习/因果实验设计）的做法是按不确定性或
    # 信息增益排序干预。对本项目而言这几乎是**免费**的 —— 不确定性度量所需的
    # 两个字段图上都已经有：
    #   · verify_confidence        置信度越低 => 越不确定 => 信息增益越大
    #   · verify_observing_sources 独立观测源越少 => 存在性越不确定
    #
    # 刻意**不**引入 SAT 选靶（LDFI）：LDFI 的剪枝能力全部来自 lineage 里的
    # **冗余结构**（fallback / 缓存 / 副本），而本图不建模冗余 —— 无冗余时它的
    # CNF 退化成「逐条失败每条边」，正是本函数已经在做的事，SAT 一分价值不加。
    # 另一个硬阻塞是 LDFI 每轮需要**按请求粒度**注入并重放（Netflix 靠 FIT 在
    # Zuul/Hystrix 注入点实现），而本 runner 是 Pod/子网级注入 + 窗口聚合 SLI，
    # 拿不到「这一个请求成功了吗」这个布尔值。
    #
    # 排序键三段，全部升序（小者优先）：
    #   ① priority          既有的四档粗分类，仍然主导
    #   ② confidence        同档内置信度低者先测（-1 表示从未验证，天然最优先）
    #   ③ observing_sources 置信度相同时，独立观测源少者先测
    def _info_gain_key(item):
        pri, d = item
        conf = d.get('conf')
        conf = -1.0 if conf is None else float(conf)
        obs = int(d.get('obs') or 0)
        return (pri, conf, obs)

    scored.sort(key=_info_gain_key)
    for _, d in scored:
        c = d.get('conf')
        d['selection_key'] = (f"priority={d['priority']} confidence="
                              f"{'未验证' if c in (None, -1.0) else f'{float(c):.3f}'} "
                              f"独立观测源={int(d.get('obs') or 0)}")
    return [d for _, d in scored[:limit]]

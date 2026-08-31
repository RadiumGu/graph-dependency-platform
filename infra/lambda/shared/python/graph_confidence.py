"""依赖边的验证状态与置信度 —— 纯函数，判据全部来自契约。

## 它解决什么

引入之前，chaos 模块把注入结果映射成 `chaos_dependency_type` ∈
{strong, weak, none}（`chaos/code/runner/graph_feedback.py:_classify`），
但存在三个结构性问题：

1. **判定从未落回图谱。** 边属性写入用了 `property(single, ...)`，而 Neptune
   对边属性拒绝基数说明（实测 400 `UnsupportedOperationException:
   "Cardinality specification may not be used with Edge properties."`）。
   异常被 `except` 吞成 `logger.error`。活图谱实测：21 个用 Calls 类故障的实验
   跑完之后，19 条 Calls 边上 `chaos_*` 属性**全部为 0**。
2. **方向错误。** 原查询 `where(outV().has(name,svc).or_(inV().has(name,svc)))`
   把同一个判定写给 svc 的**所有出边和入边**。在 svc 注入故障只能检验
   「调用 svc 的边」，对 svc 自己的下游依赖毫无信息 —— 写上去等于凭空伪造证据。
3. **观测对象错误。** `degradation_rate()` 采的是**注入目标自己**的指标。
   「打断 svc 后 svc 是否退化」近乎恒真，根本没有检验任何边。
   验证边 A→B 必须**在 B 注入、观测 A**。

本模块只做数学与语义，不碰 Neptune、不依赖 boto3，因此可被单测穷举。

## 证据模型

log-odds 累加后经 sigmoid 映射到 [0,1]：

    静态声明（aws/cfn）   先验，每个源 +1.0
    观测（deepflow/xray） 似然，每个源 +0.5，**总量封顶 +1.5**
    干预确证（chaos）     +3.0，可翻转先验
    干预证伪（chaos）     -3.0

观测证据封顶的依据：arXiv:2607.09449 推出「样本越多越容易被虚假相关性诱导出
假边」的临界阈值。不封顶的话，一条假边只要 ETL 跑得够久就会变得"高置信"。

权重与阈值一律从 profiles/graph_contract.yaml 的 edge_verification 读，
不在本文件里写死 —— 判据必须与写入门禁共用同一份声明。
"""
from __future__ import annotations

import math

from graph_contract_data import EDGE_VERIFICATION

STATUS_UNTESTED = 'untested'
STATUS_CONFIRMED = 'confirmed'
STATUS_REFUTED = 'refuted'
STATUS_INCONCLUSIVE = 'inconclusive'

_W = EDGE_VERIFICATION['evidence_weights']
_T = EDGE_VERIFICATION['thresholds']

VERIFY_ATTRS = tuple(EDGE_VERIFICATION['attrs'])
VERIFY_AUTHORITY = tuple(EDGE_VERIFICATION['authority'])


def may_write_verify_attr(source: str) -> bool:
    """只有混沌运行器可写 verify_* 属性。

    ETL 不得写：静态采集若能覆盖 verify_status，等于用先验抹掉干预后验 ——
    而干预是唯一能证伪一条边的证据。
    """
    return source in VERIFY_AUTHORITY


def classify_intervention(
    observer_baseline_requests: int,
    observer_injected_requests: int,
    observer_degradation_pct: float,
    evidence_channel: str = 'both',
    throughput_only_confirm_pct: float = 60.0,
) -> tuple[str, str]:
    """把一次注入的观测结果判成 confirmed / refuted / inconclusive。

    Args:
        observer_baseline_requests: **观测方**（调用侧）基线期请求数
        observer_injected_requests: **观测方**注入期请求数
        observer_degradation_pct:   观测方成功率下降的百分点
        evidence_channel:           'success_rate' / 'throughput_only' / 'both' / 'none'
                                    —— 证据来自哪条通道，决定证据强度
        throughput_only_confirm_pct: 纯吞吐证据要判 confirmed 需达到的退化率

    Returns:
        (status, reason) —— reason 会写进图谱与报告，便于事后追溯为何如此判定。

    判定顺序刻意先查数据量：metrics.collect() 在无数据时 fallback
    success_rate=100.0 / total_requests=0，**零流量与健康长得完全一样**。
    不先设请求量下限，一条没有流量的边会被判成 refuted（假阴性）。

    ## 为什么要分证据通道（2026-08-31 15:56 实测补入）

    两条通道的证据强度**不对等**：
      · 成功率下降 = 观测方**自己**返回了失败 —— 归因明确
      · 吞吐塌陷   = 观测方的请求量少了 —— 三种成因分不清：它自己失败到不产生
        response 行（真依赖）／它的上游不再调它（传导）／测量管道受影响

    实测踩到第二种：断 DynamoDB 后 pay-for-adoption 成功率退化 **0.00pp**、
    吞吐塌陷 100%，但它的入流量来自 petsite，而 petsite 因 petsearch 失败已不再
    提交领养 —— 「它不再被调用」被当成了「它依赖 DynamoDB」。

    所以纯吞吐证据判 confirmed 的门槛显著抬高；达不到就判 inconclusive 而不是
    confirmed —— 与「零流量不判 refuted」同一方向：**宁可判不了，不可判错**。
    """
    need = _T['min_observation_requests']
    if observer_baseline_requests < need or observer_injected_requests < need:
        return (STATUS_INCONCLUSIVE,
                f"观测方流量不足（基线 {observer_baseline_requests} / "
                f"注入期 {observer_injected_requests}，需 >= {need}）—— "
                f"零流量与健康在指标上无法区分，不能据此证伪")

    if observer_degradation_pct >= _T['confirm_degradation_pct']:
        if (evidence_channel == 'throughput_only'
                and observer_degradation_pct < throughput_only_confirm_pct):
            return (STATUS_INCONCLUSIVE,
                    f"退化 {observer_degradation_pct:.1f}% 全部来自**吞吐塌陷**、"
                    f"成功率通道无信号，而吞吐下降分不清「观测方自己失败」与"
                    f"「上游不再调它」。纯吞吐证据需 >= {throughput_only_confirm_pct}% "
                    f"才判 confirmed，故不下结论")
        chan = {'both': '成功率+吞吐双通道', 'success_rate': '成功率通道',
                'throughput_only': '仅吞吐通道'}.get(evidence_channel, evidence_channel)
        return (STATUS_CONFIRMED,
                f"观测方退化 {observer_degradation_pct:.1f}% "
                f">= {_T['confirm_degradation_pct']}%（{chan}），依赖成立")

    if observer_degradation_pct <= _T['refute_degradation_pct']:
        return (STATUS_REFUTED,
                f"观测方退化仅 {observer_degradation_pct:.1f}% "
                f"<= {_T['refute_degradation_pct']}%，注入未传导到调用方")

    return (STATUS_INCONCLUSIVE,
            f"观测方退化 {observer_degradation_pct:.1f}% 落在中间带 "
            f"({_T['refute_degradation_pct']}%~{_T['confirm_degradation_pct']}%)。"
            f"重试/熔断/缓存会让真实依赖只表现出轻微退化，"
            f"判 refuted 会删掉真实边，故不下结论")


def confidence(
    static_sources: int = 0,
    observing_sources: int = 0,
    interventions_confirmed: int = 0,
    interventions_refuted: int = 0,
) -> float:
    """按证据算 [0,1] 置信度。

    Args:
        static_sources:          声明该边的静态源数（aws-etl / cfn-etl）
        observing_sources:       观测到该边的观测源数（deepflow / xray）
        interventions_confirmed: 确证该边的注入实验次数
        interventions_refuted:   证伪该边的注入实验次数

    观测证据封顶，干预证据不封顶 —— 干预是可重复的主动实验，多次一致结论应当
    继续增强（或削弱）置信；观测只是被动计数，重复观测不构成独立证据。
    """
    lo = 0.0
    lo += static_sources * _W['static_declaration']
    lo += min(observing_sources * _W['observed_per_source'], _W['observed_cap'])
    lo += interventions_confirmed * _W['intervention_confirmed']
    lo += interventions_refuted * _W['intervention_refuted']
    return round(1.0 / (1.0 + math.exp(-lo)), 4)


def is_stale(verified_at_epoch: int | None, now_epoch: int) -> bool:
    """该边的验证是否已过期（默认 30 天）。

    过期不等于失效 —— 拓扑会演进，一条 30 天前确证过的边今天可能已经不存在。
    过期只表示"该重新验证了"，用于驱动定期实验的选边，不改 verify_status。
    """
    if not verified_at_epoch:
        return True
    return (now_epoch - verified_at_epoch) > _T['stale_verification_seconds']

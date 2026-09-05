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

# ── 依赖强度三级（Google SRE 分类，2026-09-05 引入）──────────────────────────
# 与存在性（STATUS_*）**正交**：存在性回答「边是不是真的」，
# 强度回答「它有多要紧」。影响面分析、容量规划、故障预算用的是后者。
# 出处：Google Cloud《Defining SLOs for services with dependencies》(CRE life lessons)
DEP_CLASS_HARD = 'hard'          # 其宕机 = 调用方也宕机
DEP_CLASS_DEGRADED = 'degraded'  # 介于两者之间（如缓存失效只降级延迟）
DEP_CLASS_SOFT = 'soft'          # 设计得当则其故障对调用方无影响

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
    injection_confirmed: bool | None = None,
    independent_observing_sources: int = 0,
) -> tuple[str, str]:
    """把一次注入的观测结果判成 confirmed / refuted / inconclusive。

    Args:
        observer_baseline_requests: **观测方**（调用侧）基线期请求数
        observer_injected_requests: **观测方**注入期请求数
        observer_degradation_pct:   观测方成功率下降的百分点
        evidence_channel:           'success_rate' / 'throughput_only' / 'both' / 'none'
                                    —— 证据来自哪条通道，决定证据强度
        throughput_only_confirm_pct: 纯吞吐证据要判 confirmed 需达到的退化率
        injection_confirmed: 是否**独立确认**注入真的生效了。
                             None = 未知（默认）。只有 True 才允许判 refuted ——
                             见下方「注入生效门禁」。
        independent_observing_sources: 有多少个**独立观测源**看到过这条边
                             （xray / nfm / deepflow，由 evidence_from_props 计数）。
                             >= 1 时永远不判 refuted —— 见「独立证据门禁」。

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
        # ── 注入生效门禁（2026-08-31 16:30 实测补入）────────────────────────
        # 观测方没退化有两种可能，判 refuted 之前必须排除第二种：
        #   ① 注入生效了，但影响没传导到调用方 → 这条边可疑（真 refuted）
        #   ② **注入根本没生效** → 什么都没验证，判 refuted 是凭空证伪
        #
        # 实测踩到 ②：`petsearch -[AccessesData]-> s3` 被 FIS
        # `disrupt-connectivity scope=s3` 判 refuted（退化 1.21%）。
        # 但拿 X-Ray 严格按故障窗口复核，PetSearch 在窗口内做了 10 次与 13 次
        # **成功**的 S3 调用、0 错误 0 故障 —— NACL 没有切断这条路径。
        # 而这条边有两个独立源的硬证据：X-Ray 24h 内 17,190 次调用、
        # NFM 50 条流 1.9 MB（category=AMAZON_S3）。删它就是删掉真实依赖。
        #
        # 与「零流量不判 refuted」是同一条原则的另一面：
        # **证伪比确证需要更强的前提** —— 确证只需看到影响传导，
        # 证伪需要先证明「我真的打断了它」。
        if injection_confirmed is not True:
            hint = ('注入生效性未知（未提供 injection_confirmed）'
                    if injection_confirmed is None else '已确认注入未生效')
            return (STATUS_INCONCLUSIVE,
                    f"观测方退化仅 {observer_degradation_pct:.1f}%，但{hint} —— "
                    f"无法区分「注入生效而未传导」（真 refuted）与「注入根本没生效」"
                    f"（什么都没验证）。证伪需要先证明打断确实发生，故不下结论")

        # ── 独立证据门禁（2026-09-05 补入，引入 SRE 三级分类的连带修复）──────
        # 注入确认生效、观测方却没退化，仍有两种完全不同的真相：
        #   ① 这条边是假的（图错了）                      → 真 refuted
        #   ② 边是真的，但它是 **soft dependency**        → 打断它本就不该有影响
        #
        # Google SRE 的分级把 ② 命名为一等公民：soft dependency 指「设计得当时
        # 其故障对调用方无影响」的依赖（如尽力而为的日志/追踪链路）。
        # 一条 soft dependency 通过验证的表现与「边不存在」**在干预数据上完全同形**
        # —— 区分二者的唯一依据是**独立观测源是否看到过它**。
        #
        # 实测代价已经付过一次：`petsearch -[AccessesData]-> s3` 有两个独立源的
        # 硬证据（X-Ray 24h 内 17,190 次调用、NFM 50 条流 1.9 MB），却被判 refuted。
        # 那次的直接成因是注入未生效（上面那道门禁），但**即使注入真的生效了、
        # 观测方真的没退化，判 refuted 依然是错的** —— 正确结论是「这是一条
        # soft dependency」。按 DoD-10，累计两次 refuted 就会删边，
        # 而删掉一条真实存在的 soft dependency 会在影响面分析里造成盲区。
        #
        # 所以：**有独立观测证据的边永远不得判 refuted。**
        # 此时干预对「边是否存在」这个问题是真的不可判定（存在性由独立观测源
        # 而非本次干预确立），故返回 inconclusive；强度由
        # `classify_dependency_strength` 判成 soft，并且 refute_count 不递增。
        if independent_observing_sources >= 1:
            return (STATUS_INCONCLUSIVE,
                    f"观测方退化仅 {observer_degradation_pct:.1f}% 且已确认注入生效，"
                    f"但该边有 {independent_observing_sources} 个独立观测源看到过它 —— "
                    f"这是 soft dependency（打断它本就不该影响调用方），"
                    f"不是「边不存在」。本次干预对存在性不可判定，强度判为 soft")

        return (STATUS_REFUTED,
                f"观测方退化仅 {observer_degradation_pct:.1f}% "
                f"<= {_T['refute_degradation_pct']}%，已确认注入生效，"
                f"且**无任何独立观测源**看到过这条边 —— 注入未传导到调用方，"
                f"该边可疑")

    return (STATUS_INCONCLUSIVE,
            f"观测方退化 {observer_degradation_pct:.1f}% 落在中间带 "
            f"({_T['refute_degradation_pct']}%~{_T['confirm_degradation_pct']}%)。"
            f"重试/熔断/缓存会让真实依赖只表现出轻微退化，"
            f"判 refuted 会删掉真实边，故不下结论")


def classify_dependency_strength(
    observer_degradation_pct: float,
    evidence_channel: str = 'both',
    injection_confirmed: bool | None = None,
    independent_observing_sources: int = 0,
    observer_baseline_requests: int = 0,
    observer_injected_requests: int = 0,
) -> tuple[str | None, str]:
    """把依赖判成 Google SRE 的三级：hard / degraded / soft。无法分级返回 None。

    ## 为什么需要第三个轴（2026-09-05 业界调研引入）

    原模型只有存在性一个轴（confirmed / refuted / inconclusive），
    它回答「这条边是不是真的」，但回答不了「它有多要紧」——
    而后者才是影响面分析、容量规划、故障预算真正要用的信息。

    Google SRE 的分级（*Defining SLOs for services with dependencies*）给出三档：

        hard      其宕机 = 你也宕机
        degraded  介于两者之间（如缓存失效只降级延迟，不失败）
        soft      设计得当则其故障对你无影响（如尽力而为的日志/追踪）

    这个轴与存在性**正交**：一条 soft dependency 是真实存在的边，
    只是打断它不该有影响。原模型把这种情况错判成 refuted ——
    见 classify_intervention 里的「独立证据门禁」。

    ## 两条刻意设置的判据

    **① `throughput_only` 通道永远不得判 hard，最高只能到 degraded。**
    hard 的定义是「调用方自己不行了」，而这正是**成功率通道**表达的东西。
    纯吞吐塌陷分不清「观测方自己失败到不产生 response」与「上游不再调它」——
    实测踩过：断 DynamoDB 后 pay-for-adoption 成功率退化 **0.00pp**、
    吞吐塌陷 100%，而真实成因是 petsite 因 petsearch 失败已不再提交领养。
    若允许纯吞吐判 hard，那一轮会得出「pay-for-adoption 硬依赖 DynamoDB」
    这个由传导伪造出来的强结论。

    **② soft 需要两个前提同时成立**，各自挡掉一种混淆：
      · `injection_confirmed is True` —— 否则分不清 soft 与「注入根本没生效」
      · `independent_observing_sources >= 1` —— 否则分不清 soft 与「边不存在」
    两个前提对应两次真实踩坑，缺任一个都会把结论建在同形数据上。

    ## hard 阈值为何取 70pp（不是凭空定的）

    项目自己的护栏把观测方 `success_rate < 30%` 当作「已经坏了」
    （见各实验规格的 stop_conditions）。退化 >= 70pp 即调用方跌破它自己
    定义的「坏了」这条线 —— 与既有判据同源，而不是另立一个数字。

    Returns:
        (dep_class, reason)。dep_class ∈ {'hard','degraded','soft',None}
    """
    need = _T['min_observation_requests']
    if observer_baseline_requests < need or observer_injected_requests < need:
        return (None, f"观测方流量不足（需各 >= {need}），任何强度分级都不成立")

    hard_pct = _T.get('hard_degradation_pct', 70.0)

    if observer_degradation_pct >= _T['confirm_degradation_pct']:
        if observer_degradation_pct >= hard_pct:
            if evidence_channel == 'throughput_only':
                return (DEP_CLASS_DEGRADED,
                        f"退化 {observer_degradation_pct:.1f}% 已达 hard 线 "
                        f"{hard_pct}%，但证据**全部来自吞吐通道**、成功率通道无信号。"
                        f"吞吐塌陷分不清「调用方自己失败」与「上游不再调它」，"
                        f"不足以支撑 hard，封顶为 degraded")
            return (DEP_CLASS_HARD,
                    f"退化 {observer_degradation_pct:.1f}% >= {hard_pct}% 且成功率"
                    f"通道有信号 —— 调用方跌破自身「坏了」的门线（success_rate<30%），"
                    f"其故障等同调用方故障")
        return (DEP_CLASS_DEGRADED,
                f"退化 {observer_degradation_pct:.1f}% 落在 "
                f"[{_T['confirm_degradation_pct']}%, {hard_pct}%) —— "
                f"影响真实但未使调用方失效，属降级而非宕机")

    if observer_degradation_pct <= _T['refute_degradation_pct']:
        if injection_confirmed is not True:
            return (None,
                    f"退化仅 {observer_degradation_pct:.1f}% 但注入生效性未确认 —— "
                    f"分不清 soft dependency 与「注入根本没生效」，不分级")
        if independent_observing_sources < 1:
            return (None,
                    f"退化仅 {observer_degradation_pct:.1f}%、注入已确认生效，"
                    f"但无任何独立观测源看到过这条边 —— 分不清 soft dependency 与"
                    f"「这条边不存在」。存在性未立，强度无从谈起")
        return (DEP_CLASS_SOFT,
                f"已确认注入生效、观测方退化仅 {observer_degradation_pct:.1f}%，"
                f"而 {independent_observing_sources} 个独立观测源看到过这条边 —— "
                f"边真实存在且打断它不影响调用方，是 soft dependency。"
                f"这是**设计良好**的证据，不是图谱错误")

    return (None,
            f"退化 {observer_degradation_pct:.1f}% 落在中间带 "
            f"({_T['refute_degradation_pct']}%~{_T['confirm_degradation_pct']}%) —— "
            f"重试/熔断/缓存会让 hard dependency 只表现出轻微退化，"
            f"此带内无法区分 hard 与 soft，不分级")


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

"""
graph_rag_reporter.py - Graph RAG RCA 报告生成器（Phase 5）

数据流：
  Neptune 子图 + DeepFlow 调用链 + CloudWatch 指标 + CloudTrail 变更
      → 结构化 Prompt
          → Bedrock Claude
              → RCA 报告（根因 + 置信度 + 建议操作 + 理由）
"""
import os
import json
import logging
import boto3
from collectors import infra_collector
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
from shared import get_region
REGION = get_region()
BEDROCK_MODEL = os.environ.get('BEDROCK_MODEL', 'global.anthropic.claude-sonnet-4-6')
KB_ID = os.environ.get('BEDROCK_KB_ID', '')

# CloudWatch namespace/dimension 不再在此硬编码。
#
# 2026-08-28 移除背景:此处原有一个 SVC_TO_CW 字典,与 profiles/petsite.yaml 的
# services.<name>.cloudwatch 段重复维护,且有两个实际缺陷:
#   1. key 用的是 'petadoptionshistory' —— 那是 alias,规范名是 'pethistory'
#      (与 rca_window_flush/config.py 同一类漂移),调用方传规范名时直接查不到
#   2. 只列了 5 个服务,缺 petstatusupdater
# 现统一走 config.registry.get_cloudwatch_config(),它内部先 resolve() 别名,
# 传规范名或别名都能命中。


def _build_group_context(group, rca_result: dict) -> str:
    """构建聚合告警的上下文文本：时序 + 分组依据 + 拓扑印证。

    这是聚合报告相对单点报告的**唯一增量价值** —— 单条告警看不出传播方向，
    多条告警的先后顺序配合拓扑才能判断谁是源头。

    Args:
        group: EventGroup
        rca_result: rca_engine 输出（用于取 blast_radius 等已算好的结果）

    Returns:
        供 prompt 使用的文本块
    """
    alerts = list(getattr(group, 'all_alerts', []) or [])
    lines = ["[聚合告警分析]"]
    lines.append(f"- 本组共 {len(alerts)} 条告警，"
                 f"分组依据: {getattr(group, 'correlation_type', 'standalone')}"
                 f"（关联置信度 {getattr(group, 'confidence', 0):.2f}）")
    root_svc = getattr(group, 'root_candidate_service', '') or ''
    lines.append(f"- 关联器判定的根因候选: {root_svc or '未定'}")

    # ── 时序：按 start_time 排序，算相对最早告警的时延 ──
    def _ts(a):
        return getattr(a, 'start_time', '') or ''

    ordered = sorted([a for a in alerts if _ts(a)], key=_ts)
    if not ordered:
        lines.append("- ⚠️ 组内告警均无 start_time，无法做时序分析")
        return "\n".join(lines)

    import datetime as _dt

    def _parse(s):
        try:
            return _dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None

    t0 = _parse(_ts(ordered[0]))
    lines.append("")
    lines.append("[告警时序（按发生时间排序）]")
    for i, a in enumerate(ordered):
        svc = getattr(a, 'service_name', '?') or '?'
        metric = getattr(a, 'metric_name', '') or ''
        sev = getattr(a, 'severity', '?')
        ti = _parse(_ts(a))
        if t0 and ti:
            delta = (ti - t0).total_seconds()
            dstr = "最早" if i == 0 else f"晚 {delta:.0f}s"
        else:
            dstr = "时间不可解析"
        val = getattr(a, 'metric_value', None)
        thr = getattr(a, 'threshold', None)
        detail = f" {metric}={val}(阈值 {thr})" if metric else ""
        lines.append(f"  {i+1}. [{sev}] {svc}{detail} @ {_ts(a)} — {dstr}")

    earliest_svc = getattr(ordered[0], 'service_name', '') or ''
    later_svcs = [getattr(a, 'service_name', '') for a in ordered[1:]]
    later_svcs = [s for s in later_svcs if s and s != earliest_svc]

    # ── 拓扑印证：最早告警的服务是否确实是其余服务的上游 ──
    # 用 kind='live' 而非 'dynamic'：只看当前仍然存在的依赖。
    # 已下线服务的历史调用边不能用来解释现在的传播链。
    lines.append("")
    lines.append("[拓扑印证（是否支持时序推断的传播方向）]")
    if not later_svcs:
        lines.append("  组内只涉及单个服务，无跨服务传播可印证")
    else:
        try:
            from neptune import neptune_queries as nq
            radius = nq.q1_blast_radius(earliest_svc, kind='live')
            downstream = {r.get('name') for r in (radius.get('services') or [])}
            explained = [s for s in later_svcs if s in downstream]
            unexplained = [s for s in later_svcs if s not in downstream]
            lines.append(f"  最早告警服务: {earliest_svc}")
            lines.append(f"  其当前活跃下游: {sorted(downstream) or '（无）'}")
            if explained:
                lines.append(f"  ✅ 时序与拓扑一致（{earliest_svc} 在上游）: {explained}")
            if unexplained:
                lines.append(
                    f"  ⚠️ 拓扑无法解释（这些服务不在 {earliest_svc} 的活跃下游）: "
                    f"{unexplained} —— 可能是共因故障（如共享基础设施/AZ），"
                    f"而非从 {earliest_svc} 传播"
                )
            if not explained and not unexplained:
                lines.append("  拓扑数据为空，无法印证")
        except Exception as e:
            # 拓扑印证失败不能让整份报告生成不出来
            logger.warning(f"聚合报告的拓扑印证失败(non-fatal): {e}")
            lines.append(f"  拓扑查询失败，本节跳过: {str(e)[:120]}")

    # ── 关联器算出的影响面 ──
    br = getattr(group, 'blast_radius', None) or rca_result.get('blast_radius') or []
    if br:
        names = [b.get('name') if isinstance(b, dict) else str(b) for b in br]
        lines.append("")
        lines.append(f"[关联器给出的影响面] {sorted(set(n for n in names if n))}")

    return "\n".join(lines)


def generate_group_report(group, classification: dict, rca_result: dict) -> dict:
    """聚合告警的 Graph RAG 报告（EventGroup 级）。

    此前 window_flush_handler 调用本函数名但它**并不存在** —— 运行时抛
    AttributeError，靠 fallback 降级到 generate_rca_report()。意味着
    "按 EventGroup 聚合出报告"这条路径从未实现：聚合做到了，
    聚合后的联合分析没做到，削弱了告警聚合一半的价值。

    与单点报告的区别只在一处但很关键：注入**多告警时序 + 拓扑印证**，
    让模型回答"谁先出问题、拓扑能否解释这个顺序"，而不是只描述单个服务的症状。

    Args:
        group: EventGroup（topology_correlator 输出）
        classification: fault_classifier.classify_group 输出
        rca_result: rca_engine.analyze_group 输出

    Returns:
        与 generate_rca_report 同构的字典，聚合场景下额外含 propagation_analysis
    """
    svc = (classification.get('affected_service')
           or getattr(group, 'root_candidate_service', '')
           or '')
    if not svc:
        raise ValueError("generate_group_report: 无法确定受影响服务")

    group_context = _build_group_context(group, rca_result or {})
    log_samples = (rca_result or {}).get('log_samples', {})

    report = generate_rca_report(
        svc, classification, rca_result or {},
        log_samples=log_samples,
        group_context=group_context,
    )
    # 留痕：便于事后核对报告是基于几条告警得出的
    report['group_id'] = getattr(group, 'group_id', '')
    report['alert_count'] = len(getattr(group, 'all_alerts', []) or [])
    report['correlation_type'] = getattr(group, 'correlation_type', 'standalone')
    return report


# ─── Step 1: Neptune 子图 ───────────────────────────────────────────────────

def _get_neptune_subgraph(affected_service: str) -> str:
    """提取受影响服务的多层依赖子图，包括基础设施层"""
    try:
        from neptune import neptune_client as nc
        from neptune import neptune_queries as nq

        # 上游调用者
        callers = nc.results(
            "MATCH (u)-[:Calls|DependsOn]->(n {name:$s}) RETURN u.name AS name, u.tier AS tier",
            {'s': affected_service}
        )
        # 下游依赖
        deps = nc.results(
            "MATCH (n {name:$s})-[:Calls|DependsOn]->(d) RETURN d.name AS name, d.tier AS tier",
            {'s': affected_service}
        )
        # 服务自身属性
        props = nc.results(
            "MATCH (n {name:$s}) RETURN n.tier AS tier, n.recovery_priority AS priority, n.description AS desc",
            {'s': affected_service}
        )

        lines = [f"[系统拓扑]"]
        if props:
            p = props[0]
            lines.append(f"- {affected_service}: tier={p.get('tier','?')}, recovery_priority={p.get('priority','?')}")
        for c in callers:
            lines.append(f"- {c.get('name','?')} --[:Calls]--> {affected_service}")
        for d in deps:
            lines.append(f"- {affected_service} --[:DependsOn]--> {d.get('name','?')}")

        # 基础设施层：Service → Pod → EC2 → AZ（通过图遍历获取）
        try:
            infra_path = nq.q9_service_infra_path(affected_service)
            if infra_path:
                lines.append("")
                lines.append("[基础设施层（图遍历 Service→Pod→EC2→AZ）]")
                for row in infra_path:
                    state = row.get('ec2_state') or 'unknown'
                    state_marker = '⚠️' if state != 'running' else '✅'
                    lines.append(
                        f"- {affected_service} → Pod:{row.get('pod_name','?')}({row.get('pod_status','?')}) "
                        f"→ EC2:{row.get('ec2_id','?')} {state_marker}state={state}, az={row.get('az','?')}"
                    )

            # 基础设施根因探测
            infra_fault = nq.q10_infra_root_cause(affected_service)
            if infra_fault.get('has_infra_fault'):
                lines.append("")
                lines.append("[⚠️ 基础设施层故障（图遍历发现）]")
                for ec2 in infra_fault.get('unhealthy_ec2', []):
                    lines.append(
                        f"- EC2 {ec2.get('ec2_id','?')} state={ec2.get('state','?')} az={ec2.get('az','?')} "
                        f"影响 Pod: {', '.join(ec2.get('affected_pods', []))}"
                    )
                # AZ 影响面
                az_impact = infra_fault.get('az_impact', {})
                if az_impact:
                    lines.append("- AZ 影响分析:")
                    for az, info in az_impact.items():
                        lines.append(f"  - {az}: 总Pod={info['total_pods']}, 受影响={info['affected_pods']}")

                # blast radius：故障 EC2 影响到的其他服务
                ec2_ids = [ec2.get('ec2_id') for ec2 in infra_fault.get('unhealthy_ec2', []) if ec2.get('ec2_id')]
                if ec2_ids:
                    broader = nq.q11_broader_impact(ec2_ids)
                    other_services = set(r.get('service') for r in broader if r.get('service') != affected_service)
                    if other_services:
                        lines.append(f"- 故障 EC2 还影响其他服务: {', '.join(other_services)}")

        except Exception as e:
            logger.warning(f"Neptune infra path failed: {e}")

        # === 历史上下文（Phase A 新增） ===
        try:
            # 同资源历史 Incident（Q17）
            hist_incidents = nq.q17_incidents_by_resource(affected_service, limit=3)
            if hist_incidents:
                lines.append("")
                lines.append("[历史故障记录（同服务 MentionsResource）]")
                for inc in hist_incidents:
                    lines.append(
                        f"- {inc.get('id', '?')} | {inc.get('severity', '?')} | "
                        f"根因: {inc.get('root_cause', '?')} | 修复: {inc.get('resolution', '?')}"
                    )

            # 混沌实验历史（Q18）
            chaos_hist = nq.q18_chaos_history(affected_service, limit=3)
            if chaos_hist:
                lines.append("")
                lines.append("[混沌实验历史]")
                for exp in chaos_hist:
                    degradation = exp.get('degradation', 0) or 0
                    lines.append(
                        f"- {exp.get('fault_type', '?')} | 结果: {exp.get('result', '?')} | "
                        f"恢复: {exp.get('recovery_time', 0)}s | 降级率: {float(degradation):.1%}"
                    )
        except Exception as e:
            logger.warning(f"Historical context query failed (non-fatal): {e}")

        # === 语义相似历史故障（Phase B5 新增）===
        try:
            from search.incident_vectordb import search_similar
            fault_desc = f"{affected_service} 故障 " + " ".join(lines[:5])
            similar = search_similar(fault_desc, top_k=3)
            if similar:
                lines.append("")
                lines.append("[语义相似历史故障（向量搜索）]")
                for s in similar:
                    lines.append(
                        f"- {s.get('incident_id', '?')} | {s.get('severity', '?')} | "
                        f"服务: {s.get('affected_service', '?')} | "
                        f"根因: {s.get('root_cause', '?')} | "
                        f"相似度: {s.get('score', 0):.2f}"
                    )
        except Exception as e:
            logger.warning(f"Semantic incident search failed (non-fatal): {e}")

        return '\n'.join(lines)
    except Exception as e:
        logger.warning(f"Neptune subgraph failed: {e}")
        return f"[系统拓扑]\n- {affected_service}（图谱查询失败）"


# ─── Step 2: CloudWatch 指标 ────────────────────────────────────────────────

def _get_cloudwatch_metrics(affected_service: str, window_minutes: int = 30) -> str:
    """拉取最近 N 分钟的关键指标"""
    try:
        cw = boto3.client('cloudwatch', region_name=REGION)
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)

        # 通用查询：EKS Pod CPU + 内存（ContainerInsights）
        queries = []

        # ALB 5xx（所有服务通用）
        queries.append({
            'Id': 'alb5xx',
            'Expression': 'SUM(SEARCH(\'{AWS/ApplicationELB} HTTPCode_Target_5XX_Count\', \'Sum\', 60))',
            'Label': 'ALB_5xx_total',
            'ReturnData': True,
        })
        # EKS Pod restarts
        # 从 profile 派生的 registry 取维度值（内部会先 resolve 别名）。
        # profile 里的字段名是 dimension_value，取不到则退回服务名本身。
        from config import registry as _registry
        svc_raw = (_registry.get_cloudwatch_config(affected_service).get('dimension_value')
                   or affected_service)
        queries.append({
            'Id': 'cpu',
            'Expression': f'AVG(SEARCH(\'{{ContainerInsights,ClusterName,Namespace,PodName}} pod_cpu_utilization PodName="{svc_raw}"\', \'Average\', 60))',
            'Label': 'Pod_CPU_avg',
            'ReturnData': True,
        })

        resp = cw.get_metric_data(
            MetricDataQueries=queries,
            StartTime=start, EndTime=end
        )

        lines = [f"[CloudWatch 指标（最近 {window_minutes} 分钟）]"]
        for result in resp.get('MetricDataResults', []):
            vals = result.get('Values', [])
            if vals:
                avg = round(sum(vals) / len(vals), 1)
                peak = round(max(vals), 1)
                lines.append(f"- {result['Label']}: 均值={avg}, 峰值={peak}")
            else:
                lines.append(f"- {result['Label']}: 无数据")
        return '\n'.join(lines)
    except Exception as e:
        logger.warning(f"CloudWatch metrics failed: {e}")
        return "[CloudWatch 指标]\n- 查询失败"


# ─── Step 3: 拼 Prompt 调 Bedrock ───────────────────────────────────────────

def generate_rca_report(
    affected_service: str,
    classification: dict,
    rca_result: dict,
    log_samples: dict = None,
    group_context: str = None,
) -> dict:
    """
    Graph RAG 主入口：组装所有数据源 → 调用 Bedrock Claude → 返回结构化报告

    Args:
        affected_service: 受影响服务（聚合场景下传根因候选服务）
        classification: 故障分类结果
        rca_result: rca_engine 的输出
        log_samples: 日志采样
        group_context: 聚合告警的额外上下文（由 generate_group_report 构建）。
            为 None 时行为与原先完全一致，保持向后兼容。
    """
    severity = classification.get('severity', 'P1')
    error_services = rca_result.get('error_services', [])
    changes = rca_result.get('recent_changes', [])
    candidates = rca_result.get('root_cause_candidates', [])

    # 1. Neptune 子图
    subgraph_text = _get_neptune_subgraph(affected_service)

    # 2. CloudWatch 指标
    cw_text = _get_cloudwatch_metrics(affected_service)

    # 2.5 基础设施层（Pod + DB）
    infra_data = infra_collector.collect(affected_service)
    infra_text = infra_collector.format_for_prompt(infra_data)

    # 3. DeepFlow 观测
    df_lines = ["[DeepFlow 调用链观测]"]
    if error_services:
        for s in error_services[:5]:
            df_lines.append(
                f"- {s['service']}: 5xx 开始={s['first_error']}, "
                f"错误次数={s['error_count']}, 错误率={s.get('error_rate_pct',0):.1f}%"
            )
    else:
        df_lines.append("- 当前无 5xx 错误数据（可能是非 HTTP 类故障或数据延迟）")
    df_text = '\n'.join(df_lines)

    # 4. CloudTrail 变更
    ct_lines = ["[近期配置变更（CloudTrail）]"]
    if changes:
        for c in changes[:3]:
            ct_lines.append(f"- {c['time'][:16]} {c['event']} on {c['resource'][:40]}")
    else:
        ct_lines.append("- 无近期变更记录")
    ct_text = '\n'.join(ct_lines)

    # 3b-2. 拓扑变更（图谱侧，CloudTrail 看不见的那一类）
    # CloudTrail 记录 AWS API 级变更（部署/实例停止/RDS 修改/扩缩容），
    # 但它按构造**看不见**两类对依赖图谱最相关的变化:
    #   · 依赖消失 —— A 不再调用 B，这不产生任何 AWS API 调用，是「流量缺席」
    #   · 依赖出现 —— 应用内配置/开关导致 A 开始调 B
    # 二者恰恰是「上游是否还存在」这个根因判断的直接输入，故单列一段。
    tc_lines = ["[拓扑变更（图谱观测，CloudTrail 无法覆盖）]"]
    try:
        from neptune import neptune_queries as _nq
        tc = _nq.q19_topology_changes(affected_service, since_seconds=86400, limit=10)
        if tc:
            import datetime as _dt
            for c in tc:
                try:
                    when = _dt.datetime.fromtimestamp(
                        int(c.get('ts', 0)), _dt.timezone.utc
                    ).strftime('%Y-%m-%d %H:%M:%SZ')
                except (ValueError, OSError, TypeError):
                    when = str(c.get('ts'))
                tc_lines.append(
                    f"- {when} {c.get('kind')}: {c.get('subject')}"
                    f"（{c.get('detail', '')}）"
                )
        else:
            tc_lines.append("- 近 24h 无拓扑变更事件")
    except Exception as e:
        # 变更日志是增量信息，取不到不该让整份报告生成不出来
        logger.warning(f"拓扑变更查询失败（non-fatal）: {e}")
        tc_lines.append(f"- 查询失败，本节跳过: {str(e)[:120]}")
    tc_text = '\n'.join(tc_lines)

    # 3c. 应用日志采样（由 rca_engine.step3c 传入）
    log_lines = ["[应用日志采样（CloudWatch）]"]
    if log_samples:
        for svc_name, lines in log_samples.items():
            log_lines.append(f"--- {svc_name} ---")
            for line in lines[:5]:
                log_lines.append(f"  {line}")
    else:
        log_lines.append("- 无日志采样数据（log_source 未配置或无 ERROR 行）")
    log_text = '\n'.join(log_lines)

    # 4.5 Bedrock KB 语义相似历史案例
    kb_results = _query_kb_similar_incidents(affected_service, rca_result, REGION)

    # 5. 历史 Incident
    hist_lines = ["[历史故障记录（Neptune）]"]
    top = candidates[0] if candidates else {}
    if top.get('evidence'):
        for ev in top['evidence']:
            hist_lines.append(f"- {ev}")
    else:
        hist_lines.append("- 无历史记录")
    hist_text = '\n'.join(hist_lines)

    # KB 结果格式化
    kb_lines = []
    if kb_results:
        for r in kb_results[:3]:
            kb_lines.append(f"- {r}")
    else:
        kb_lines.append("- 无相似历史案例（知识库暂无数据）")
    kb_text = '\n'.join(kb_lines)

    # 6.5 Layer 2 AWS Service Probe 结果
    probe_results = rca_result.get('aws_probe_results', [])
    if probe_results:
        from collectors.aws_probers import format_probe_results, ProbeResult
        # Re-hydrate into ProbeResult objects for formatting
        probe_objs = []
        for r in probe_results:
            pr = ProbeResult(
                service_name=r['service'], healthy=r['healthy'],
                score_delta=0, summary=r['summary'], evidence=r.get('evidence', [])
            )
            probe_objs.append(pr)
        probe_text = format_probe_results(probe_objs)
    else:
        probe_text = "[Layer2 AWS Probers]\nNo anomalies detected across monitored AWS services."

    # 6. 构建 Prompt
    # 聚合场景：把多告警时序与拓扑印证放在最前面 —— 它是判断"谁是源头"的
    # 首要依据，位置靠前能让模型优先采信，而不是被后面的单服务数据带走。
    _group_block = f"{group_context}\n\n" if group_context else ""
    _group_json_field = (
        '  "propagation_analysis": "传播链分析：谁先出问题、拓扑是否解释这个顺序（2-3句）",\n'
        if group_context else ""
    )
    _group_hint = (
        "\n本次是**多条告警的聚合分析**。请特别回答：最早出现的告警是否就是根因源头？"
        "拓扑关系能否解释告警的先后顺序？若时序与拓扑方向矛盾，须明确指出。"
        if group_context else ""
    )
    prompt = f"""你是一位资深 SRE，正在分析 PetSite 微服务平台的故障。
请严格基于以下已验证的系统事实（不要推断图中不存在的关系），输出根因分析报告。{_group_hint}

故障概况：
- 受影响服务：{affected_service}
- 严重度：{severity}

{_group_block}{subgraph_text}

{df_text}

{cw_text}

{ct_text}

{tc_text}

{infra_text}

{probe_text}

{log_text}

{hist_text}

[语义相似历史案例（Bedrock KB）]
{kb_text}

请直接输出以下 JSON 格式（不要用 markdown 代码块包裹，不要加任何解释文字）：
{{
  "root_cause": "根因描述（一句话）",
  "confidence": 数字（0-100，等于下方四项之和），
  "confidence_breakdown": {{
    "deepflow": 数字（有5xx调用链时序证据得40，无得0），
    "cloudtrail": 数字（有近期变更事件得30，无得0），
    "graph": 数字（Neptune图谱确认为链路起点得20，无数据得0），
    "history": 数字（有历史同类Incident得10，无得0）
  }},
  "evidence": ["证据1", "证据2", "证据3"],
{_group_json_field}  "recommended_action": "建议操作",
  "reasoning": "推理过程（3-5句话）",
  "blast_radius": "影响范围描述"
}}

注意：confidence 等于 confidence_breakdown 四项之和，无 DeepFlow 数据时 deepflow 项必须为 0。"""

    # 7. 调用 Bedrock
    try:
        bedrock = boto3.client('bedrock-runtime', region_name=REGION)
        body = json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'max_tokens': 8192,
            'messages': [{'role': 'user', 'content': prompt}]
        })
        resp = bedrock.invoke_model(modelId=BEDROCK_MODEL, body=body)
        resp_body = json.loads(resp['body'].read())
        text = resp_body['content'][0]['text'].strip()

        # 提取 JSON（兼容 Claude 带 ```json ... ``` 包裹的输出）
        import re
        result = None
        logger.info(f"Bedrock raw text (first 300): {repr(text[:300])}")
        # 方法1：去掉 markdown code fence 直接 parse
        stripped = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
        stripped = re.sub(r'\s*```$', '', stripped.strip(), flags=re.MULTILINE).strip()
        try:
            result = json.loads(stripped)
            logger.info("JSON parse: method1 (stripped) succeeded")
        except Exception as e1:
            logger.info(f"JSON parse: method1 failed: {e1}")
        # 方法2：找匹配的 { ... } 最外层（正确处理嵌套大括号）
        if not result:
            start = text.find('{')
            if start != -1:
                depth = 0
                end = -1
                for i in range(start, len(text)):
                    if text[i] == '{':
                        depth += 1
                    elif text[i] == '}':
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if end != -1:
                    try:
                        result = json.loads(text[start:end+1])
                    except Exception:
                        pass
        if not result:
            result = {'root_cause': text[:200], 'confidence': 0, 'evidence': [], 'recommended_action': '', 'reasoning': text[:300]}

        # 确保 root_cause 是字符串（LLM 偶尔返回嵌套对象）
        if isinstance(result.get('root_cause'), dict):
            rc = result['root_cause']
            result['root_cause'] = rc.get('description', rc.get('summary', str(rc)[:200]))
        
        # 确保 confidence 是数字
        conf = result.get('confidence', 0)
        if isinstance(conf, str):
            try:
                result['confidence'] = float(conf.replace('%', ''))
            except ValueError:
                result['confidence'] = 0

        # 钳制到 [0, 100]。2026-08-28 新增。
        # 提示词要求 confidence 等于 confidence_breakdown 四项之和
        # （40+30+20+10 = 100 上限），但 LLM 不可靠地遵守 ——
        # 生产图谱里实测存在 root_cause_confidence = **1.1** 的 Incident，
        # 即 LLM 返回了 110 而 decision_engine 的 `rag_conf / 100.0`
        # 没有上界，直接变成 1.1。
        #
        # 越界时**打 warning**而不是静默修正:静默修正会让我们永远不知道
        # 模型在违反自己的输出契约，而那本身是需要调提示词的信号。
        try:
            raw_conf = float(result.get('confidence', 0) or 0)
        except (TypeError, ValueError):
            raw_conf = 0.0
        if raw_conf < 0 or raw_conf > 100:
            logger.warning(
                f"LLM 返回的 confidence 越界（{raw_conf}），已钳制到 [0,100]。"
                f"提示词要求它等于 confidence_breakdown 四项之和（≤100），"
                f"越界说明模型未遵守输出契约"
            )
        result['confidence'] = max(0.0, min(100.0, raw_conf))

        result['source'] = 'graph_rag_bedrock'
        logger.info(f"Graph RAG report: confidence={result.get('confidence')}, root_cause={result.get('root_cause','')[:60]}")
        return result

    except Exception as e:
        logger.error(f"Bedrock call failed: {e}", exc_info=True)
        # 降级：返回规则引擎的结果
        return {
            'root_cause': top.get('service', affected_service) if top else affected_service,
            'confidence': int((top.get('confidence', 0.3) if top else 0.3) * 100),
            'evidence': top.get('evidence', []) if top else [],
            'recommended_action': '请人工检查',
            'reasoning': '（Bedrock 调用失败，使用规则引擎结果）',
            'blast_radius': f"{severity} 级故障",
            'source': 'rule_engine_fallback',
        }


def _query_kb_similar_incidents(service: str, rca_result: dict, region: str) -> list:
    """使用 Bedrock Knowledge Base 语义搜索相似历史故障案例。

    未配置 BEDROCK_KB_ID 时直接返回空列表 —— 否则会用空 knowledgeBaseId 去调
    retrieve()，每次 RCA 白跑一次注定 ValidationException 的 API 调用。

    历史背景（2026-08-28）：原 KB `petsite-rca-incident-kb-rds` 已删除。
    其语料只有 1 篇种子文档，导致任何查询都返回同一篇，且得分与相关性负相关
    （乱码 0.89 > 真相关查询 0.77），代码里 score > 0.3 的阈值形同虚设，
    结果是每次 RCA 都被注入一条标着「相似度 89%」的伪造先例。
    语义检索改由 S3 Vectors（search/incident_vectordb.py）承担：它有自动写入
    路径，语料随运行增长。若将来重建策划语料库，需先解决得分语义与摄取路径问题。
    """
    if not KB_ID:
        return []
    try:
        import boto3
        client = boto3.client('bedrock-agent-runtime', region_name=region)
        top = (rca_result.get('root_cause_candidates') or [{}])[0]
        evidence = ' '.join(top.get('evidence', []))
        query = f"服务 {service} 故障 {evidence}"

        resp = client.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={'text': query[:500]},
            retrievalConfiguration={'vectorSearchConfiguration': {'numberOfResults': 3}}
        )
        results = []
        for r in resp.get('retrievalResults', []):
            text = r.get('content', {}).get('text', '')
            score = r.get('score', 0)
            if score > 0.3 and text:
                lines = [l for l in text.split('\n') if any(k in l for k in ['根因', '修复', 'MTTR', 'Why', 'rollout'])]
                summary = ' | '.join(lines[:3]) if lines else text[:150]
                results.append(f"(相似度{score:.0%}) {summary}")
        return results
    except Exception as e:
        # 不要把错误信息当作检索结果返回：调用方会把返回值逐条渲染进
        # 提示词的「相似历史案例」小节，于是 "KB查询失败: AccessDenied..." 会被
        # 当成一条历史案例喂给模型。返回空列表，让调用方走「暂无数据」分支。
        logger.warning(f"KB 检索失败(non-fatal): {str(e)[:200]}")
        return []

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

# DeepFlow 服务名 → CloudWatch namespace/dimension 映射
SVC_TO_CW = {
    'payforadoption':     {'ns': 'AWS/ApplicationELB', 'dim': 'pay-for-adoption'},
    'petsite':            {'ns': 'AWS/ApplicationELB', 'dim': 'service-petsite'},
    'petsearch':          {'ns': 'AWS/ApplicationELB', 'dim': 'search-service'},
    'petlistadoptions':   {'ns': 'AWS/ApplicationELB', 'dim': 'list-adoptions'},
    'petadoptionshistory':{'ns': 'AWS/ApplicationELB', 'dim': 'pethistory-service'},
}


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
        svc_raw = SVC_TO_CW.get(affected_service, {}).get('dim', affected_service)
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
) -> dict:
    """
    Graph RAG 主入口：组装所有数据源 → 调用 Bedrock Claude → 返回结构化报告
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
    prompt = f"""你是一位资深 SRE，正在分析 PetSite 微服务平台的故障。
请严格基于以下已验证的系统事实（不要推断图中不存在的关系），输出根因分析报告。

故障概况：
- 受影响服务：{affected_service}
- 严重度：{severity}

{subgraph_text}

{df_text}

{cw_text}

{ct_text}

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
  "recommended_action": "建议操作",
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
    """使用 Bedrock Knowledge Base 语义搜索相似历史故障案例"""
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
        return [f"KB查询失败: {str(e)[:80]}"]

"""把 AgentTool(search_available_pets) -> petsearch 这条桥接边的实测写回图谱。

观测方是**主动探测**（invoke_agent_runtime）而非被动 SLI —— 这是必须的：
aws-samples 的 agentcore-strands-agent-faults 模板 README 明确指出
「a tool-level fault that the agent handles gracefully completes as a successful
invocation, it is invisible to infrastructure error metrics. Surface the agent's
own signal instead.」而且 DeepFlow 的 agent 是 EKS DaemonSet，看不到 AgentCore
托管运行时的 ENI 流量。

三轮实测（同一 agent、同一 prompt）：
  健康 petsearch          26 次调用全部成功，25 次返回 10-11 只幼犬  = 96.2%
  petsearch 业务容器全挂   22 次全部零宠物数据，成功的调用均自述
                          「pet search service temporarily unavailable」 = 0%
  恢复后                   回到 11 只幼犬

注入效力可独立核验：search-service 两个 Pod 处于 1/2 Running、容器重启 +1
（abort 打断 liveness 探针 -> kubelet 杀容器 -> tproxy 残留在 Pod netns）。

一个必须记下的测量陷阱：高并发探测会产生 RuntimeClientError，与「依赖失败」同形。
11 并发时 7/22 报该错，误读成注入效果；降到 2 并发后 26/26 调用成功，
证实那是**探测器自身的假象**，不是依赖信号。
"""
from __future__ import annotations

import sys
import time

sys.path[:0] = ['infra/lambda/shared/python', 'chaos/code']

from graph_confidence import (  # noqa: E402
    classify_intervention, classify_dependency_strength)
from runner.edge_verification import (  # noqa: E402
    candidate_edges, evidence_from_props, write_verdict, confidence)

BASE_N, BASE_GOOD = 26, 25
INJ_N, INJ_GOOD = 22, 0

base_rate = BASE_GOOD / BASE_N * 100
inj_rate = INJ_GOOD / INJ_N * 100
deg = base_rate - inj_rate
print(f"观测方（WaggleAIAdoption 主动探测）有效数据率: "
      f"基线 {base_rate:.1f}% -> 注入期 {inj_rate:.1f}%  退化 {deg:.1f}pp")

cands = candidate_edges('petsearch')
tgt = [c for c in cands if c.get('label') == 'DependsOn']
print(f"petsearch 的 DependsOn 入边: {len(tgt)} 条")

for c in tgt:
    props = c.get('props') or {}
    st, obs, cn, rn = evidence_from_props(props)
    status, reason = classify_intervention(
        BASE_N, INJ_N, deg,
        # 观测方**自己**返回了失败（agent 明确自述服务不可用），
        # 不是吞吐塌陷 —— 所以是 success_rate 通道，可支撑 hard
        evidence_channel='success_rate',
        injection_confirmed=True,          # Pod 1/2 Running + 重启+1，效力可核验
        independent_observing_sources=obs,
        edge_baseline_calls=BASE_N)        # 探测次数即该路径被行使的次数
    dep_class, dep_reason = classify_dependency_strength(
        deg, evidence_channel='success_rate', injection_confirmed=True,
        independent_observing_sources=obs,
        observer_baseline_requests=BASE_N, observer_injected_requests=INJ_N)

    cn2 = cn + (1 if status == 'confirmed' else 0)
    rn2 = rn + (1 if status == 'refuted' else 0)
    v = {
        'edge_id': c['eid'], 'label': c['label'], 'observer': c.get('observer'),
        'status': status,
        'reason': (
            '主动探测 WaggleAIAdoption 的 search_available_pets 工具：petsearch 健康时 '
            f'{BASE_GOOD}/{BASE_N} 次返回 10-11 只幼犬；petsearch 业务容器全挂时 '
            f'{INJ_N} 次全部零宠物数据、成功调用均自述 pet search service '
            'temporarily unavailable。观测方用 agent 自身信号而非基础设施指标'
            '（工具级故障被 agent 优雅处理后计为成功调用，对基础设施指标不可见）。'),
        'confidence': confidence(st, obs, cn2, rn2),
        'degradation_pct': round(deg, 2),
        'evidence_channel': 'success_rate',
        'injection_confirmed': True,
        'edge_baseline_calls': BASE_N,
        'observer_total_calls': BASE_N,
        'observing_sources': obs,
        'dependency_class': dep_class,
        'dependency_class_reason': dep_reason,
        'confirm_count': cn2, 'refute_count': rn2,
        'verified_at': int(time.time()),
        'experiment_id': ('active-probe:WaggleAIAdoption.search_available_pets '
                          'vs petsearch-container-outage'),
    }
    print(f"\n  判定: {status}   强度: {dep_class}   置信度: {v['confidence']}")
    print(f"  独立观测源: {obs}")
    print(f"  存在性理由: {reason[:120]}")
    print(f"  强度理由: {dep_reason[:120]}")
    print(f"  写回: {'成功' if write_verdict(v) else '失败'}")

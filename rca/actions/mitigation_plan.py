"""rca/actions/mitigation_plan.py — 消费 AWS DevOps Agent 的处置计划

## 为什么需要这一层

DevOps Agent **刻意不执行处置**（工作坊首页原话：it deliberately does not
execute remediation）。它产出计划，执行由我们自己的层负责。而计划有一份
官方契约，我们的处置层原先是自己一套分类（`playbook_engine.PLAYBOOKS` +
`semi_auto._exec_*`），两边对不上。

这一层把官方 schema 翻译成 `action_executor` 的原子动作，并在执行前加两道闸。

## 官方 schema（工作坊 Module 2 正文给出）

```json
{
  "planId": "mp-f6g7h8i9j0",
  "investigationId": "inv-a1b2c3d4e5",
  "status": "READY",
  "confidence": "HIGH",
  "actions": [
    { "sequence": 1, "type": "SCALE_OUT", "target": "order-service",
      "parameters": {"desiredCount": 4}, "rationale": "..." }
  ],
  "rollback": { ... }
}
```

## 两道闸，以及为什么是这两道

### 闸一：`confidence` 必须用**我们的图谱证据**交叉验证

计划自称 `HIGH` 只是规划方的自我评价。它的依据是遥测相关性 —— 而相关性
不等于依赖关系。我们手上有别人没有的东西：这条依赖边**被干预验证过没有**。

判据：计划要动的 target，它的依赖边如果只是 `untested`，
那么"动这个 target 能解决问题"这个推断没有实测支撑，
**不该按 HIGH 执行**。降级为需人工审核。

这不是保守，是有实据的：`mcp/README.md` 记着 DevOps Agent 不查图谱时
有 75% 的概率仅凭"FIS 实验模板存在"下结论 —— **模板是意图，不是结果**。

### 闸二：没有 `rollback` 的计划不自动执行

与"故障注入必须可自动恢复"是同一条纪律的另一面：
施加变更之前必须先有撤销路径。混沌注入靠 `spec.duration` 自动到期，
处置动作靠 `rollback` 字段 —— 缺了它，一次失败的处置就是一个新故障，
而且没人知道怎么退回去。

## 默认 Mode 2（人在环）

工作坊自己也推荐"所有场景先走人在环，靠评分卡累积信心后再放开"。
本模块默认 `require_approval=True`，`semi_auto` 已经在这个位置，不重建。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: 官方 action type -> 我们的原子动作。
#:
#: 只登记**我们真的能做**的。登记一个做不到的类型比不登记更糟：
#: 计划会被判为"可执行"，然后在执行那一刻失败，
#: 而此时故障还在、时间已经花掉了。
#: 未知类型一律走人工，见 `plan_to_actions` 的 unsupported 分支。
_TYPE_MAP: dict[str, str] = {
    'SCALE_OUT': 'scale_deployment',
    'SCALE_UP': 'scale_deployment',
    'ROLLBACK': 'rollout_undo',
    'RESTART': 'rollout_restart',
    'ROLLING_RESTART': 'rollout_restart',
}

#: `ISOLATE` 刻意**不**映射。
#:
#: 工作坊用 ECS 服务隔离实现它；我们在 EKS 上，等价做法是改 NetworkPolicy
#: 或摘除 Service endpoint —— 那是**切断流量**，属于我们混沌注入侧的能力，
#: 拿它当"处置"会把一个降级变成一个中断。要做得单独设计，不在这里顺手接。
_KNOWN_UNSUPPORTED = {'ISOLATE', 'FAILOVER', 'REROUTE_TRAFFIC'}

CONFIDENCE_ORDER = {'LOW': 0, 'MEDIUM': 1, 'HIGH': 2}


class PlanRejected(Exception):
    """计划不满足执行前提。带上原因，供审计与通知使用。"""


def _norm_confidence(raw: Any) -> str:
    c = str(raw or '').strip().upper()
    return c if c in CONFIDENCE_ORDER else 'LOW'


def validate_plan(plan: dict) -> tuple[bool, str]:
    """结构校验。返回 (是否可继续, 原因)。

    只看结构，不看证据 —— 证据那一闸在 `gate_plan`。
    分开是为了让结构错误与证据不足给出不同的诊断：
    前者是发来的东西坏了，后者是我们不敢信。
    """
    if not isinstance(plan, dict):
        return False, '计划不是一个对象'
    if not plan.get('planId'):
        return False, '缺 planId —— 无法审计，也无法去重'
    status = str(plan.get('status') or '').upper()
    if status != 'READY':
        return False, f'status={status!r}，只有 READY 的计划可执行'
    actions = plan.get('actions')
    if not isinstance(actions, list) or not actions:
        return False, 'actions 为空 —— 没有可执行的动作'
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            return False, f'actions[{i}] 不是对象'
        if not a.get('type'):
            return False, f'actions[{i}] 缺 type'
        if not a.get('target'):
            return False, f'actions[{i}] 缺 target —— 不知道要动谁'
    return True, 'ok'


def gate_plan(
    plan: dict,
    edge_evidence: Callable[[str], dict] | None = None,
) -> dict:
    """两道闸。返回决策 dict，**不执行任何动作**。

    `edge_evidence(target) -> {'verify_status': ..., 'verify_confidence': ...}`
    由调用方注入（通常查图谱）。传 None 时视为"拿不到证据"，
    按最保守处理 —— 拿不到证据与证据显示未验证，在风险上是同一档。

    返回:
        {
          'allow_auto': bool,        # 是否允许自动执行
          'effective_confidence': str,
          'reasons': [str, ...],     # 每一条降级/拒绝的理由
          'unsupported': [str, ...], # 我们做不到的 action type
        }
    """
    ok, why = validate_plan(plan)
    if not ok:
        raise PlanRejected(why)

    reasons: list[str] = []
    claimed = _norm_confidence(plan.get('confidence'))
    effective = claimed

    # ── 闸二：rollback ──────────────────────────────────────────────
    # 放在前面判，因为它与证据无关、判定最确定。
    if not plan.get('rollback'):
        effective = 'LOW'
        reasons.append(
            '计划没有 rollback —— 施加变更前必须先有撤销路径。'
            '一次失败的处置就是一个新故障，且没人知道怎么退回去。'
            '与「故障注入必须可自动恢复」是同一条纪律。')

    # ── 闸一：用图谱证据交叉验证 confidence ─────────────────────────
    targets = [str(a.get('target')) for a in plan['actions'] if a.get('target')]
    for t in dict.fromkeys(targets):          # 去重且保序
        ev = (edge_evidence(t) if edge_evidence else None) or {}
        vs = str(ev.get('verify_status') or '').lower()
        if vs == 'confirmed':
            continue                          # 有实测支撑，不降级
        if vs == 'refuted':
            raise PlanRejected(
                f'target {t!r} 的依赖边是 refuted —— 图谱曾声称存在、'
                f'故障注入证明不成立。基于一条被证伪的依赖去处置，'
                f'动的可能是无关的东西。拒绝执行。')
        # untested / inconclusive / 查不到 —— 都降到需人工
        effective = 'LOW'
        reasons.append(
            f'target {t!r} 的依赖证据是 {vs or "查不到"} —— '
            f'「动它能解决问题」这个推断没有实测支撑。'
            f'计划自称 {claimed} 是基于遥测相关性，而相关性不是依赖关系。')

    unsupported = sorted({
        str(a.get('type')).upper() for a in plan['actions']
        if str(a.get('type')).upper() not in _TYPE_MAP
    })
    if unsupported:
        effective = 'LOW'
        reasons.append(
            f'含我们无法执行的动作类型 {unsupported} —— '
            f'走人工，不要部分执行后留下半途状态。')

    return {
        'allow_auto': effective == 'HIGH' and not reasons,
        'claimed_confidence': claimed,
        'effective_confidence': effective,
        'reasons': reasons,
        'unsupported': unsupported,
    }


def plan_to_actions(plan: dict) -> list[dict]:
    """把官方计划翻译成内部动作序列，按 `sequence` 排序。

    未知类型不静默丢弃，而是标 `executor=None` 带回去 ——
    静默丢弃会让"执行了 3 个动作"看起来是全部完成，
    而实际上第 4 个被吞了。
    """
    ok, why = validate_plan(plan)
    if not ok:
        raise PlanRejected(why)

    out = []
    for a in sorted(plan['actions'],
                    key=lambda x: int(x.get('sequence') or 0)):
        t = str(a.get('type')).upper()
        params = a.get('parameters') or {}
        item: dict = {
            'sequence': int(a.get('sequence') or 0),
            'type': t,
            'target': a.get('target'),
            'rationale': a.get('rationale') or '',
            'executor': _TYPE_MAP.get(t),
            'kwargs': {},
        }
        if item['executor'] == 'scale_deployment':
            # 官方字段名是 desiredCount（ECS 口径）；我们是 K8s replicas。
            # 两个都收，缺了就不猜 —— 猜一个副本数可能把服务扩到 1 或缩到 0。
            n = params.get('desiredCount', params.get('replicas'))
            if n is None:
                item['executor'] = None
                item['note'] = ('SCALE_OUT 未给 desiredCount/replicas，'
                                '不猜副本数 —— 猜错可能缩到 0')
            else:
                item['kwargs'] = {'replicas': int(n)}
        elif t in _KNOWN_UNSUPPORTED:
            item['note'] = (f'{t} 在 EKS 上的等价做法是切断流量，'
                            f'那属于混沌注入能力而不是处置 —— '
                            f'拿它当处置会把降级变成中断')
        elif item['executor'] is None:
            item['note'] = f'未登记的动作类型 {t}'
        out.append(item)
    return out


def execute_plan(
    plan: dict,
    edge_evidence: Callable[[str], dict] | None = None,
    require_approval: bool = True,
    approved: bool = False,
    dry_run: bool = True,
) -> dict:
    """跑一份计划。**默认 dry_run=True 且需要审批。**

    默认值刻意保守：这个函数会改动生产工作负载，
    调用方必须显式表达"我知道我在做什么"。
    """
    decision = gate_plan(plan, edge_evidence=edge_evidence)
    steps = plan_to_actions(plan)
    result: dict = {'planId': plan.get('planId'), 'decision': decision,
                    'steps': [], 'executed': False}

    if require_approval and not approved:
        result['skipped_reason'] = (
            'Mode 2（人在环）：等待人工审批。'
            + ('' if decision['allow_auto'] else
               ' 注意本计划即使在 Mode 1 也不允许自动执行：'
               + '；'.join(decision['reasons'])))
        return result
    if not decision['allow_auto'] and not approved:
        result['skipped_reason'] = ('闸未通过且无人工批准：'
                                    + '；'.join(decision['reasons']))
        return result

    from . import action_executor as ae
    for s in steps:
        if not s['executor']:
            result['steps'].append({**s, 'result': 'skipped',
                                    'why': s.get('note', '无对应执行器')})
            continue
        fn = getattr(ae, s['executor'], None)
        if fn is None:
            result['steps'].append({**s, 'result': 'skipped',
                                    'why': f"executor {s['executor']} 不存在"})
            continue
        try:
            r = fn(s['target'], dry_run=dry_run, **s['kwargs'])
            result['steps'].append({**s, 'result': 'ok', 'detail': r})
        except Exception as exc:                              # noqa: BLE001
            # 一步失败就停：后续动作可能依赖前一步的结果，
            # 继续执行会让系统落在一个计划里没描述过的中间态。
            result['steps'].append({**s, 'result': 'error',
                                    'detail': repr(exc)})
            result['aborted_at'] = s['sequence']
            break
    result['executed'] = True
    return result

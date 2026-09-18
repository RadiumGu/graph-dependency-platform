"""一次性回填：把 agentcore-etl 的边从 dependency_kind='dynamic' 改成正确的取值，
并修复因此被误置 active=false 的边。

## 为什么需要回填而不是等 ETL 自己刷新

`dependency_kind` 在契约里是 **edge_write_once_attrs**（只在新建时写）。这条规则本身
是对的 —— 它记录「谁首先发现了这条依赖」，被后写的源覆盖等于抹掉发现史。
但代价是：改了 ETL 的写入逻辑，**既有边永远不会被更新**。所以要一次性迁移，
与 migrate_identity_keys.py / backfill_node_timestamp.py 同一类脚本。

## 正确的取值按证据通道分

    static     控制面能独立证明存在、与流量无关
               → RoutesTo（Gateway 的 target 列表）
               → DependsOn（AgentTool → 后端服务，来自 gateway target 的 backend 字段）
    inference  LLM 在运行时按 query 决定的调用，只能从 span 观测到
               → InvokesTool / Delegates / Retrieves

判别依据不是「边类型」而是**它是被哪条采集路径写出来的**。`DependsOn` 两条路径都会写
（控制面的 gateway target 与 span 的 _TOOL_BACKEND 映射），但两者都表示「这个工具打这个
后端」这一**结构**事实，与某次调用无关，所以统一算 static。

## 修复被误置失活的边

`Retrieves -> waggle-ai-nutrition-kb` 在 2026-09-05 被 deactivate_stale_dynamic_edges
置成 active=false，因为它被标了 dynamic 而 6h 内没有观测。那个知识库客观存在
（控制面 list_knowledge_bases 返回它），依赖也真实存在 —— 只是几小时没人问营养问题。
恢复 active=true，并按新语义标 drift_status='observed_then_silent' 表达「确实有一段
时间没观测到」，把不确定性放在 drift_status 而不是伪装成确定的否定。
"""
import argparse
import sys
import time

sys.path.insert(0, 'infra/lambda/shared/python')
from neptune_client_base import neptune_query  # noqa: E402

STATIC_LABELS = ['RoutesTo', 'DependsOn']
INFERENCE_LABELS = ['InvokesTool', 'Delegates', 'Retrieves']
SOURCE = 'agentcore-etl'


def count(q):
    r = neptune_query(q)['result']['data']['@value']
    v = r[0] if r else 0
    return int(v['@value'] if isinstance(v, dict) else v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true',
                    help='真正改写；不给就是 dry-run 只统计')
    args = ap.parse_args()
    now = int(time.time())
    mode = '写入' if args.write else 'dry-run'
    print(f"=== agent 边 dependency_kind 回填（{mode}）===\n")

    total = 0
    for kind, labels in (('static', STATIC_LABELS), ('inference', INFERENCE_LABELS)):
        for lb in labels:
            n = count(f"g.E().hasLabel('{lb}').has('source','{SOURCE}')"
                      f".has('dependency_kind','dynamic').count()")
            tot = count(f"g.E().hasLabel('{lb}').has('source','{SOURCE}').count()")
            print(f"  {lb:<13} 需改 {n}/{tot} 条 → dependency_kind='{kind}'")
            total += n
            if n and args.write:
                neptune_query(
                    f"g.E().hasLabel('{lb}').has('source','{SOURCE}')"
                    f".has('dependency_kind','dynamic')"
                    f".property('dependency_kind','{kind}')"
                    f".property('kind_backfilled_at',{now}).iterate()")
    print(f"\n  合计需改 {total} 条")

    print("\n=== 修复被误置 active=false 的 agent 边 ===")
    wrong = count(f"g.E().has('source','{SOURCE}').has('active',false).count()")
    print(f"  当前 active=false 的 agentcore 边: {wrong} 条")
    if wrong and args.write:
        neptune_query(
            f"g.E().has('source','{SOURCE}').has('active',false)"
            f".property('active',true)"
            f".property('drift_status','observed_then_silent')"
            f".property('unobserved_since',{now})"
            f".property('reactivated_at',{now})"
            f".property('reactivation_reason',"
            f"'wrongly deactivated by dynamic-edge sweep; sparse agent invocation "
            f"means absence of observation is not evidence of absence')"
            f".iterate()")
        print("  已恢复 active=true 并标 drift_status='observed_then_silent'")

    print("\n=== 回填后核对 ===")
    for k in ['dynamic', 'static', 'inference']:
        n = count(f"g.E().has('source','{SOURCE}').has('dependency_kind','{k}').count()")
        print(f"  agentcore 边 dependency_kind='{k}': {n}")
    print(f"  agentcore 边 active=false: "
          f"{count(f'''g.E().has('source','{SOURCE}').has('active',false).count()''')}")
    if not args.write:
        print("\n  （dry-run，未改写。加 --write 执行）")


if __name__ == '__main__':
    main()

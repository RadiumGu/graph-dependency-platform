#!/usr/bin/env python3
"""一次性回填：把节点的 last_updated / last_scanned 迁到契约声明的 TIMESTAMP_FIELD。

## 为什么必须有这一步

节点过期收敛以 TIMESTAMP_FIELD（last_seen）为判据。这个判据本身是对的 ——
2026-09-04 实测过：改用覆盖率高得多的 last_updated 会**误杀活节点**
（7 个 LambdaFunction 由 etl_cfn 独家每日刷新，last_seen 新鲜而 last_updated
已陈旧 7 天以上）。

但只修写入方是不够的，因为**它救不了要被回收的那批节点**：

    511 个死 Pod 不会再被任何 ETL 触碰
      -> 新代码永远不会给它们写 last_seen
      -> 它们永远停留在「不可判定」
      -> 而它们正是这个机制存在的理由

实测印证：修完写入方跑一轮 ETL 后，last_seen 覆盖率从 1.4% 升到 30.4%，
而 node_expiry_unjudgeable 仍有 528 —— 升上去的全是活节点，死节点一个没动。
不做这一步就等于交付一个结构上永不触发的闸门。

## 回填语义

`last_seen := max(已有 last_seen, last_updated, last_scanned)`

取最大值而不是覆盖：某些节点两个字段都有（etl_cfn 写的 LambdaFunction
既有 last_seen 又有 last_scanned），取较新的那个才是「最后一次被谁看到」的
真实答案。回填后：

  · 活节点 —— 值是新鲜的，下一轮 ETL 还会覆盖，不受影响
  · 死节点 —— 值是它最后一次被采集到的时刻，据此判过期是**准确的**

## 安全

纯**增加属性**，不删任何东西、不改任何既有值的语义。默认 dry-run。
回填本身不会让任何节点立刻消失 —— 节点过期收敛默认也是 dry-run，
且只做软标记（active=false），要 GRAPH_NODE_EXPIRY_ENABLED=true 才改写。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from graph_contract import NODE_TYPES, TIMESTAMP_FIELD  # noqa: E402
from neptune_client_base import neptune_query  # noqa: E402

SOURCES = ('last_updated', 'last_scanned')


def one(gremlin):
    v = neptune_query(gremlin)['result']['data']['@value'][0]
    return v['@value'] if isinstance(v, dict) else v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='真的写入（默认只报不改）')
    args = ap.parse_args()

    mode = '执行回填' if args.apply else 'DRY-RUN（只报不改）'
    print(f"节点时间戳回填 -> {TIMESTAMP_FIELD} —— {mode}\n" + '=' * 76)
    print(f"{'类型':<20}{'总数':>6}{'已有':>6}{'可回填':>7}{'仍无':>6}")
    print('-' * 76)

    tot = have = fill = none = 0
    plan = []
    for lb in NODE_TYPES:
        n = one(f"g.V().hasLabel('{lb}').count()")
        if not n:
            continue
        h = one(f"g.V().hasLabel('{lb}').has('{TIMESTAMP_FIELD}').count()")
        src_or = ','.join(f"__.has('{s}')" for s in SOURCES)
        # 缺判据字段、但有可迁移来源的
        f = one(f"g.V().hasLabel('{lb}').not(__.has('{TIMESTAMP_FIELD}'))"
                f".or({src_or}).count()")
        z = one(f"g.V().hasLabel('{lb}').not(__.has('{TIMESTAMP_FIELD}'))"
                f".not(__.or({src_or})).count()")
        tot += n; have += h; fill += f; none += z
        if f or z:
            print(f"{lb:<20}{n:>6}{h:>6}{f:>7}{z:>6}")
        if f:
            plan.append(lb)

    print('-' * 76)
    print(f"{'合计':<20}{tot:>6}{have:>6}{fill:>7}{none:>6}")
    print(f"\n回填后覆盖率将从 {have/tot*100:.1f}% 升到 {(have+fill)/tot*100:.1f}%")
    if none:
        print(f"仍有 {none} 个节点无任何时间戳可迁移 —— 这些是追加式事件日志"
              f"（Incident / ChaosExperiment / TopologyChange）与来自 json 的声明，"
              f"契约已给它们 expires_seconds: null，本就不参与过期判定。")

    if not args.apply:
        print("\n未做任何改动。确认无误后加 --apply 执行。")
        return 0

    print()
    for lb in plan:
        # 逐来源回填，按 SOURCES 顺序后写覆盖前写 —— last_scanned 通常比
        # last_updated 新（etl_cfn 每日跑），所以放后面
        for src in SOURCES:
            neptune_query(
                f"g.V().hasLabel('{lb}').not(__.has('{TIMESTAMP_FIELD}'))"
                f".has('{src}').as('v')"
                f".sideEffect(__.property(single,'{TIMESTAMP_FIELD}',__.values('{src}')))"
                f".iterate()")
        got = one(f"g.V().hasLabel('{lb}').has('{TIMESTAMP_FIELD}').count()")
        print(f"  {lb:<20} 现有 {TIMESTAMP_FIELD} 的节点: {got}")

    total_after = one(f"g.V().has('{TIMESTAMP_FIELD}').count()")
    print(f"\n完成。全图带 {TIMESTAMP_FIELD} 的节点: {total_after}/{tot} "
          f"({total_after/tot*100:.1f}%)")
    print("复核：跑一轮 etl_aws，看 node_expiry_unjudgeable 是否显著下降、"
          "node_expiry_stale 是否浮出真实数字（此时仍是 dry-run，不会改图）")
    return 0


if __name__ == '__main__':
    sys.exit(main())

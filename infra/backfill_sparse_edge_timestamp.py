#!/usr/bin/env python3
"""回填稀疏观测源边的 last_seen —— 让它们从「双向不可见」变成「可见地陈旧」。

## 问题

稀疏观测源（契约 `sparse_observation_sources`，当前是 `deepflow-dns`）的边由
etl_deepflow 的「影子依赖」分支创建。该分支历史上**不写 last_seen**
（2026-09-07 的 01974a8 才补上），于是存量边一个时间戳都没有。

后果不是「过期太慢」，而是**两条清理路径都看不见它们**：

    失效路径  g.E()...has('dependency_kind','dynamic')
                  .not(has('source',within(稀疏源)))     ← 被显式排除（a7f5015）
                  .has('last_seen', lt(cutoff))
    标记路径  g.E()...or(inference, 稀疏源)
                  .has('last_seen', lt(cutoff))          ← 没有 last_seen 就匹配不上

实测 2026-09-07：22 条 deepflow-dns 边，**有 last_seen 的 0 条**，
其中 4 条的 last_drift_check 停在 2026-03-19（172 天前）。它们既不会被失效，
也不会被标陈旧 —— 是双向不可见的永久墓碑。

## 为什么回填是安全的，而且只有现在才安全

先做 a7f5015（把稀疏源从失效路径排除）**是这件事的前提**。在那之前回填
`last_seen` 会让 19/22 条边立刻被 6 小时的 TTL 判 `active=false` ——
即断言「这些依赖不存在」，而能证明的只是「6 小时窗口内没解析」。
那正是 2026-09-05 `Retrieves -> nutrition-kb` 那次错误陈述的复现。

现在失效路径已排除它们，回填只会让**标记路径**看见它们，
结果是打上 `drift_status='observed_then_silent'` + `unobserved_since`，
`active` 不受影响。也就是「可见地陈旧」而非「被判定消失」。

## 基准取 last_drift_check，不取 now()

取 `now()` 等于宣称「刚刚观测到」，那是伪造证据 —— 这批边恰恰是**没有**被
最近观测到才成为问题的。`last_drift_check` 是写入方真实记录的「drift 检查
最后一次处理这条边」的时刻，是手头唯一诚实的下界。

实测 22 条全部有 last_drift_check，无需兜底。若将来出现两者都缺的边，
本脚本报告并跳过 —— 不猜。

幂等：只处理 `hasNot(last_seen)` 的边，重复运行是空操作。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lambda', 'shared', 'python'))

from neptune_client_base import neptune_query  # noqa: E402

try:
    from graph_contract_data import SPARSE_OBSERVATION_SOURCES, TIMESTAMP_FIELD
except ImportError:
    print("无法从契约读 SPARSE_OBSERVATION_SOURCES / TIMESTAMP_FIELD。"
          "请设置 PYTHONPATH 指向 infra/lambda/shared/python，"
          "并确认已跑过 scripts/gen_graph_contract.py --write。", file=sys.stderr)
    raise


def _one(q):
    d = neptune_query(q)['result']['data']['@value']
    v = d[0] if d else 0
    return v['@value'] if isinstance(v, dict) else v


def _rows(q):
    d = neptune_query(q)['result']['data']['@value']
    return d[0]['@value'] if d else []


def _unpack(r):
    it = iter(r['@value'])
    d = dict(zip(it, it))
    return {k: (v['@value'] if isinstance(v, dict) else v) for k, v in d.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='实写；缺省是 dry-run')
    args = ap.parse_args()

    srcs = sorted(SPARSE_OBSERVATION_SOURCES or ())
    if not srcs:
        print("契约未声明 sparse_observation_sources —— 无事可做。"
              "若这不符合预期，检查 gen_graph_contract.py 是否导出了该键。")
        return 0
    vals = ','.join(f"'{s}'" for s in srcs)
    base = f"g.E().has('source',within({vals}))"

    total = _one(base + ".count()")
    missing = _one(base + f".hasNot('{TIMESTAMP_FIELD}').count()")
    fixable = _one(base + f".hasNot('{TIMESTAMP_FIELD}').has('last_drift_check').count()")
    stuck = _one(base + f".hasNot('{TIMESTAMP_FIELD}').hasNot('last_drift_check').count()")

    print(f"稀疏源 {srcs}")
    print(f"  边总数                     {total}")
    print(f"  缺 {TIMESTAMP_FIELD}                {missing}")
    print(f"  可回填（有 last_drift_check） {fixable}")
    print(f"  无基准可回填               {stuck}")

    if stuck:
        # 不猜时间戳。报出来让人处理，而不是拿 now() 蒙一个。
        q = (base + f".hasNot('{TIMESTAMP_FIELD}').hasNot('last_drift_check')"
             ".project('et','src','tgt')"
             ".by(__.label())"
             ".by(__.outV().coalesce(__.values('name'),__.constant('?')))"
             ".by(__.inV().coalesce(__.values('name'),__.constant('?'))).fold()")
        print(f"\n  ⚠️ 以下 {stuck} 条既无 {TIMESTAMP_FIELD} 也无 last_drift_check，"
              f"没有诚实的基准，**跳过而不猜**：")
        for r in [_unpack(x) for x in _rows(q)]:
            print(f"     {r['et']:<14} {r['src'][:26]:<26} -> {r['tgt'][:34]}")

    if not fixable:
        print("\n没有需要回填的边。")
        return 0

    if not args.apply:
        print("\n这是 dry-run。加 --apply 实写。")
        print("回填后这些边会被 mark_stale_inference_edges 标 observed_then_silent，"
              "\nactive 不受影响（失效路径已排除稀疏源）。")
        return 0

    # 一条 Gremlin 完成：last_seen := last_drift_check。
    # 边属性天然单值，无需 single 基数。
    q = (base + f".hasNot('{TIMESTAMP_FIELD}').has('last_drift_check')"
         f".property('{TIMESTAMP_FIELD}', __.values('last_drift_check'))"
         ".property('timestamp_backfilled_from','last_drift_check')"
         ".iterate()")
    neptune_query(q)

    after_missing = _one(base + f".hasNot('{TIMESTAMP_FIELD}').count()")
    print(f"\n已回填。缺 {TIMESTAMP_FIELD} 的边：{missing} -> {after_missing}")
    if after_missing != stuck:
        print(f"  ⚠️ 预期剩余 {stuck} 条（无基准），实际 {after_missing} 条 —— 请核查。")
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

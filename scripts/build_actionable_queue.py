#!/usr/bin/env python3
"""生成「真正可执行」的待验边清单。

## 判据（三道，缺一道就会混进打不了的边）

    ① verify_status = untested            还没有结论
    ② verify_blocked_class IS NULL        不属于「四类未测」里已知打不了的三类
    ③ active IS NOT false                 图谱自己没把它判定为已不存活   ← 容易漏

## ③ 是我 2026-09-17 上一轮漏掉的

上一轮只用了 ①②，于是清单里混进 5 条 `active=False` 的边。其中两条差点被拿去
做故障注入：

    payforadoption -> serviceseks2-databasewriter2462cc03   active=False, last_seen 142h
    pethistory     -> serviceseks2-databasewriter2462cc03   active=False, last_seen 142h

它们是**一次 Aurora 故障转移留下的痕迹**。实例的名字与角色是相反的：

    serviceseks2-databasereader1f54479b8   IsClusterWriter = True    ← 名叫 reader，是写实例
    serviceseks2-databasewriter2462cc03    IsClusterWriter = False   ← 名叫 writer，是读实例

`last_seen` 呈互补模式，正是角色互换的指纹：

    目标（真实角色）              payforadoption  pethistory  petlistadoptions
    …reader1f54479b8（写）        0h ✅confirmed  0h ✅        142h
    …writer2462cc03（读）         142h            142h         0h ✅confirmed

也就是说 6 天前 payforadoption/pethistory 连的是 `writer2462cc03`（那时它是写实例），
转移后改连 `reader1f54479b8`。前者的 L4 流量记录随之陈旧，
并被 `deactivate_stale_dynamic_edges` 正确置为 `active=False`。

**过期机制是好的，是我的清单判据不完整。** 若真去打这两条，会得到
「重启当前读实例、那两个服务毫无反应」—— 一个**可预知且会误导**的 soft：
读者会以为这两个服务对数据库是软依赖，而事实是它们连的根本不是这个实例。

## 顺带记一条：不要按名字推断 Aurora 实例角色

`classify_modeling_artifacts.py` 的输出末尾早就写着
「角色快照 2026-09-15：databasereader1f54479b8=writer，databasewriter2462cc03=reader
（名字与角色相反，勿按名字推断）」。每次要用到角色时都应现查
`describe-db-clusters` 的 `DBClusterMembers[].IsClusterWriter` ——
故障转移随时会再翻一次。
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / 'rca'), str(_ROOT / 'dr-plan-generator')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from neptune import neptune_client as nc                    # noqa: E402
from neptune.neptune_queries import _dependency_edge_labels  # noqa: E402


def _chokepoints() -> list:
    """割点名单。承重判据用它 —— 割点关联边错了，blocked 数（爆炸半径）就错。"""
    from graph import queries as dq
    return [c['chokepoint'] for c in (dq.q_articulation_chokepoints() or [])
            if c.get('chokepoint')]


def build() -> dict:
    labels = ",".join(f"'{x}'" for x in _dependency_edge_labels())
    chokes = _chokepoints()

    rows = nc.results(
        f"MATCH (a)-[e]->(b) WHERE type(e) IN [{labels}] "
        "AND coalesce(e.verify_status,'untested') = 'untested' "
        "AND e.verify_blocked_class IS NULL "
        "RETURN coalesce(a.name,a.arn) AS src, labels(a)[0] AS src_label, "
        "type(e) AS edge_type, coalesce(b.name,b.arn) AS dst, "
        "labels(b)[0] AS dst_label, coalesce(e.active, true) AS active, "
        "e.dependency_kind AS kind, e.source AS source, e.last_seen AS last_seen "
        "ORDER BY dst, src")

    inactive = [r for r in rows if r.get('active') is False]
    live = [r for r in rows if r.get('active') is not False]
    core = [r for r in live
            if r['src'] in chokes or r['dst'] in chokes]

    by_target = collections.Counter((r['dst'], r['dst_label']) for r in core)
    return {
        '_generated_by': 'scripts/build_actionable_queue.py',
        '_criterion': {
            'untested': "verify_status = untested",
            'not_blocked': "verify_blocked_class IS NULL —— 排除已知打不了的三类",
            'active': "active IS NOT false —— 图谱自己没判定它已不存活（**上一版漏了这道**）",
            'core': "源或目标在 q_articulation_chokepoints 给出的割点名单上",
        },
        'counts': {
            'untested_unblocked': len(rows),
            'excluded_inactive': len(inactive),
            'actionable': len(live),
            'actionable_core': len(core),
        },
        'excluded_inactive_edges': inactive,
        'core_by_target': [
            {'target': d, 'label': dl, 'edges': n}
            for (d, dl), n in by_target.most_common()],
        'actionable_core_edges': core,
        'actionable_noncore_edges': [r for r in live if r not in core],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='')
    args = ap.parse_args()

    d = build()
    c = d['counts']
    print(f"  未测且未分类            {c['untested_unblocked']} 条")
    print(f"  其中 active=False 排除  {c['excluded_inactive']} 条")
    print(f"  ▶ 真正可执行            {c['actionable']} 条")
    print(f"    其中承重（割点关联）  {c['actionable_core']} 条")
    if d['excluded_inactive_edges']:
        print("\n  被排除的（图谱已判定不存活，打它们只会得到可预知的 soft）:")
        for r in d['excluded_inactive_edges']:
            print(f"   {str(r['src'])[:20]:22s}-{r['edge_type'][:12]:13s}->"
                  f"{str(r['dst'])[-30:]:32s} src={r['source']}")
    print("\n  承重边按目标聚合:")
    for t in d['core_by_target']:
        print(f"   {str(t['target'])[:46]:48s} [{str(t['label'])[:14]:15s}] "
              f"{t['edges']} 条")

    if args.out:
        p = pathlib.Path(args.out)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str),
                     encoding='utf-8')
        print(f"\n  已写 {p}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

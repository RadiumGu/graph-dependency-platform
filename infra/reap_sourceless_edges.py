"""物理删除「无 source 且已陈旧失活」的 dependency 边。

## 判据是三条同时成立，不是「没 source 就删」

    ① 没有 `source`         —— 没有任何源认领它，永远不会被刷新或被 reconcile 清理
    ② `active=false`        —— 已被 TTL 过期收敛判定过（不是我这个脚本自己判的）
    ③ `last_seen` 超 TTL 的 N 倍（默认 100 倍）—— 远超「可能只是暂时没流量」的范围

三条缺一不可。只看 ① 会删掉刚被创建、还没轮到写 source 的边；只看 ①② 会删掉
昨天才失活、今天可能恢复的边。加 ③ 是因为 `Calls` 的 TTL 只有 1800s，
超 100 倍 = 50 小时，而实测这批边是 170~185 天没被观测到 —— 差了 80 倍以上。

## 为什么是物理删除而不是继续留着 active=false

`active=false` 的边仍会出现在不带 `has('active', true)` 过滤的查询里
（实测 UI 的「关系明细」就是这样，那三条 `→ petsite` 才会被看见）。
而它们既补不上 source（事后无从推断当初是哪个源写的），也不会被任何源清理 ——
留着只会让每个读图的人重新问一遍「这三条为什么数据源是空的」。

## 为什么不做成 ETL 的自动清理

这批边的成因是历史性的（创建时那条代码路径还没写 source），修完写入侧之后
不会再产生新的。做成常驻自动清理，等于给一个已经不会复发的问题装一个
长期运行的删除器 —— 那才是危险的。所以是一次性脚本 + 默认 dry-run。
"""
import argparse
import sys
import time

sys.path.insert(0, 'infra/lambda/shared/python')
from graph_contract import EDGE_TYPES  # noqa: E402
from neptune_client_base import neptune_query  # noqa: E402

STALE_MULTIPLIER = 100


def rows(q):
    r = neptune_query(q)['result']['data']['@value']
    if not r:
        return []
    out = []
    for x in r[0]['@value']:
        it = iter(x['@value'])
        d = dict(zip(it, it))
        out.append({k: (v['@value'] if isinstance(v, dict) else v)
                    for k, v in d.items()})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='真正删除；不给就是 dry-run')
    ap.add_argument('--multiplier', type=int, default=STALE_MULTIPLIER,
                    help='last_seen 需超过 TTL 的多少倍（默认 100）')
    args = ap.parse_args()
    now = int(time.time())
    print(f"=== 无 source 的陈旧 dependency 边清理（"
          f"{'写入' if args.write else 'dry-run'}，阈值 = TTL × {args.multiplier}）===\n")

    total = 0
    victims = []
    for label, spec in sorted(EDGE_TYPES.items()):
        if spec.get('dependency') is not True:
            continue
        ttl = spec.get('expires_seconds')
        if not ttl:
            continue
        cutoff = now - ttl * args.multiplier
        sel = (f"g.E().hasLabel('{label}').hasNot('source')"
               f".has('active',false).has('last_seen',lt({cutoff}))")
        rs = rows(sel + ".project('s','d','ls','id').by(__.outV().values('name'))"
                        ".by(__.inV().values('name')).by(values('last_seen'))"
                        ".by(id()).fold()")
        if not rs:
            continue
        print(f"【{label}】TTL={ttl}s，阈值 {ttl * args.multiplier}s"
              f"（{ttl * args.multiplier / 86400:.1f} 天）→ {len(rs)} 条")
        for r in rs:
            days = (now - int(r['ls'])) / 86400
            print(f"    {str(r['s'])[:24]:<26} -> {str(r['d'])[:22]:<24} "
                  f"{days:>6.1f} 天未观测")
            victims.append((label, r['id']))
        total += len(rs)
        if args.write:
            neptune_query(sel + ".drop().iterate()")
            print(f"    → 已删除 {len(rs)} 条")

    print(f"\n  合计 {total} 条")
    if not args.write:
        print("  （dry-run，未删除。加 --write 执行）")
        return

    print("\n=== 删除后核对 ===")
    for label, spec in sorted(EDGE_TYPES.items()):
        if spec.get('dependency') is not True:
            continue
        r = neptune_query(f"g.E().hasLabel('{label}').hasNot('source').count()"
                          )['result']['data']['@value']
        v = r[0] if r else 0
        n = int(v['@value'] if isinstance(v, dict) else v)
        if n:
            print(f"  {label}: 仍有 {n} 条无 source（未达清理判据，保留）")


if __name__ == '__main__':
    main()

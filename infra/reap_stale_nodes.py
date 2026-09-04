#!/usr/bin/env python3
"""节点回收 —— 清掉「当前采集策略说不该在图里、但历史上已写入」的残留节点。

## 为什么需要它

边有过期收敛（infra/lambda/shared/python/graph_cleanup.py + T-272），
节点没有。契约里结构边的 `expires_seconds: None` 写的是「生命周期跟随两端
节点」—— 但节点根本没有生命周期机制，所以这句话在实现上是悬空的。

代价在 2026-09-04 的契约审计里兑现：TargetGroup 声明了 `preferred: arn`，
18 个节点里 14 个已回填 arn，4 个永远不会有，于是身份键切换被永久阻塞。
而契约里那条 note 写的解锁条件是「待存量都带上 arn 后再切」——
一个**不可满足**的前提。

## 两条回收规则（形态相同、成因完全不同）

规则 A · 采集策略残留
    节点名命中当前的 skip 前缀 —— 即**现行策略说它不该被采集**，
    但它在 skip 规则加进代码之前就已经写进图里了。skip 只挡住刷新，
    从不回收旧数据。这与 source 词表门禁那个坑同型：
    「代码已挡住新的、旧的还在库里」。
    前缀**从 etl_aws/config.py 读**，不在这里硬写 —— 否则两处会漂移，
    而漂移的方向必然是这个脚本删掉策略其实想保留的东西。

规则 B · 源端已消失
    节点对应的 AWS 资源已经不存在了。判据必须是**向 AWS 实查一遍清单**，
    不能只看时间戳陈旧 —— 2026-09-04 实测 4 个「看起来都死了」的
    TargetGroup 里有 1 个（openclaw-tg-v2）在 AWS 侧活得很好，只是被
    规则 A 排除了采集。只按陈旧度删会销毁一个活资源的节点。

## 安全

默认 **dry-run**，只报不改。要 `--apply` 才真的删。
删顶点会连带删掉其上所有边（Neptune 的 drop() 语义），所以报告里会一并
列出即将失去的边数。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
for p in (REPO / 'infra' / 'lambda' / 'shared' / 'python', REPO / 'infra' / 'lambda' / 'etl_aws'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from neptune_client_base import neptune_query, safe_str  # noqa: E402


def q(gremlin):
    return neptune_query(gremlin)['result']['data']['@value']


def one(gremlin):
    v = q(gremlin)[0]
    return v['@value'] if isinstance(v, dict) else v


def _unwrap(d, k):
    v = d.get(k)
    return v['@value'] if isinstance(v, dict) else v


def _rows(gremlin):
    out = []
    for r in q(gremlin)[0]['@value']:
        it = iter(r['@value'])
        out.append(dict(zip(it, it)))
    return out


def _age_days(raw) -> float | None:
    if isinstance(raw, str):
        try:
            raw = dt.datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
        except Exception:
            return None
    if not raw:
        return None
    return (dt.datetime.now(dt.timezone.utc).timestamp() - raw) / 86400


# ── 规则 A：采集策略残留 ──────────────────────────────────────────────────

def skip_prefix_rules() -> list[tuple[str, tuple[str, ...]]]:
    """从 etl_aws 的配置读出 (节点类型, 跳过前缀) —— 单一数据源，不在此硬写。"""
    import config as etl_config
    return [
        ('TargetGroup', tuple(getattr(etl_config, 'SKIP_TG_PREFIXES', ()))),
        ('LambdaFunction', tuple(getattr(etl_config, 'CDK_LAMBDA_SKIP_PREFIXES', ()))),
    ]


def find_policy_residue() -> list[dict]:
    found = []
    for label, prefixes in skip_prefix_rules():
        if not prefixes:
            continue
        for row in _rows(
            f"g.V().hasLabel('{label}').project('n','lu','ls','e')"
            f".by('name').by(coalesce(values('last_updated'),constant('')))"
            f".by(coalesce(values('last_scanned'),constant('')))"
            f".by(__.bothE().count()).fold()"
        ):
            name = _unwrap(row, 'n')
            hit = next((p for p in prefixes if name.startswith(p)), None)
            if not hit:
                continue
            found.append({
                'label': label, 'name': name,
                'age': _age_days(_unwrap(row, 'lu') or _unwrap(row, 'ls')),
                'edges': _unwrap(row, 'e'),
                'rule': f"A·策略残留（命中 skip 前缀 {hit!r}）",
            })
    return found


# ── 规则 B：源端已消失（目前只覆盖 TargetGroup）────────────────────────────

def find_absent_target_groups() -> list[dict]:
    """向 AWS 实查目标组清单，图里有而 AWS 没有的即为源端已消失。

    刻意只覆盖 TargetGroup：规则 B 要求「该类型有一份可信的权威清单」，
    每种类型都得单独把这份清单做出来才敢用。把它推广到别的类型之前，
    必须先为那个类型写出等价的实查逻辑 —— 拿陈旧度当代理判据会误删活资源。
    """
    import boto3
    region = os.environ.get('REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'ap-northeast-1'
    elb = boto3.client('elbv2', region_name=region)
    live = set()
    for page in elb.get_paginator('describe_target_groups').paginate():
        live |= {tg['TargetGroupName'] for tg in page['TargetGroups']}
    if not live:
        raise RuntimeError('AWS 侧目标组清单为空 —— 拒绝据此判定「全部已消失」')

    found = []
    for row in _rows(
        "g.V().hasLabel('TargetGroup').project('n','lu','ls','e')"
        ".by('name').by(coalesce(values('last_updated'),constant('')))"
        ".by(coalesce(values('last_scanned'),constant('')))"
        ".by(__.bothE().count()).fold()"
    ):
        name = _unwrap(row, 'n')
        if name in live:
            continue
        found.append({
            'label': 'TargetGroup', 'name': name,
            'age': _age_days(_unwrap(row, 'lu') or _unwrap(row, 'ls')),
            'edges': _unwrap(row, 'e'),
            'rule': 'B·源端已消失（AWS describe-target-groups 无此名）',
        })
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='真的删除（默认只报不改）')
    ap.add_argument('--only-label', help='限定只处理某个节点类型')
    ap.add_argument('--rule', choices=['A', 'B', 'both'], default='both')
    args = ap.parse_args()

    cands = []
    if args.rule in ('A', 'both'):
        cands += find_policy_residue()
    if args.rule in ('B', 'both'):
        cands += find_absent_target_groups()
    if args.only_label:
        cands = [c for c in cands if c['label'] == args.only_label]

    # 同一节点可能同时命中两条规则，按 (label,name) 去重并保留全部命中理由
    merged: dict[tuple[str, str], dict] = {}
    for c in cands:
        k = (c['label'], c['name'])
        if k in merged:
            merged[k]['rule'] += ' + ' + c['rule']
        else:
            merged[k] = c
    cands = sorted(merged.values(), key=lambda c: (c['label'], c['name']))

    mode = '执行删除' if args.apply else 'DRY-RUN（只报不改）'
    print(f"节点回收 —— {mode}\n" + '=' * 78)
    if not cands:
        print('没有命中任何回收规则。')
        return 0

    print(f"{'类型':<16}{'名字':<48}{'陈旧':>8}{'边':>4}")
    print('-' * 78)
    for c in cands:
        nm = c['name'] if len(c['name']) <= 46 else '…' + c['name'][-45:]
        age = f"{c['age']:.1f}d" if c['age'] is not None else '无戳'
        print(f"{c['label']:<16}{nm:<48}{age:>8}{c['edges']:>4}")
        print(f"                └─ {c['rule']}")

    total_edges = sum(c['edges'] for c in cands)
    print('-' * 78)
    print(f"共 {len(cands)} 个节点，连带删除 {total_edges} 条边")

    if not args.apply:
        print("\n未做任何改动。确认无误后加 --apply 执行。")
        return 0

    print()
    for c in cands:
        n = one(f"g.V().hasLabel('{c['label']}').has('name','{safe_str(c['name'])}')"
                f".sideEffect(__.drop()).count()")
        print(f"  已删除 {c['label']}/{c['name'][:50]} (匹配 {n})")
    print(f"\n完成。复核：GRAPH_LIVE_AUDIT=true python3.11 -m pytest "
          f"tests/test_42_contract_meta_fields.py -q")
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""身份键迁移 —— 把节点从「以 name 匹配」切到「以不可变属性匹配」前的必备步骤。

## 为什么必须先跑这个

profiles/graph_contract.yaml 把若干类型的身份键从可变的 `name` 改成了不可变
属性（Subnet→subnet_id、VPC→vpc_id、SecurityGroup→sg_id、TargetGroup→arn）。
写入侧随之从 `mergeV([label, name])` 改成 `mergeV([label, <id_key>])`。

**切换本身会造重复**：如果活图谱里某个节点没有那个 id 属性，
新的 mergeV 匹配不到它，就会新建一个 —— 旧节点带着历史属性永远孤立。
这正是 EC2Instance 曾经发生过的事（14 个节点里 4 个是重复实体）。

所以部署顺序必须是：
    1. 跑本脚本 --audit   看清有多少节点缺 id 属性、有多少 id 值撞车
    2. 跑本脚本 --apply   回填缺失的 id 属性、合并撞车的重复节点
    3. 才部署新的 ETL 代码

## 三类处理

- **缺 id 属性**：能从既有属性推出就回填（例如 TargetGroup 的 arn 此前只在
  采集侧的循环变量里、没写进图谱）。推不出的**只报告不猜** —— 用推断值当身份
  等于制造假数据。
- **id 值撞车**：同一个 id 值对应多个节点 = 已经是重复实体。保留 last_seen /
  last_updated 最新的那个，把另一个的边改挂过去再删除。
- **id 值唯一且齐全**：无需动作，切换后 mergeV 会精确命中。

默认 --audit（只读）。--apply 才写。
"""
from __future__ import annotations

import argparse
import collections
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'infra' / 'lambda' / 'shared' / 'python'))

import yaml  # noqa: E402

CONTRACT = yaml.safe_load((REPO / 'profiles' / 'graph_contract.yaml').read_text())


def _neptune_query(gremlin: str):
    """走共享层的 SigV4 客户端。放在函数里 import 是为了让 --help 不需要凭证。"""
    from neptune_client_base import neptune_query
    return neptune_query(gremlin)


def _values(resp) -> list:
    return (resp or {}).get('result', {}).get('data', {}).get('@value', [])


def _unwrap(v):
    """展开 Gremlin/GraphSON 的类型包装。

    `g:Map` 的 @value 是**扁平的键值交替列表** [k1,v1,k2,v2,...] 而不是字典，
    所以必须特判 —— 否则 project(...) 的结果会是 list，取 .get() 直接 AttributeError。
    """
    if isinstance(v, dict):
        if v.get('@type') == 'g:Map':
            flat = [_unwrap(x) for x in v.get('@value', [])]
            return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
        if '@value' in v:
            return _unwrap(v['@value'])
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def affected_types() -> dict[str, str]:
    """契约里身份键不是 name 的类型 —— 只有这些需要迁移。"""
    return {lb: spec['identity']
            for lb, spec in CONTRACT['node_types'].items()
            if spec.get('identity') and spec['identity'] != 'name'}


def audit_type(label: str, id_key: str) -> dict:
    """统计该类型的节点总数、缺 id 属性的数量、id 值撞车情况。"""
    total = _values(_neptune_query(f"g.V().hasLabel('{label}').count()"))
    total = _unwrap(total[0]) if total else 0

    missing = _values(_neptune_query(
        f"g.V().hasLabel('{label}').not(has('{id_key}')).values('name').fold()"))
    missing = _unwrap(missing[0]) if missing else []

    rows = _values(_neptune_query(
        f"g.V().hasLabel('{label}').has('{id_key}')"
        f".project('name','id','vid')"
        f".by('name').by('{id_key}').by(id).fold()"))
    rows = _unwrap(rows[0]) if rows else []

    by_id = collections.defaultdict(list)
    for r in rows:
        by_id[r.get('id')].append(r)
    collisions = {k: v for k, v in by_id.items() if len(v) > 1}

    return {'label': label, 'id_key': id_key, 'total': total,
            'missing': missing, 'with_id': len(rows), 'collisions': collisions}


def print_audit(results: list[dict]) -> bool:
    """打印审计结果，返回「是否需要 --apply」。"""
    need = False
    print(f"{'类型':<18}{'身份键':<14}{'节点数':>7}{'带id':>7}{'缺id':>7}{'撞车':>7}")
    print('-' * 62)
    for r in results:
        nm, nc = len(r['missing']), len(r['collisions'])
        if nm or nc:
            need = True
        print(f"{r['label']:<18}{r['id_key']:<14}{r['total']:>7}"
              f"{r['with_id']:>7}{nm:>7}{nc:>7}")
    for r in results:
        if r['missing']:
            print(f"\n[缺 {r['id_key']}] {r['label']} 共 {len(r['missing'])} 个：")
            for n in r['missing'][:20]:
                print(f"    {n}")
            if len(r['missing']) > 20:
                print(f"    ... 另有 {len(r['missing']) - 20} 个")
            print(f"  → 切换后这些节点不会被 mergeV 命中，会各新建一个重复节点。")
        if r['collisions']:
            print(f"\n[{r['id_key']} 撞车] {r['label']} 共 {len(r['collisions'])} 组：")
            for idv, group in list(r['collisions'].items())[:10]:
                names = ', '.join(str(g.get('name')) for g in group)
                print(f"    {idv} → {len(group)} 个节点: {names}")
            print(f"  → 这些已经是重复实体，--apply 会保留最新的一个并改挂边。")
    return need


def merge_group(label: str, id_key: str, idv, group: list, apply: bool) -> None:
    """把一组共享同一 id 值的节点合并成一个。

    保留策略：last_seen / last_updated 最大者。取不到时间戳就保留第一个 ——
    此时不做删除，只报告，避免凭任意顺序丢数据。
    """
    stamped = []
    for g in group:
        vid = g.get('vid')
        ts = _values(_neptune_query(
            f"g.V('{vid}').coalesce(values('last_seen'),values('last_updated'),"
            f"constant(0)).fold()"))
        ts = _unwrap(ts[0]) if ts else [0]
        stamped.append((ts[0] if ts else 0, vid, g.get('name')))
    stamped.sort(reverse=True)
    if stamped[0][0] == 0:
        print(f"  ! {label} {idv}: 全组都没有时间戳，跳过合并（不敢凭顺序删）")
        return
    keep_ts, keep_vid, keep_name = stamped[0]
    for ts, vid, name in stamped[1:]:
        print(f"  合并 {label} {idv}: 删 {name}(vid={vid}, ts={ts}) "
              f"保留 {keep_name}(ts={keep_ts})")
        if not apply:
            continue
        # 把出边与入边改挂到保留节点上，再删除。
        # 用 addE 复制而非移动 —— Gremlin 没有移动边的原语；重复边由目标侧的
        # coalesce upsert 语义吸收（同 (src,label,dst) 只会有一条）。
        _neptune_query(
            f"g.V('{vid}').outE().as('e').inV().as('dst').select('e').label().as('lb')"
            f".select('dst').addE(select('lb')).from(V('{keep_vid}')).iterate()")
        _neptune_query(
            f"g.V('{vid}').inE().as('e').outV().as('src').select('e').label().as('lb')"
            f".select('src').addE(select('lb')).to(V('{keep_vid}')).iterate()")
        _neptune_query(f"g.V('{vid}').drop()")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--audit', action='store_true', default=True, help='只读审计（默认）')
    g.add_argument('--apply', action='store_true', help='执行回填与合并')
    ap.add_argument('--only', help='只处理某一个类型')
    args = ap.parse_args()

    if not os.environ.get('NEPTUNE_ENDPOINT'):
        sys.exit("需要 NEPTUNE_ENDPOINT 环境变量")

    types = affected_types()
    if args.only:
        if args.only not in types:
            sys.exit(f"{args.only} 的身份键就是 name，不需要迁移。"
                     f"需要迁移的是: {sorted(types)}")
        types = {args.only: types[args.only]}

    print(f"契约 v{CONTRACT['version']}：身份键非 name 的类型 {len(types)} 个 "
          f"→ {', '.join(f'{k}={v}' for k, v in sorted(types.items()))}\n")

    results = [audit_type(lb, key) for lb, key in sorted(types.items())]
    need = print_audit(results)

    if not need:
        print("\n无需迁移：所有节点都带身份属性且无撞车，切换后 mergeV 会精确命中。")
        return

    if not args.apply:
        print("\n以上是只读审计。确认后加 --apply 执行。")
        return

    print("\n=== 执行合并 ===")
    for r in results:
        for idv, group in r['collisions'].items():
            merge_group(r['label'], r['id_key'], idv, group, apply=True)
    print("\n完成。建议重跑 --audit 复核，再部署新 ETL 代码。")


if __name__ == '__main__':
    main()

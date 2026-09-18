#!/usr/bin/env python3.11
"""
归并图谱里重复的 EC2Instance 节点。

## 为什么会有重复

`upsert_vertex` 原先以 `name` 作身份键，而 EC2 的 name 来自 **Name 标签** ——
可变属性。标签一加/改，mergeV 匹配不到旧节点就新建一个，
旧节点带着当时的属性永远孤立在图里。

实测（2026-08-29）：14 个 EC2Instance 里 **4 个是重复实体**，
4 台 EKS 工作节点各有两份 —— 一份在实例还没打 Name 标签时以实例 ID 命名、
88 天前停止更新，一份以 Name 标签命名。

写入侧已修（`identity_prop='instance_id'`，见 neptune_client.py），
所以归并后不会再生。**顺序很重要：必须先部署写入侧修复，再归并** ——
反过来的话下一轮 ETL 立刻把重复造回来（与 last_scanned 那个坑同理）。

## 为什么要迁移边而不是直接删

实测边数差异悬殊但两侧都有量：42 vs 3、31 vs 3、32 vs 3，
以及 **35 vs 16** —— 后者那 16 条边直接删就是丢数据。

## 存活方的选择

以**边数最多**者为存活方。刻意**不用 last_updated 判断**：
写入侧改用 instance_id 匹配后，mergeV 在两个同 instance_id 的节点间
命中哪一个是**不确定的**，实测两份节点的 last_updated 已经变成完全相同，
按时间选等于抛硬币。边是真实的连接关系，边多的那份承载的信息更多。

用法：
    python3.11 infra/merge_duplicate_ec2_nodes.py            # 干跑（默认）
    python3.11 infra/merge_duplicate_ec2_nodes.py --apply    # 实际执行
"""
import os
import sys
import argparse
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'rca')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings
warnings.filterwarnings('ignore')
from neptune import neptune_client as nc  # noqa: E402


def find_duplicates() -> dict:
    """返回 {instance_id: [ {vid, name, deg}, ... ]}，只含重复的。"""
    rows = nc.results("""
        MATCH (n:EC2Instance) WHERE n.instance_id IS NOT NULL
        WITH n.instance_id AS iid, collect(n) AS ns WHERE size(ns) > 1
        UNWIND ns AS n
        RETURN iid AS iid, id(n) AS vid, n.name AS name,
               size([(n)-[r]-() | r]) AS deg
    """, {})
    grouped = defaultdict(list)
    for r in rows:
        grouped[r['iid']].append(
            {'vid': r['vid'], 'name': r['name'], 'deg': int(r['deg'] or 0)})
    return dict(grouped)


def edges_of(vid: str) -> list:
    """列出某节点的全部边（含方向、标签、对端 vid、属性）。"""
    out = []
    for direction, pattern in (('out', '(n)-[r]->(m)'), ('in', '(m)-[r]->(n)')):
        rows = nc.results(f"""
            MATCH (n) WHERE id(n) = $vid
            MATCH {pattern}
            RETURN type(r) AS label, id(m) AS other, properties(r) AS props
        """, {'vid': vid})
        for r in rows:
            out.append({'dir': direction, 'label': r['label'],
                        'other': r['other'], 'props': r.get('props') or {}})
    return out


def migrate_edge(surv_vid: str, e: dict, apply: bool) -> str:
    """
    把一条边迁到存活方。已存在同标签同对端同方向的边则跳过。

    全程用 openCypher —— `rca/neptune/neptune_client.py` 只暴露
    query()/results() 两个 openCypher 入口，没有 Gremlin 通道。
    不为此另开一条 Gremlin 连接：混用两套查询语言写同一批数据，
    是把「两个实现掩盖同一个缺陷」这个坑再挖一遍。
    """
    other = e['other']
    label = e['label']
    if e['dir'] == 'out':
        exists_q = (f"MATCH (s)-[r:{label}]->(o) WHERE id(s)=$s AND id(o)=$o "
                    f"RETURN count(r) AS n")
    else:
        exists_q = (f"MATCH (o)-[r:{label}]->(s) WHERE id(s)=$s AND id(o)=$o "
                    f"RETURN count(r) AS n")
    n = nc.results(exists_q, {'s': surv_vid, 'o': other})
    if n and int(n[0]['n'] or 0) > 0:
        return 'exists'
    if not apply:
        return 'would-create'

    # 属性走参数化，不做字符串拼接 —— 边属性可能含引号。
    props = e['props'] or {}
    set_clause = ''
    params = {'s': surv_vid, 'o': other}
    if props:
        parts = []
        for i, (k, v) in enumerate(props.items()):
            pk = f'p{i}'
            parts.append(f"r.{k} = ${pk}")
            params[pk] = v
        set_clause = ' SET ' + ', '.join(parts)

    if e['dir'] == 'out':
        create_q = (f"MATCH (s), (o) WHERE id(s)=$s AND id(o)=$o "
                    f"CREATE (s)-[r:{label}]->(o){set_clause} RETURN 1 AS x")
    else:
        create_q = (f"MATCH (s), (o) WHERE id(s)=$s AND id(o)=$o "
                    f"CREATE (o)-[r:{label}]->(s){set_clause} RETURN 1 AS x")
    nc.results(create_q, params)
    return 'created'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='实际执行；缺省为干跑')
    args = ap.parse_args()

    dupes = find_duplicates()
    if not dupes:
        print("没有发现重复的 EC2Instance 节点。")
        return 0

    print(f"发现 {len(dupes)} 个重复实体（按 instance_id 归并）\n")
    total_migrated = total_skipped = total_deleted = 0

    for iid, nodes in sorted(dupes.items()):
        nodes.sort(key=lambda x: -x['deg'])
        survivor, losers = nodes[0], nodes[1:]
        print(f"── {iid} ──")
        print(f"   存活: name={survivor['name']} 边数={survivor['deg']} "
              f"vid={survivor['vid']}")
        for loser in losers:
            print(f"   合并: name={loser['name']} 边数={loser['deg']} "
                  f"vid={loser['vid']}")
            for e in edges_of(loser['vid']):
                r = migrate_edge(survivor['vid'], e, args.apply)
                mark = {'exists': '已存在', 'created': '已迁移',
                        'would-create': '将迁移'}[r]
                if r == 'exists':
                    total_skipped += 1
                else:
                    total_migrated += 1
                print(f"      [{mark}] {e['dir']:3} -[{e['label']}]- "
                      f"{str(e['other'])[:28]}")
            if args.apply:
                nc.results("MATCH (n) WHERE id(n)=$vid DETACH DELETE n",
                           {'vid': loser['vid']})
                print(f"      → 已删除该节点")
            else:
                print(f"      → 将删除该节点")
            total_deleted += 1
        print()

    verb = '已' if args.apply else '将'
    print(f"汇总：{verb}迁移 {total_migrated} 条边，"
          f"跳过 {total_skipped} 条（对端已有同类边），"
          f"{verb}删除 {total_deleted} 个重复节点")
    if not args.apply:
        print("\n这是干跑。加 --apply 实际执行。")
    return 0


if __name__ == '__main__':
    sys.exit(main())

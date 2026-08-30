#!/usr/bin/env python3
"""清理端点组合未声明的边 —— 契约驱动，不硬编码要删什么。

## 缺陷来源

`etl_aws/neptune_client.py:find_vertex_by_name(name)` 原本**不带标签**：
缓存迭代丢弃 label 按名匹配，回退查询 `g.V().has('name',X).limit(1)` 任取其一。
本图有 12 组名字跨标签重复（`gateway-service` 同时是 Deployment / K8sService /
Microservice），于是返回哪个节点取决于 ETL 步骤先后。

2026-08-30 活图谱实测后果：

    形态                                    条数    应为
    (Namespace)-[RunsOn]->(Pod)              65    不该存在（chaos-mesh /
                                                   deepflow 是基础设施 Pod，
                                                   没有对应 Microservice）
    (K8sService)-[RunsOn]->(Pod)             63    (Microservice)-[RunsOn]->(Pod)
    (Deployment)-[RunsOn]->(Pod)             45    同上
    (Deployment)-[Manages]->(K8sService)      6    ->(Microservice)
    (Deployment)-[Manages]->(Deployment)      4    ->(Microservice)
                                            ---
                                            183    占全图边数的 10%

正确的 `(Microservice)-[RunsOn]->(Pod)` 当时只有 36 条 —— 也就是说这类边
**83% 的源端点是错的**，影响面分析从 Microservice 出发遍历 RunsOn 会漏掉大部分 Pod。

## 为什么按契约扫而不是写死 5 条 DROP

写死的清理脚本只对「今天这一批」有效，下次换个形态又得改脚本。
按 `profiles/graph_contract.yaml` 的 `pairs` 扫，等于让清理和门禁共用同一份
判据：门禁拦住新的、这个脚本清掉存量的，两边永远不会对不齐。

## 顺序要求

必须**先部署修好的函数代码**再跑 `--apply`。否则旧代码下一轮 ETL 会把这些
错源边原样重建，白删一次。`--audit` 任何时候都能跑。

用法:
    python3.11 infra/fix_wrong_source_edges.py            # 只审计（默认）
    python3.11 infra/fix_wrong_source_edges.py --apply    # 真正删除
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'infra' / 'lambda' / 'shared' / 'python'))

from neptune_client_base import neptune_query  # noqa: E402

CONTRACT = REPO / 'profiles' / 'graph_contract.yaml'


def _flat_map(m):
    v = m['@value'] if isinstance(m, dict) else m
    return {v[i]: (v[i + 1]['@value'] if isinstance(v[i + 1], dict) else v[i + 1])
            for i in range(0, len(v), 2)}


def survey() -> dict[tuple[str, str, str], int]:
    """普查全图的 (srcLabel, edgeLabel, dstLabel) 三元组及各自条数。"""
    r = neptune_query(
        "g.E().project('s','e','d')"
        ".by(__.outV().label()).by(__.label()).by(__.inV().label())"
        ".groupCount().by(__.select('s','e','d'))"
    )
    data = r['result']['data']['@value'][0]['@value']
    out = {}
    for i in range(0, len(data), 2):
        kv = _flat_map(data[i])
        n = data[i + 1]['@value'] if isinstance(data[i + 1], dict) else data[i + 1]
        out[(kv['s'], kv['e'], kv['d'])] = n
    return out


def classify(triples, edge_types):
    """按契约 pairs 把三元组分成合规 / 违约两组。"""
    ok, bad = {}, {}
    for (s, e, d), n in triples.items():
        spec = edge_types.get(e)
        if spec is None:
            bad[(s, e, d)] = (n, f"边类型 {e} 未声明")
        elif [s, d] not in spec.get('pairs', []):
            declared = ', '.join(f'{a}->{b}' for a, b in spec.get('pairs', []))
            bad[(s, e, d)] = (n, f"端点组合未声明；已声明: {declared or '(无)'}")
        else:
            ok[(s, e, d)] = n
    return ok, bad


def drop_shape(s: str, e: str, d: str) -> int:
    """删除某一形态的全部边，返回删除条数。"""
    q = (f"g.E().hasLabel('{e}')"
         f".where(__.outV().hasLabel('{s}'))"
         f".where(__.inV().hasLabel('{d}'))")
    r = neptune_query(q + ".count()")
    v = r['result']['data']['@value'][0]
    before = v['@value'] if isinstance(v, dict) else v
    neptune_query(q + ".drop().iterate()")
    return int(before or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真正删除；缺省只审计')
    args = ap.parse_args()

    if not os.environ.get('NEPTUNE_ENDPOINT'):
        print("需要 NEPTUNE_ENDPOINT；注意客户端读的是 REGION 而非 AWS_REGION")
        return 2

    edge_types = yaml.safe_load(CONTRACT.read_text())['edge_types']
    triples = survey()
    ok, bad = classify(triples, edge_types)
    n_ok = sum(ok.values())
    n_bad = sum(n for n, _ in bad.values())

    print(f"全图 {n_ok + n_bad} 条边 / {len(triples)} 种形态")
    print(f"  合规 {n_ok} 条（{len(ok)} 种）")
    print(f"  违约 {n_bad} 条（{len(bad)} 种）\n")

    if not bad:
        print("没有端点违约的边。")
        return 0

    for (s, e, d), (n, why) in sorted(bad.items(), key=lambda x: -x[1][0]):
        print(f"  {n:>5}x  ({s})-[{e}]->({d})")
        print(f"         {why}")

    if not args.apply:
        print(f"\n[审计模式] 未做任何改动。加 --apply 才会删除这 {n_bad} 条。")
        print("提醒：必须先部署修好的函数代码，否则下一轮 ETL 会原样重建。")
        return 0

    print(f"\n[APPLY] 开始删除 {len(bad)} 种形态共 {n_bad} 条边 ...")
    total = 0
    for (s, e, d) in bad:
        try:
            n = drop_shape(s, e, d)
            total += n
            print(f"  已删 {n:>5} 条  ({s})-[{e}]->({d})")
        except Exception as ex:
            print(f"  失败 ({s})-[{e}]->({d}): {ex}")

    print(f"\n共删除 {total} 条。复核：")
    ok2, bad2 = classify(survey(), edge_types)
    print(f"  合规 {sum(ok2.values())} 条 / 违约 {sum(n for n, _ in bad2.values())} 条")
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""标记**粒度影子边** —— 同一个依赖在图谱里被两个粒度各记一次。

    python3 scripts/mark_granularity_duplicates.py          # 只报告
    python3 scripts/mark_granularity_duplicates.py --apply  # 写回

## 问题

同一条依赖会被两条边表示，因为发现管道落在不同粒度上：

    petsite -PublishesTo-> SNSTopic:ServicesEks2-topicpetadoption…   confirmed   aws-etl
    petsite -AccessesData-> AWSServiceEndpoint:sns                   （空）      appsignals-etl

两条都是**真的**，但它们不是两个依赖。后果有两层：

1. **覆盖率分母把一个依赖算两行、只给一次学分。** 实测 6/47 = 13%。
2. **更糟：证据散落在不同粒度上且互不一致。** 实测：

       petsearch → S3            端点 inconclusive（真跑过实验） / 资源 空
       payforadoption → DynamoDB 端点 bootstrap_only              / 资源 空
       petsite → SNS             端点 空                          / 资源 confirmed
       petsite → StepFunctions   端点 空                          / 资源 confirmed

   有时判定落在粗粒度、有时落在细粒度。**DR 影响面分析单读任一侧都只得到
   局部图景**，而这正是 `verify_dependency_class` 的下游消费者在做的事。

## 为什么用 `verify_assessability` 而不是 `verify_status`

`verify_status` 回答「取到了什么证据」。「这条边是另一条边的重复」**不是一个
证据陈述**，塞进去就是又一次把两套轴挤进一个字段 —— 本仓库为此付过两次代价
（`verify_evidence_channel` 混轴、`verify_dependency_class` 被我写进
platform_pull）。

`verify_assessability` 回答「这条边为什么（不）能靠切断实验拿到 confirmed」。
影子边拿不到独立判定，因为**它不是一个独立的依赖**：切断实验的作用域是资源
ARN（IAM deny / FIS 都是），天然落在细粒度那条边上。所以 `granularity_duplicate`
属于这个轴。

## 关键：不能一律把粗粒度那侧排除

证据落在哪一侧**不一致**，一律排除粗粒度会把真实证据从分母里藏掉 ——
`petsearch → s3` 的 `inconclusive` 是**真跑过实验采集来的事实**，
本仓库的不变式是它不能被覆盖、也不能被悄悄移出分母。

所以分三组，只有 A 组进「可排除」：

    A 影子边无判定、对侧有判定    -> 标记并可排除（不丢证据）
    B 判定在粗粒度、细粒度是空    -> **标记但拒绝排除**，报成「待归并」
    C 两侧都无判定                -> 标记并可排除（没有证据可丢）

B 组的正解是把证据归并到细粒度那条边上，那是一次**语义搬迁**，
需要逐条确认手段的作用域是否真的适用于资源粒度，不在本脚本内自动做。
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in ('..', os.path.join('..', 'chaos', 'code'),
          os.path.join('..', 'infra', 'lambda', 'shared', 'python'),
          os.path.join('..', 'infra', 'lambda', 'rca_window_flush')):
    sys.path.insert(0, os.path.abspath(os.path.join(_HERE, p)))

#: 端点名 -> 承载同一个 AWS 服务的**资源**节点标签。
#:
#: 为什么需要这张表：图谱节点上已有 `granularity`（`AWSServiceEndpoint` 是
#: `'service'`），但「dynamodb 这个端点与 DynamoDBTable 这个标签是同一个服务」
#: 这层对应关系不在 schema 里。放一处并配理由，不要在各 ETL 里各抄一份 ——
#: 本仓库的依赖边清单曾四处各抄一份，其中两处漂移到实际错误。
EP_TO_RESOURCE_LABEL = {
    'sns': 'SNSTopic',
    'sqs': 'SQSQueue',
    'stepfunctions': 'StepFunction',
    'dynamodb': 'DynamoDBTable',
    's3': 'S3Bucket',
    'secretsmanager': 'Secret',
}
#: 有意不含 `ssm` / `sts` / `xray`：图谱里没有对应的资源级节点类型，
#: 它们的端点边**不是影子**，是那条依赖唯一的表示。
#: 漏掉这个判断会把 4 条真实的唯一边误标成重复。
_NO_RESOURCE_NODE = frozenset({'ssm', 'sts', 'xray'})

KIND = 'granularity_duplicate'


def _edge_ids(nc, service: str, target: str) -> list[str]:
    """取 service -> target 之间全部边的 id。

    用 openCypher（本文件其余部分也走它），不要混进 Gremlin 客户端 ——
    仓库里两套客户端并存，接口不同（`nc.results(cypher)` vs
    `query_gremlin_parsed(gremlin)`），混用过一次。
    """
    rows = nc.results(
        "MATCH (s)-[r]->(d) WHERE s.name = $sn AND d.name = $dn "
        "RETURN id(r) AS eid, type(r) AS rel",
        {'sn': service, 'dn': target})
    return [(r.get('eid'), r.get('rel')) for r in rows if r.get('eid')]


def find_shadows(rows: list[dict]) -> dict:
    """把影子边分成 A/B/C 三组。纯函数，便于门禁直接喂造数据。"""
    by_src: dict = {}
    for r in rows:
        by_src.setdefault(r.get('service'), []).append(r)
    groups: dict = {'A': [], 'B': [], 'C': [], 'skipped_no_resource': []}
    for svc, rs in by_src.items():
        by_label: dict = {}
        for r in rs:
            by_label.setdefault(r.get('target_label') or '?', []).append(r)
        for r in rs:
            if (r.get('target_label') or '') != 'AWSServiceEndpoint':
                continue
            ep = (r.get('target') or '').lower()
            if ep in _NO_RESOURCE_NODE:
                groups['skipped_no_resource'].append((svc, ep))
                continue
            res_label = EP_TO_RESOURCE_LABEL.get(ep)
            if not res_label:
                continue
            peers = by_label.get(res_label) or []
            if not peers:
                continue
            shadow_st = r.get('verify_status') or ''
            peer_st = [p.get('verify_status') or '' for p in peers]
            item = {'service': svc, 'ep': ep, 'shadow_status': shadow_st,
                    'peer_label': res_label,
                    'peer_target': peers[0].get('target'),
                    'peer_status': peer_st[0]}
            if not shadow_st and any(peer_st):
                groups['A'].append(item)
            elif shadow_st and not any(peer_st):
                groups['B'].append(item)
            elif not shadow_st and not any(peer_st):
                groups['C'].append(item)
            else:
                # 两侧都有判定：重复但没有证据会被藏掉，按 A 处置
                groups['A'].append(item)
    return groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    from compliance_export.queries import fetch_function_mapping, _dep_labels
    from neptune import neptune_client as nc
    rows = fetch_function_mapping(nc, _dep_labels())
    g = find_shadows(rows)

    print("=== A 组：影子边无判定（或两侧都有），标记并可排除 %d 条 ===" % len(g['A']))
    for i in g['A']:
        print("  %-16s 端点:%-14s st=%-16s ← %s:%s st=%s" % (
            i['service'], i['ep'], i['shadow_status'] or '空',
            i['peer_label'], (i['peer_target'] or '')[:32], i['peer_status'] or '空'))
    print("\n=== B 组：判定在粗粒度、细粒度是空 —— **标记但拒绝排除** %d 条 ===" % len(g['B']))
    for i in g['B']:
        print("  %-16s 端点:%-14s st=%-16s ← %s:%s st=空" % (
            i['service'], i['ep'], i['shadow_status'],
            i['peer_label'], (i['peer_target'] or '')[:32]))
    if g['B']:
        print("  ↑ 排除这些会把**真跑过实验采集来的事实**从分母里藏掉。")
        print("    正解是把证据归并到细粒度那条边，属语义搬迁，需逐条确认")
        print("    切断手段的作用域是否真的适用于资源粒度，本脚本不自动做。")
    print("\n=== C 组：两侧都无判定，标记并可排除 %d 条 ===" % len(g['C']))
    for i in g['C']:
        print("  %-16s 端点:%-14s ← %s:%s" % (
            i['service'], i['ep'], i['peer_label'], (i['peer_target'] or '')[:32]))
    if g['skipped_no_resource']:
        print("\n=== 刻意跳过 %d 条：图谱无对应资源节点类型，端点边是该依赖唯一表示 ==="
              % len(g['skipped_no_resource']))
        for svc, ep in sorted(set(g['skipped_no_resource'])):
            print("  %-16s 端点:%s" % (svc, ep))

    tot = len(rows)
    excludable = len(g['A']) + len(g['C'])
    print("\n=== 口径影响 ===")
    print("  全部边                     %d" % tot)
    print("  粒度影子边（A+B+C）        %d" % (excludable + len(g['B'])))
    print("  其中可排除（A+C）          %d  -> 去重后分母 %d" % (excludable, tot - excludable))
    print("  拒绝排除（B，待归并）      %d" % len(g['B']))

    if not a.apply:
        print("\n（只报告。加 --apply 写回 verify_assessability=%s）" % KIND)
        return 0

    from runner.edge_verification import write_assessability
    n = 0
    for grp in ('A', 'B', 'C'):
        for i in g[grp]:
            reason = ("与 %s:%s 是同一依赖的两个粒度（端点级 vs 资源级）；"
                      "切断实验作用域是资源 ARN，独立判定应落在资源粒度那条边。"
                      "分组 %s%s" % (
                          i['peer_label'], i['peer_target'], grp,
                          "（判定在本条粗粒度边上，**拒绝移出分母**，待归并）"
                          if grp == 'B' else ""))
            for eid, rel in _edge_ids(nc, i['service'], i['ep']):
                if write_assessability(eid, KIND, reason):
                    n += 1
                    print("  ✓ %s -%s-> %s" % (i['service'], rel, i['ep']))
    print("\n  已标记 %d 条边的 verify_assessability=%s" % (n, KIND))
    print("  ⚠️ 刻意**未碰** verify_status —— 影子关系不是证据陈述。")
    return 0


if __name__ == '__main__':
    sys.exit(main())

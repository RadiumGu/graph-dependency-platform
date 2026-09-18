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

#: 切断手段 -> 该手段证据的**作用域粒度**。
#:
#: ## 为什么规范侧不能固定选细粒度
#:
#: 第一版规则写的是「切断实验作用域是资源 ARN，所以规范侧恒为资源粒度」。
#: 2026-09-17 逐条复核 B 组时发现**不成立**：
#:
#:     petsearch -> s3  的实验是 FIS disrupt-connectivity **scope=s3**
#:       —— 网络层、按 AWS **服务**切，不是按桶 ARN 切。
#:       证据天然属于端点粒度那条边，规范侧是粗粒度那条。
#:
#:     payforadoption -> dynamodb 的证据是源码审计，点名
#:       `repository.go:529 db.Table(...)` 具体到那张表 —— 规范侧是资源粒度。
#:
#: 所以规范侧**由证据的作用域决定**。这不是细节：选错规范侧会把
#: 一条服务级切断的结论挂到某个具体资源上，等于声称「我们验证过这个桶」，
#: 而实际验证的是「到 S3 这个服务的连通性」—— 那是范围虚报，
#: 与本仓库 severance 标错的三次是同一类问题。
_SEVERANCE_SCOPE_GRANULARITY = {
    'iam-deny': 'resource',        # 策略 Resource 写的是资源 ARN
    'fis-network': 'service',      # FIS disrupt-connectivity scope=<服务>
    'fis-reboot': 'resource',      # 针对具体实例
    'source-audit': 'resource',    # 源码里点名的是具体资源
}
#: 实验 id 里出现这些片段时按此推断作用域 —— 早期实验没写 verify_severance
#: （报告 §11.2 披露的「7 条未记录切断手段」）。**只用于推断作用域，
#: 不用于追认手段** —— 追认手段是虚假陈述，本报告明确拒绝那样做。
_EXPERIMENT_HINT_GRANULARITY = (
    ('fis-network-disrupt', 'service'),
    ('network-disrupt', 'service'),
    ('fis', 'resource'),
)


def evidence_granularity(status: str, severance: str, experiment: str) -> str:
    """判断这条边的证据是**服务级**还是**资源级**作用域。

    拿不准时返回 `'unknown'` —— 那时**拒绝归并**，不猜。
    猜错的代价是把结论挂到错误的粒度上，构成范围虚报。
    """
    sev = (severance or '').strip().lower()
    if sev in _SEVERANCE_SCOPE_GRANULARITY:
        return _SEVERANCE_SCOPE_GRANULARITY[sev]
    exp = (experiment or '').lower()
    for frag, gran in _EXPERIMENT_HINT_GRANULARITY:
        if frag in exp:
            return gran
    return 'unknown'


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
                    'peer_status': peer_st[0],
                    'severance': r.get('verify_severance'),
                    'experiment': r.get('verify_experiment'),
                    'reason': r.get('verify_reason'),
                    'channel': r.get('verify_evidence_channel'),
                    'confidence': r.get('confidence'),
                    'evidence_granularity': evidence_granularity(
                        shadow_st, r.get('verify_severance'),
                        r.get('verify_experiment'))}
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



def _consolidate(nc, g: dict, apply: bool) -> int:
    """把 B 组的判定搬到**规范侧**那条边。

    规范侧由证据作用域决定（见 `_SEVERANCE_SCOPE_GRANULARITY`）：

        作用域 service  -> 端点粒度那条本来就是规范侧，**无需搬迁**，
                           改把资源粒度那条标成重复
        作用域 resource -> 判定要搬到资源粒度那条边
        作用域 unknown  -> **拒绝搬迁**，原样留着并报出来

    ## 搬迁时必须原样带走 severance / channel / experiment

    改写其中任何一项就是在合规产物上**错标证据来源**。本仓库为此付过三次
    代价：一条用 16 秒实例重启验证的边被标成 `iam-deny`，而两种手段的
    适用范围差得很远（IAM deny 期间调用一直失败，覆盖「依赖不可用」；
    实例重启只是瞬时中断，**明确不覆盖「数据库彻底不可用」**）。
    报告按 severance 披露适用范围，标错就是虚假陈述。

    所以搬迁是**逐字复制**，只换 edge_id，并在 reason 末尾追加一句搬迁说明
    （让读者能查到这条判定不是在这个粒度上直接测出来的）。
    """
    from runner.edge_verification import write_verdict
    import time as _t
    moved = skipped = 0
    for i in g['B']:
        gran = i['evidence_granularity']
        if gran != 'resource':
            print("  ⏭ %s -> %s：作用域=%s，%s"
                  % (i['service'], i['ep'], gran,
                     "端点粒度本就是规范侧，不搬" if gran == 'service'
                     else "**判不出作用域，拒绝搬迁**"))
            skipped += 1
            continue
        tgt_ids = _edge_ids(nc, i['service'], i['peer_target'])
        if not tgt_ids:
            print("  ✗ %s -> %s：找不到资源粒度那条边 %s，拒绝搬迁"
                  % (i['service'], i['ep'], i['peer_target']))
            skipped += 1
            continue
        reason = (str(i.get('reason') or '')
                  + "｜【粒度归并】本判定原记录在服务粒度边 "
                    "AWSServiceEndpoint:%s 上，因证据作用域是**资源级**"
                    "（severance=%s）而搬到这条资源粒度边。"
                    "证据本身未重新采集，severance / evidence_channel / "
                    "experiment 逐字保留。" % (i['ep'], i['severance']))
        for eid, rel in tgt_ids:
            print("  %s %s -%s-> %s  搬入 status=%s"
                  % ("→" if apply else "[dry]", i['service'], rel,
                     (i['peer_target'] or '')[:40], i['shadow_status']))
            if not apply:
                continue
            ok = write_verdict({
                'edge_id': eid, 'label': rel, 'observer': i['service'],
                'status': i['shadow_status'], 'reason': reason,
                'confidence': i.get('confidence') or 0.4,
                'verified_at': int(_t.time()),
                'evidence_channel': i.get('channel') or 'unknown',
                'severance': i.get('severance') or '',
                'verifier': 'granularity-consolidation',
                'experiment_id': i.get('experiment') or '',
                'confirm_count': 0, 'refute_count': 0,
            })
            moved += 1 if ok else 0
    print("\n  搬迁 %d 条，跳过 %d 条%s" % (moved, skipped,
          "" if apply else "（dry-run，加 --apply 生效）"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--consolidate', action='store_true',
                    help='把 B 组的判定搬到规范侧那条边（只搬作用域判得出的）')
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
    for i in g['B']:
        gran = i['evidence_granularity']
        canon = ("粗粒度（端点）本身就是规范侧" if gran == 'service'
                 else "资源粒度是规范侧，需把判定搬过去" if gran == 'resource'
                 else "**作用域判不出 -> 拒绝归并**")
        print("    作用域=%-8s severance=%-12s -> %s"
              % (gran, i['severance'] or '未记录', canon))
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

    if a.consolidate:
        return _consolidate(nc, g, apply=a.apply)

    if not a.apply:
        print("\n（只报告。加 --apply 写回 verify_assessability=%s；"
              "加 --consolidate 归并 B 组）" % KIND)
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

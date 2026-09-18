#!/usr/bin/env python3
"""X-Ray GetServiceGraph 快照 —— 用于量化 Transaction Search 对 etl_xray 的影响面。

为什么要有这个脚本而不是临时敲命令：
  基线与开启后的对比，**测量代码本身必须完全一致**，否则测量差异会混进结论里。
  这与本项目已有的纪律同源 —— 缩短注入时长会制造出正好要防的假 refuted。

它只测 `GetServiceGraph`，因为 etl_xray 只调这一个 API（见
todo/agentobv/05-etl_xray影响面量化_20260904-0835.md 第二节）。刻意不测 Neptune
里的边数：那要经过 ETL 的合并语义与失效判定，会把 ETL 逻辑混进「X-Ray API 行为是否
变化」这个问题里。

用法：
  python3 xray_servicegraph_snapshot.py --label before   # 存基线
  python3 xray_servicegraph_snapshot.py --label after    # 存对比
  python3 xray_servicegraph_snapshot.py --compare before after
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

import boto3

REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
LOADGEN_ID = 'i-05f0b897988a48d17'          # petsite-loadgen，最重要的混淆变量
WINDOWS_HOURS = [1, 6]                      # 多窗口取样，避免单窗口抖动误导
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', 'todo', 'agentobv', 'snapshots')


def _svc_key(s):
    """服务的身份 —— 名字 + 类型。单看名字不够：X-Ray 会用多个条目表示同一个
    Lambda（服务本体 Type=None、AWS::Lambda 容器、AWS::Lambda::Function 执行）。"""
    return f"{s.get('Name')}|{s.get('Type')}"


def measure():
    x = boto3.client('xray', region_name=REGION)
    now = int(time.time())
    out = {
        'captured_at': now,
        'captured_at_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'region': REGION,
        'windows': {},
    }

    for hours in WINDOWS_HOURS:
        start = now - hours * 3600
        svcs = []
        try:
            p = x.get_paginator('get_service_graph')
            for page in p.paginate(StartTime=start, EndTime=now):
                svcs.extend(page.get('Services', []) or [])
        except Exception as exc:  # noqa: BLE001
            out['windows'][f'{hours}h'] = {'error': str(exc)}
            continue

        by_ref = {s.get('ReferenceId'): s for s in svcs}
        edges = []
        for s in svcs:
            for e in (s.get('Edges') or []):
                tgt = by_ref.get(e.get('ReferenceId'))
                if tgt:
                    edges.append(f"{_svc_key(s)} -> {_svc_key(tgt)}")

        out['windows'][f'{hours}h'] = {
            'service_count': len(svcs),
            'edge_count': len(edges),
            'type_distribution': dict(Counter(str(s.get('Type')) for s in svcs)),
            # 存**集合**而不只是计数 —— 计数相同不代表内容相同（3 个消失 3 个出现
            # 会让计数看不出任何变化）。集合差集才是真正的判据。
            'services': sorted(_svc_key(s) for s in svcs),
            'edges': sorted(edges),
            'has_summary_statistics': any('SummaryStatistics' in s for s in svcs),
        }

    # ── 混淆变量与被测配置，必须与测量同时记录 ──
    try:
        ec2 = boto3.client('ec2', region_name=REGION)
        r = ec2.describe_instances(InstanceIds=[LOADGEN_ID])
        out['loadgen_state'] = r['Reservations'][0]['Instances'][0]['State']['Name']
    except Exception as exc:  # noqa: BLE001
        out['loadgen_state'] = f'unknown: {exc}'

    try:
        out['trace_segment_destination'] = x.get_trace_segment_destination()
        out['trace_segment_destination'].pop('ResponseMetadata', None)
    except Exception as exc:  # noqa: BLE001
        out['trace_segment_destination'] = {'error': str(exc)}

    try:
        rules = x.get_indexing_rules().get('IndexingRules', [])
        out['indexing_rules'] = [
            {'name': r.get('Name'), 'rule': r.get('Rule')} for r in rules
        ]
    except Exception as exc:  # noqa: BLE001
        out['indexing_rules'] = [{'error': str(exc)}]

    try:
        recs = x.get_sampling_rules().get('SamplingRuleRecords', [])
        out['head_sampling'] = [
            {'name': r['SamplingRule'].get('RuleName'),
             'fixed_rate': r['SamplingRule'].get('FixedRate'),
             'reservoir': r['SamplingRule'].get('ReservoirSize')}
            for r in recs
        ]
    except Exception as exc:  # noqa: BLE001
        out['head_sampling'] = [{'error': str(exc)}]

    return out


def save(label, data):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, f'{label}.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return os.path.normpath(path)


def load(label):
    path = os.path.join(SNAP_DIR, f'{label}.json')
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def compare(a_label, b_label):
    a, b = load(a_label), load(b_label)
    print(f"对比  {a_label} ({a['captured_at_iso']})  →  {b_label} ({b['captured_at_iso']})")
    print()

    # ── 先判混淆变量：不成立就没有结论 ──
    print('── 前提校验（任一不成立则结论无效）──')
    ok = True
    if a.get('loadgen_state') != b.get('loadgen_state'):
        print(f"  ❌ 压测机状态变了: {a.get('loadgen_state')} → {b.get('loadgen_state')}")
        print('     负载条件不同，Services/Edges 差异归因不了 —— 结论无效')
        ok = False
    else:
        print(f"  ✅ 压测机状态一致: {a.get('loadgen_state')}")

    hs_a = {h['name']: h.get('fixed_rate') for h in a.get('head_sampling', [])}
    hs_b = {h['name']: h.get('fixed_rate') for h in b.get('head_sampling', [])}
    if hs_a != hs_b:
        print(f'  ❌ head sampling 变了: {hs_a} → {hs_b}')
        print('     采样率变化会同时改变拓扑，与 TS 的影响无法分离 —— 结论无效')
        ok = False
    else:
        print(f'  ✅ head sampling 一致: {hs_a}')

    d_a = a.get('trace_segment_destination', {}).get('Destination')
    d_b = b.get('trace_segment_destination', {}).get('Destination')
    print(f'  {"✅" if d_a != d_b else "⚠️ "} 摄入目的地: {d_a} → {d_b}'
          + ('' if d_a != d_b else '   ← 没有变化，这次对比测不到 TS 的影响'))
    if d_a == d_b:
        ok = False
    print()

    # ── 再看拓扑 ──
    verdicts = []
    for w in [f'{h}h' for h in WINDOWS_HOURS]:
        wa, wb = a['windows'].get(w, {}), b['windows'].get(w, {})
        if 'error' in wa or 'error' in wb:
            print(f'── {w} 窗口：取数失败，跳过 ──')
            continue
        sa, sb = set(wa['services']), set(wb['services'])
        ea, eb = set(wa['edges']), set(wb['edges'])
        print(f'── {w} 窗口 ──')
        print(f"  Services  {wa['service_count']:3d} → {wb['service_count']:3d}"
              f"   丢失 {len(sa - sb)}  新增 {len(sb - sa)}")
        print(f"  Edges     {wa['edge_count']:3d} → {wb['edge_count']:3d}"
              f"   丢失 {len(ea - eb)}  新增 {len(eb - ea)}")
        for name, missing in (('Services', sa - sb), ('Edges', ea - eb)):
            if missing:
                print(f'  ⚠️  {name} 丢失明细:')
                for m in sorted(missing)[:12]:
                    print(f'       - {m}')
                if len(missing) > 12:
                    print(f'       ... 另有 {len(missing) - 12} 条')
        # 判据：以「边是否丢失」为主 —— etl_xray 产出的就是边
        lost_ratio = len(ea - eb) / len(ea) if ea else 0.0
        verdicts.append((w, lost_ratio, len(ea - eb), len(ea)))
        print()

    print('── 判定 ──')
    if not ok:
        print('  INVALID —— 前提校验未通过，不要基于本次对比下结论')
        return 2
    worst = max((v[1] for v in verdicts), default=0.0)
    for w, ratio, lost, total in verdicts:
        print(f'  {w}: 边丢失 {lost}/{total} = {ratio:.1%}')
    if worst == 0:
        print('  ✅ CONFIRMED-SAFE —— 一条边都没丢，GetServiceGraph 不受 TS 影响')
        print('     → 残余风险清零，可推进 agent 观测方案')
    elif worst < 0.10:
        print(f'  ⚠️  MINOR —— 最大丢失 {worst:.1%}，疑似正常抖动而非 TS 导致')
        print('     → 建议再取一次 6 小时窗口复核，确认不是趋势')
    else:
        print(f'  ❌ DEGRADED —— 最大丢失 {worst:.1%}，TS 疑似影响服务图')
        print('     → 下一步：把索引百分比提到 100% 再测，区分「索引率导致」')
        print('       还是「摄入模式切换导致」；若仍丢失则考虑改从 aws/spans 读拓扑')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--label')
    ap.add_argument('--compare', nargs=2, metavar=('BEFORE', 'AFTER'))
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare)

    if not args.label:
        ap.error('需要 --label 或 --compare')

    data = measure()
    path = save(args.label, data)
    print(f"快照 '{args.label}' 已存 → {path}")
    print(f"  时间          : {data['captured_at_iso']}")
    print(f"  压测机        : {data.get('loadgen_state')}")
    print(f"  摄入目的地    : {data.get('trace_segment_destination', {}).get('Destination')}")
    print(f"  索引规则      : {data.get('indexing_rules')}")
    print(f"  head sampling : {data.get('head_sampling')}")
    for w, d in sorted(data['windows'].items()):
        if 'error' in d:
            print(f"  {w:>3} 窗口      : 失败 {d['error']}")
        else:
            print(f"  {w:>3} 窗口      : Services {d['service_count']}, Edges {d['edge_count']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

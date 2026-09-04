#!/usr/bin/env python3
"""迁移期间暂停 / 恢复图谱 ETL 的调度 —— **只改 State，绝不删除**。

为什么需要它：
    应用重建期间，Pod 在起停、K8sService 在换、边在闪断。5 分钟一轮的 ETL 会把
    这些**过渡态**当成真实拓扑写进图里 —— 一边拆一边记，最后要花更多力气清理
    这些噪声边，而且清理时分不清「这条边是过渡态噪声」还是「真的被删掉了」。
    正确做法是重建期间停止采集，重建完成后做一次干净的全量。

为什么是 disable 而不是删规则：
    用户明确的硬约束 —— **ETL 可改不可删**。删了规则要重建，重建就可能漏掉
    某条（本脚本实测发现调度规则不止 GOAL.md 里写的那 2 条），
    而 disable 是可逆的、且 --resume 能精确还原到暂停前的状态。

用法：
    python3 etl_schedule_pause.py --status            # 只看，不改
    python3 etl_schedule_pause.py --pause             # 暂停并把原状态存盘
    python3 etl_schedule_pause.py --resume            # 按存盘状态还原
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import boto3

REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
# 状态存盘位置。--resume 只还原**暂停前是 ENABLED** 的规则 ——
# 如果某条规则本来就是 DISABLED，恢复时把它打开就是擅自改变了配置。
STATE_FILE = pathlib.Path(__file__).resolve().parent.parent / 'todo' / 'etl_schedule_state.json'

# 认哪些 Lambda 属于图谱 ETL。用前缀而不是写死名字 ——
# 实测调度规则不止 2 条，硬编码必然漏。
LAMBDA_PREFIXES = ('neptune-etl-', 'gp-')


def _discover(events) -> list[dict]:
    """找出所有「有调度表达式」且「打到图谱 ETL Lambda」的规则。

    只收有 ScheduleExpression 的 —— 事件驱动的规则（neptune-etl-trigger-eks 等）
    不该暂停：它们由真实的基础设施变更触发，那正是重建期间**最需要**记录的信号，
    而且它们本身不会周期性写过渡态。
    """
    out = []
    paginator = events.get_paginator('list_rules')
    for page in paginator.paginate():
        for r in page.get('Rules', []):
            if not r.get('ScheduleExpression'):
                continue
            targets = events.list_targets_by_rule(Rule=r['Name']).get('Targets', [])
            fns = [t['Arn'].rsplit(':function:', 1)[-1].split(':')[0]
                   for t in targets if ':function:' in t.get('Arn', '')]
            if not any(f.startswith(LAMBDA_PREFIXES) for f in fns):
                continue
            out.append({
                'name': r['Name'],
                'state': r.get('State'),
                'schedule': r['ScheduleExpression'],
                'targets': fns,
            })
    return sorted(out, key=lambda x: x['name'])


def cmd_status(events) -> int:
    rules = _discover(events)
    print(f'图谱 ETL 的调度规则（{REGION}）：{len(rules)} 条\n')
    for r in rules:
        mark = '🟢' if r['state'] == 'ENABLED' else '⚪'
        print(f"  {mark} {r['name']:34} {r['schedule']:22} {r['state']:9} → {', '.join(r['targets'])}")
    if STATE_FILE.exists():
        saved = json.loads(STATE_FILE.read_text())
        print(f"\n  ⚠️  存在暂停存盘（{time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(saved['paused_at']))}）"
              f"，共 {len(saved['paused'])} 条待恢复：{', '.join(saved['paused'])}")
        print('     用 --resume 还原。')
    return 0


def cmd_pause(events) -> int:
    rules = _discover(events)
    enabled = [r for r in rules if r['state'] == 'ENABLED']
    if not enabled:
        print('没有处于 ENABLED 的调度规则，无需暂停。')
        return 0
    if STATE_FILE.exists():
        print(f'❌ 已存在暂停存盘 {STATE_FILE}，先 --resume 再重新暂停，'
              f'否则会覆盖掉原始状态而无法还原。')
        return 1

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        'paused_at': int(time.time()),
        'region': REGION,
        'paused': [r['name'] for r in enabled],
        'snapshot': rules,
    }, ensure_ascii=False, indent=2))

    for r in enabled:
        events.disable_rule(Name=r['name'])
        print(f"  ⏸  已禁用 {r['name']:34} ({r['schedule']})")
    print(f'\n✅ 暂停 {len(enabled)} 条，原状态已存 {STATE_FILE}')
    print('⚠️  重建完成后必须 --resume，并跑一次干净全量重建。')
    return 0


def cmd_resume(events) -> int:
    if not STATE_FILE.exists():
        print(f'❌ 找不到暂停存盘 {STATE_FILE} —— 无从判断哪些规则本来是开着的。'
              f'\n   擅自把所有规则打开会改变暂停前的配置，故拒绝。')
        return 1
    saved = json.loads(STATE_FILE.read_text())
    for name in saved['paused']:
        events.enable_rule(Name=name)
        print(f'  ▶️  已启用 {name}')
    STATE_FILE.unlink()
    print(f"\n✅ 恢复 {len(saved['paused'])} 条，存盘已清理。")
    print('⚠️  下一步：跑一次干净的全量重建，核对图谱与实际系统一致。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--status', action='store_true')
    g.add_argument('--pause', action='store_true')
    g.add_argument('--resume', action='store_true')
    a = ap.parse_args()

    events = boto3.client('events', region_name=REGION)
    if a.status:
        return cmd_status(events)
    if a.pause:
        return cmd_pause(events)
    return cmd_resume(events)


if __name__ == '__main__':
    sys.exit(main())

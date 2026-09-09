#!/usr/bin/env python3
"""撤回 2026-09-09 因假 injection_confirmed 而写错的两条判定。

## 为什么必须撤回而不是等下次覆盖

2026-09-09 跑 `petsearch -> DynamoDBTable` 与 `petsearch -> S3Bucket`
的注入实验，判定链给出 `inconclusive` + `dependency_class=soft`。
这两个结论**建立在一个假的生效性证据上**：

`xray_metrics.took_effect` 当时拿**不等长窗口的绝对计数**相减：

    基线窗 1800s: 8470 次  ->  4.706 次/秒
    注入窗  120s:  548 次  ->  4.567 次/秒   ← 速率几乎没变

「减少 7922 次」被判成「打断确认生效」，实际上**注入完全没生效** ——
`externalTargets` 只封住了 apply 时 DNS 解析出的那一个 IP，
而 DynamoDB 区域端点有多个轮换 IP；S3 更是 Gateway 端点，
靠路由表 + 前缀列表，封单个 IP 根本不覆盖。

旁证：整个注入期 `petsite -> search-service` 的响应码全是 200、
平均延迟 190-350ms、p99 恒定 ~3008ms —— **完全平坦**，
没有任何被打断的痕迹。

于是 `injection_confirmed=True` + 观测方零退化 → 判定链走到
「soft dependency（打断它本就不该影响调用方）」。而正确结论是
**什么都没验到**。

`soft` 不是中性标签：它会被 DR 影响面分析读成「这条依赖不影响可用性」，
从而在故障预案里被降级。用一个测量 bug 得出的 soft 比 untested 危险得多。

## 撤回到什么状态

清掉本次实验写入的全部 verify_* 属性，回到「未测试」——
而不是改写成别的结论。我们**不知道**这两条边是什么性质，
诚实的状态是没有结论。

## 用法

    python3 scripts/retract_false_soft_verdicts.py            # dry-run
    python3 scripts/retract_false_soft_verdicts.py --apply
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 只撤这个实验 id 前缀写出来的判定 —— 精确到本次事故，
#: 不碰任何别的会话或别的实验留下的结论。
_BAD_EXPERIMENTS = ('chaosmesh/vx-petsearch-dynamodbtable',
                    'chaosmesh/vx-petsearch-s3bucket')

#: 本次实验写入的属性。清掉它们等于回到未测试。
_VERIFY_PROPS = (
    'verify_status', 'verify_confidence', 'verify_last', 'verify_by',
    'verify_experiment', 'verify_degradation', 'verify_reason',
    'verify_confirm_count', 'verify_refute_count', 'verify_evidence_channel',
    'verify_dependency_class', 'verify_dependency_class_reason',
    'verify_observing_sources',
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    os.environ.setdefault('REGION', 'ap-northeast-1')
    from neptune import neptune_client as nc

    exp_list = ', '.join(f"'{e}'" for e in _BAD_EXPERIMENTS)
    rows = nc.results(f"""
MATCH (a)-[r]->(b)
WHERE r.verify_experiment IN [{exp_list}]
RETURN id(r) AS eid, type(r) AS edge, a.name AS src, b.name AS dst,
       r.verify_status AS status, r.verify_dependency_class AS dep_class,
       r.verify_experiment AS exp, properties(r) AS props
""")
    print(f'受影响的边: {len(rows)} 条')
    for r in rows:
        print(f"  {r['edge']} {r['src']} -> {r['dst'][:44]}")
        print(f"      status={r['status']}  dependency_class={r['dep_class']}")
        print(f"      experiment={r['exp']}")

    if not rows:
        print('\n没有需要撤回的判定（可能已被撤回或覆盖）。')
        return 0
    if not args.apply:
        print('\n（dry-run。加 --apply 执行撤回）')
        return 0

    # 留痕先于修改 —— 撤回本身也是一次改动，必须可回溯。
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
    trace = ROOT / 'todo' / f'retracted-false-soft-verdicts_{ts}.json'
    trace.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                     encoding='utf-8')
    print(f'\n留痕已写: {trace.relative_to(ROOT)}')

    n = 0
    for r in rows:
        # openCypher 的 REMOVE 一次可以列多个属性
        props = ', '.join(f'r.{p}' for p in _VERIFY_PROPS)
        nc.results(f"""
MATCH (a)-[r]->(b)
WHERE id(r) = $eid
REMOVE {props}
RETURN count(r) AS n
""", {'eid': r['eid']})
        n += 1
        print(f"  已撤回 {r['edge']} {r['src']} -> {r['dst'][:40]}")
    print(f'\n撤回 {n} 条判定，这两条边回到「未测试」状态。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

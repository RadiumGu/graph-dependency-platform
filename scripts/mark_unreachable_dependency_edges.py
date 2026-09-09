#!/usr/bin/env python3
"""给「用现有后端无法注入」的依赖边写上 verify_blocked_reason。

## 为什么需要这个脚本

选靶逻辑早就会排除结构性不可注入的边
（`chaos/code/runner/injectability.py`，由 `tests/test_47::t305_06` 钉着
「排除发生在打分之前」）。**但那份知识只存在于代码里，图谱上没有痕迹。**

于是查图的人看到的是一条没有 `verify_status` 的边 ——
与「还没轮到」完全无法区分。

实测（2026-09-09）：115 条依赖边里 10 条 confirmed、14 条 inconclusive、
91 条从未尝试。对这 91 条逐条跑 `injectability()`：

    injectable                 78 条  ← 工具是有的，纯粹没跑
    unreachable_by_any_backend 13 条  ← 用任何后端都打不到

那 13 条全部是 AgentCore 层 + 1 条 Lambda→Neptune。混在 untested 里
会让覆盖率给出**错误的努力方向** —— 看起来「再跑几轮就能覆盖」，
实际上永远轮不到。

## 为什么不改 verify_status

`statuses` 只有 untested / confirmed / refuted / inconclusive，描述的是
**验证结果**；「能不能验」是正交维度。一条边可以同时「被阻断」且「未测试」。
把它塞进 status 会让「不可验」看起来像一种验证结论，而它恰恰是「没有结论」。

## 用法

    python3 scripts/mark_unreachable_dependency_edges.py            # dry-run
    python3 scripts/mark_unreachable_dependency_edges.py --apply    # 实写

`--apply` 会把被改动的边导出成 JSON 留痕（照 `scripts/clean_*` 的先例），
方便回滚与审计。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 三个都要进 path，缺一个就 ImportError：
#   chaos/code/runner  -> injectability
#   rca                -> neptune.neptune_client
#   仓库根             -> shared（neptune_client 里 `from shared import get_region`，
#                         实测只加 rca/ 会 ModuleNotFoundError: shared）
for p in (ROOT / 'chaos' / 'code' / 'runner', ROOT / 'rca', ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

#: 依赖边类型。从契约取，不硬编码 —— 硬编码清单在本仓库漂移过
#: （rca 那份曾少 Invokes，线上漏 16 条边）。
def _dependency_edge_labels() -> list:
    import yaml
    with open(ROOT / 'profiles' / 'graph_contract.yaml', encoding='utf-8') as fh:
        gc = yaml.safe_load(fh) or {}
    return sorted(k for k, v in (gc.get('edge_types') or {}).items()
                  if isinstance(v, dict) and v.get('dependency'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='实际写入（默认只打印计划）')
    args = ap.parse_args()

    import injectability as inj

    # 脚本没有 conftest 兜底，`NEPTUNE_ENDPOINT` 未设时 requests 会报
    # `Invalid URL 'https://:8182/openCypher': No host supplied` —— 那个报错
    # 完全看不出是「环境变量没设」。照 tests/conftest.py 的做法给同一个兜底。
    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    os.environ.setdefault('REGION', 'ap-northeast-1')

    from neptune import neptune_client as nc

    labels = _dependency_edge_labels()
    rel = '|'.join(labels)

    # 只看**还没有结论**的边：已经 confirmed / refuted / inconclusive 的
    # 说明实际上验过或试过，不该被标成「打不到」。
    rows = nc.results(f"""
MATCH (a)-[r:{rel}]->(b)
WHERE r.verify_status IS NULL OR r.verify_status = 'untested'
RETURN id(r) AS eid, type(r) AS edge,
       labels(a)[0] AS src_label, a.name AS src,
       labels(b)[0] AS dst_label, b.name AS dst,
       r.verify_status AS status, r.verify_blocked_reason AS existing
""")

    planned, skipped = [], 0
    for row in rows:
        verdict, why = inj.injectability(row['src_label'], row['dst_label'])
        if verdict != inj.UNREACHABLE:
            skipped += 1
            continue
        if row.get('existing'):
            skipped += 1          # 已标注过，不重复写
            continue
        planned.append({**row, 'reason': why})

    print(f'候选（无结论的依赖边）: {len(rows)} 条')
    print(f'判定为后端不可达      : {len(planned)} 条')
    print(f'跳过（可注入或已标注）: {skipped} 条')
    print()
    for p in planned:
        print(f"  {p['edge']:<12} {p['src']} -> {p['dst']}")
        print(f"      {p['reason']}")

    if not args.apply:
        print('\n（dry-run。加 --apply 实写）')
        return 0
    if not planned:
        print('\n无需写入。')
        return 0

    # 留痕先于写入 —— 照 scripts/clean_nondependency_verify_attrs.py 的先例：
    # 先导出被改动对象再改，否则出错时无从回滚。
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
    trace = ROOT / 'todo' / f'marked-unreachable-edges_{ts}.json'
    trace.write_text(json.dumps(planned, ensure_ascii=False, indent=2),
                     encoding='utf-8')
    print(f'\n留痕已写: {trace.relative_to(ROOT)}')

    written = 0
    for p in planned:
        safe = str(p['reason']).replace("'", "\\'")
        nc.results(f"""
MATCH (a)-[r:{p['edge']}]->(b)
WHERE a.name = $src AND b.name = $dst
  AND (r.verify_status IS NULL OR r.verify_status = 'untested')
SET r.verify_blocked_reason = '{safe}'
RETURN count(r) AS n
""", {'src': p['src'], 'dst': p['dst']})
        written += 1
    print(f'已标注 {written} 条边')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

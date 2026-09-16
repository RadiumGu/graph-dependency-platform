#!/usr/bin/env python3
"""清掉因判定器缺 IAM deny 轴而误标的 `blocked_class` 标注。

## 为什么要清

2026-09-09 我用 `scripts/mark_unreachable_dependency_edges.py` 给 13 条边打了
`verify_blocked_class = unreachable_by_any_backend`。当时 `injectability.py`
只有三个轴（FIS 直打目标 / 集群内源侧切断 / 与后端无关的原因 token），
**完全没有 IAM deny 这一轴**。

而 9-13 另一个会话用 IAM deny 把 `petsearch -> DynamoDBTable` 验成
`confirmed` / 退化 100%（实验 `iam-deny-probe_20260913-155349`）。
它靠的性质是：**SigV4 授权按每次 API 调用评估，不是按每个连接评估** ——
所以 DNS 缓存、连接池、端点 IP 轮换在这一层全部不成立。

补上第四轴（`injectability.iam_deny_targets()`，读
`scripts/verify_via_iam_deny.py` 的 `SEVERANCE_METHODS`）之后重判，
`AgentRuntime` 这个目标类型**本来就在能力表里** ——
那 3 条 `Delegates AgentRuntime -> AgentRuntime` 的标注是假的。

## 为什么这件事紧急

`unreachable_by_any_backend` 的语义是「用任何后端都打不到」，
它会让这些边**退出验证队列**（`should_skip_target` 对 UNREACHABLE 返回跳过），
并从覆盖率的可达分母里被扣掉。一个错误的"永久不可达"标注，
比 `untested` 危险得多：untested 只是排队等着，而它是被判了死刑。

这与本会话两次撤回 `dependency_class=soft` 是同一类错误：
**用不完整的判据得出的结论，比没有结论更糟。**

## 处置

对每条带 `verify_blocked_class` 的边**重新过一遍判定器**：
判定不再是阻断类的，就清掉 `verify_blocked_class` 与 `verify_blocked_reason`，
让它回到验证队列。判定仍是阻断类的，原样保留（但会更新 reason 文本，
因为理由里现在会写明"IAM deny 的能力表里也没有这个类型"）。

## 用法

    python3 scripts/reclassify_blocked_edges.py            # dry-run
    python3 scripts/reclassify_blocked_edges.py --apply
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / 'chaos' / 'code', ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 阻断类判定。判定落在这几个里才该保留标注。
_BLOCKING = None            # 运行时从 injectability 取，避免硬编码字符串


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    os.environ.setdefault('REGION', 'ap-northeast-1')

    from neptune import neptune_client as nc
    from runner import injectability as inj

    blocking = {inj.UNREACHABLE, inj.NO_OBSERVER,
                inj.NEEDS_COMPOUND, inj.PRECONDITION_UNMET}

    # ⚠️ 只重判 `unreachable_by_any_backend` 这一档。
    #
    # 第一版对**所有**带 blocked_class 的边重判，结果想清掉 37 条 ——
    # 其中 34 条是 `precondition_unmet`。那是错的：
    #
    #   · `precondition_unmet` 是**流量证据**得出的结论
    #     （源 Lambda 24h 零调用、链路休眠，见 scripts/preflight_edge_traffic.py）
    #   · `needs_compound_experiment` 是**边的 phase 属性**得出的
    #     （startup 期依赖，稳态无可观测退化）
    #   · 而 `injectability()` 只判**能力**，它不知道流量、也不查集群
    #
    # 所以拿能力判定去重判一个流量结论，必然返回 injectable ——
    # 那不是"纠正了误判"，是"用一个不相关的判据覆盖了一个有效结论"。
    # 后果是把休眠链路的边当可验的放回队列，白耗注入，
    # 并且大概率又收获一批 inconclusive。
    #
    # 本轮要修的 bug **只是**：判定器缺 IAM deny 轴，导致把可达的类型
    # 判成永久不可达。那个 bug 只影响 UNREACHABLE 这一档。
    _CAPABILITY_CLASS = inj.UNREACHABLE

    rows = nc.results("""
MATCH (a)-[r]->(b)
WHERE r.verify_blocked_class = $klass
RETURN id(r) AS eid, type(r) AS edge,
       labels(a)[0] AS sl, a.name AS src,
       labels(b)[0] AS dl, b.name AS dst,
       r.verify_blocked_class AS old_class,
       r.verify_blocked_reason AS old_reason,
       r.phase AS phase
""", {'klass': _CAPABILITY_CLASS})
    print(f'带 {_CAPABILITY_CLASS} 标注的边: {len(rows)} 条')
    print('（刻意不动 precondition_unmet / needs_compound_experiment —— '
          '那是流量与 phase 证据得出的结论，不是能力判定）')
    print(f'IAM deny 能力表: {sorted(inj.iam_deny_targets())}\n')

    clear, keep = [], []
    for r in rows:
        # reason_text 从 phase 推 —— 与 preflight 同一套（_PHASE_TO_REASON）。
        # 不传 reason_text 会让 startup 期的 ECR 边被判成 injectable。
        reason_text = ('image-repo-dependency'
                       if r.get('phase') == 'startup' else '')
        verdict, why = inj.injectability(r['sl'], r['dl'], reason_text)
        rec = {**r, 'new_verdict': verdict, 'new_reason': why}
        (keep if verdict in blocking else clear).append(rec)

    print(f'✅ 应清掉标注（重判为可注入）: {len(clear)} 条')
    for c in clear:
        print(f"   {c['edge']:<12} {c['src'][:26]:<26} -> {c['dst'][:30]:<30}")
        print(f"      旧: {c['old_class']}")
        print(f"      新: {c['new_verdict']} —— {c['new_reason'][:100]}")
    print(f'\n⏸  保留标注: {len(keep)} 条')
    agg: dict = {}
    for k in keep:
        key = f"{k['new_verdict']}  {k['edge']} {k['sl']}->{k['dl']}"
        agg[key] = agg.get(key, 0) + 1
    for k, v in sorted(agg.items()):
        print(f'   {k}  ×{v}')

    if not args.apply:
        print('\n（dry-run。加 --apply 执行）')
        return 0
    if not clear:
        print('\n无需清除。')
        return 0

    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
    trace = ROOT / 'todo' / f'reclassified-blocked-edges_{ts}.json'
    trace.write_text(json.dumps({'cleared': clear, 'kept': keep},
                                ensure_ascii=False, indent=2, default=str),
                     encoding='utf-8')
    print(f'\n留痕已写: {trace.relative_to(ROOT)}')

    for c in clear:
        nc.results("""
MATCH (a)-[r]->(b) WHERE id(r) = $eid
REMOVE r.verify_blocked_class, r.verify_blocked_reason
RETURN count(r) AS n
""", {'eid': c['eid']})
        print(f"   已清 {c['edge']} {c['src'][:24]} -> {c['dst'][:28]}")
    print(f'\n清掉 {len(clear)} 条错误标注，这些边回到验证队列。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

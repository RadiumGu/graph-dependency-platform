#!/usr/bin/env python3
"""韧性评分卡 —— 形态抄工作坊，**判据用我们的门禁**。

## 抄什么、不抄什么

AWS 工作坊 Module 4 的评分卡：DynamoDB 存演练结果 + Python 出报告 +
`put-metric-data` 发到 `ClosedLoop/Resilience`（`DrillRecoveryRate`、MTTR）。
它量的是**每阶段耗时 + 是否恢复**。

形态可以抄。**判据不行。**

## 为什么判据不行

「注入了 → 告警响了 → 处置跑了 → 打勾」这套判据的前提是
**流程跑通就等于证明了什么**。这个前提在本项目已经被证伪两次，
两次都往图谱写进了错误结论（均已撤回，留痕在
`todo/retracted-false-soft-verdicts_20260909-*.json`）：

1. `xray_metrics.took_effect` 拿**不等长窗口**比绝对计数
   （基线 1800s / 注入 120s），`8470-548>0` 判「注入生效」——
   而速率是 4.71 → 4.57 次/秒，**根本没变**。注入完全没打断。
2. 复合实验删了 Pod，观测方吞吐必然塌陷，拿它当「依赖被切断」的证据 ——
   而删 Pod 本身就会让上游调用塌陷，这个信号**分不清**
   「连不上目标」与「Pod 在重启」。

两次都产出 `dependency_class=soft`。而 `soft` 会被 DR 影响面分析读成
「这条依赖不影响可用性」并在故障预案里降级。
**用测量缺陷得出的 soft 比 untested 危险得多** ——
untested 只是没有信息，soft 是错误信息，而且带着置信度数字。

一份不校验注入生效性的评分卡，就是这类假证据的**批量**版本。

## 我们的判据：三道门禁

一条演练记录要计入恢复率，必须同时满足：

1. **注入确实生效** —— `injection_confirmed is True`，且判据必须是
   **速率级**而不是计数级（`tests/test_70` 钉着这一点）
2. **基线窗与注入窗等长** —— 否则计数不可比
   （`scripts/verify_external_target_edges.py` 里同一个坑踩过第二次）
3. **观测方信号未被混淆** —— 用了复合手法（删 Pod / 重启）时，
   观测方吞吐失去判别力，该条只能记观察不能记结论

不满足的记为 `inconclusive`，**既不计入分子也不计入分母** ——
与 `verified_ratio_addressable_pct` 把永久工具天花板从分母里扣掉是同一个道理：
分母里掺进不可能项或不可信项，得出的比率会给出错误的努力方向。

## 用法

    python3 scripts/emit_resilience_scorecard.py --dry-run
    python3 scripts/emit_resilience_scorecard.py            # 发 CloudWatch
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / 'chaos' / 'code', ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

NAMESPACE = 'GraphDependency/Resilience'

#: 演练记录的来源：我们自己的实验留痕，不另建 DynamoDB 表。
#:
#: 工作坊用 DynamoDB 是因为它的闭环由 EventBridge 串联、各阶段分别写表。
#: 我们的实验是单进程跑完并落 JSON 留痕，再引入一张表只会多一处
#: 需要同步的真相来源 —— 本仓库已经因为「同类清单各处一份」踩过坑。
_DRILL_GLOBS = (
    'todo/chaos-external-edge-run_*.json',
)


def _load_drills() -> list[dict]:
    out = []
    for pat in _DRILL_GLOBS:
        for f in sorted(glob.glob(str(ROOT / pat))):
            try:
                data = json.loads(pathlib.Path(f).read_text(encoding='utf-8'))
            except Exception:                                  # noqa: BLE001
                continue
            for rec in (data if isinstance(data, list) else [data]):
                if isinstance(rec, dict):
                    out.append({**rec, '_source': os.path.basename(f)})
    return out


def classify_drill(rec: dict) -> tuple[str, str]:
    """一条演练记录的评分卡归类。返回 (类别, 理由)。

    类别:
        valid_recovered    注入生效、信号可信、系统恢复 —— 计入分子与分母
        valid_not_recovered 注入生效、信号可信、未恢复 —— 只计入分母
        inconclusive       证据不足 —— **两边都不计**
    """
    # ── 门禁一：注入是否确实生效 ──────────────────────────────────
    eff = rec.get('injection_confirmed')
    if eff is not True:
        why = rec.get('injection_confirmed_why') or rec.get('reason') or ''
        return 'inconclusive', (
            f'注入生效性 = {eff!r}（{why[:120]}）—— '
            f'没有打断就没有验证。这类记录进分母会把「没做成的实验」'
            f'算成「系统没恢复」，两者的处置动作完全不同。')

    # ── 门禁二：观测方信号是否被混淆 ──────────────────────────────
    # 复合模式删过 Pod，观测方吞吐必然塌陷 —— 该信号没有判别力。
    if rec.get('pods_deleted') or rec.get('verdict') == 'observation_only':
        return 'inconclusive', (
            '复合手法删过 Pod：观测方吞吐必然塌陷，'
            '分不清「连不上目标」与「Pod 在重启」。'
            '需要对照臂（同样删 Pod 但不切断）才能判定。')

    # ── 门禁三：基线窗与注入窗是否等长 ────────────────────────────
    w = rec.get('observer_window_seconds')
    if w is None:
        return 'inconclusive', (
            '记录没有 observer_window_seconds —— '
            '无法确认基线窗与注入窗等长。不等长时计数不可比，'
            '这个坑本项目踩过两次。')

    # ── 恢复判定 ─────────────────────────────────────────────────
    # 用既有的 verdict：confirmed 表示干预确实传导到了观测方，
    # 也就是说这条依赖是真的、且系统**没有**吸收掉故障。
    verdict = str(rec.get('verdict') or '')
    deg = rec.get('observer_degradation_pct')
    if verdict == 'confirmed':
        return 'valid_not_recovered', (
            f'注入生效且观测方退化 {deg}% —— 故障传导到了调用方，'
            f'说明这条路径上没有有效的降级保护')
    if verdict in ('inconclusive', ''):
        return 'inconclusive', f'判定为 {verdict or "无"}，不计入'
    return 'valid_recovered', (
        f'注入生效（已确认打断）但观测方退化 {deg}% —— '
        f'调用方吸收了故障')


def scorecard() -> dict:
    drills = _load_drills()
    buckets: dict[str, list] = {'valid_recovered': [],
                                'valid_not_recovered': [],
                                'inconclusive': []}
    for rec in drills:
        cat, why = classify_drill(rec)
        buckets[cat].append({'edge': rec.get('edge'), 'why': why,
                             'source': rec.get('_source')})

    scored = len(buckets['valid_recovered']) + len(buckets['valid_not_recovered'])
    return {
        'drills_total': len(drills),
        'drills_scored': scored,
        'drills_inconclusive': len(buckets['inconclusive']),
        # 分母刻意只用**证据可信**的那些。掺进 inconclusive 会让
        # 「没做成的实验」看起来像「系统没恢复」，而两者的处置完全不同：
        # 前者要修实验方法，后者要修系统。
        'recovery_rate_pct': (
            round(len(buckets['valid_recovered']) / scored * 100, 2)
            if scored else None),
        # 这个数掉下去比恢复率掉下去更值得警觉：它意味着
        # 我们的实验方法本身在退化，产出的都是不可用的证据。
        'evidence_quality_pct': (
            round(scored / len(drills) * 100, 2) if drills else None),
        'buckets': buckets,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sc = scorecard()
    print('=== 韧性评分卡（判据：我们的注入生效性门禁）===')
    for k in ('drills_total', 'drills_scored', 'drills_inconclusive',
              'recovery_rate_pct', 'evidence_quality_pct'):
        print(f'  {k:<24} {sc[k]}')
    print()
    for cat, items in sc['buckets'].items():
        if not items:
            continue
        print(f'── {cat}（{len(items)} 条）──')
        for it in items:
            edge = str(it.get('edge'))[:64]
            print(f'   {edge}')
            print(f"      {it['why'][:150]}")
    print()

    if sc['recovery_rate_pct'] is None:
        print('⚠️  没有任何证据可信的演练记录 —— 恢复率无从计算。')
        print('   这不是「恢复率 0%」：0% 表示试了都没恢复，')
        print('   而现在是**一次都没有拿到可信证据**。两者的下一步完全不同。')

    data = [
        {'MetricName': 'DrillsScored', 'Value': sc['drills_scored'],
         'Unit': 'Count'},
        {'MetricName': 'DrillsInconclusive', 'Value': sc['drills_inconclusive'],
         'Unit': 'Count'},
    ]
    if sc['evidence_quality_pct'] is not None:
        data.append({'MetricName': 'EvidenceQualityPct',
                     'Value': sc['evidence_quality_pct'], 'Unit': 'Percent'})
    if sc['recovery_rate_pct'] is not None:
        data.append({'MetricName': 'DrillRecoveryRate',
                     'Value': sc['recovery_rate_pct'], 'Unit': 'Percent'})

    if args.dry_run:
        print(f'[dry-run] 本会发 {len(data)} 个指标到 {NAMESPACE}')
        for d in data:
            print(f"   {d['MetricName']} = {d['Value']} {d['Unit']}")
        return 0

    import boto3
    cw = boto3.client('cloudwatch',
                      region_name=os.environ.get('REGION', 'ap-northeast-1'))
    cw.put_metric_data(Namespace=NAMESPACE, MetricData=data)
    print(f'已发 {len(data)} 个指标到 {NAMESPACE}')

    out = ROOT / 'todo' / (
        'resilience-scorecard_'
        + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
        + '.json')
    out.write_text(json.dumps(sc, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    print(f'留痕: {out.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

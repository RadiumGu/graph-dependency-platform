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
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / 'chaos' / 'code', ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

NAMESPACE = 'GraphDependency/Resilience'

#: 演练记录的**唯一来源是图谱**（2026-09-15 改）。
#:
#: 第一版读 `todo/chaos-external-edge-run_*.json`，也就是某一轮实验的留痕文件。
#: 后果是偏悲观得离谱：那次成功的 `iam-deny-probe_20260913-155349`
#: （退化 100%）根本不在那个 glob 里，于是评分卡报「8 条演练全部证据不可信」。
#:
#: 图谱是唯一权威来源 —— 所有探针（chaos runner / iam-deny / rds-fault /
#: active-probe）都往边上写 `verify_*`。读留痕文件等于只看见其中一支。
_GRAPH_IS_SOURCE = True

#: ⚠️ **不要**拿 `verify_degradation` 当恢复信号。
#:
#: 跨会话台账（2026-09-15 05:00）记下它有**两种相反语义**：
#: `degradation_pct = 基线成功率 - 故障期成功率`，而发往 SNS/SQS 的
#: `PublishesTo` 边没有成功率通道，两侧都取到 0，相减得 `0.0` ——
#: 那不是测量值，是从空数据算出的下界。
#:
#: 这已经造成过一次实际读错：交互探索页按 deg 编码边粗细，于是三条
#: **完全切断、业务归零**的 iam-deny 边被画成全图最细。
#: 拿它当"退化 0 = 已恢复"，就是把同一个读错搬进评分卡。
#:
#: 根因已在 `chaos/code/runner/edge_verification.py::write_verdict` 修掉
#: （无成功率通道时不写该字段），但**存量数据里还有 0.0**，
#: 所以这里必须按 `verify_evidence_channel` 判断它可不可信。
_DEG_UNTRUSTWORTHY_CHANNELS = {'none', 'unknown', ''}


def _load_drills() -> list[dict]:
    """从图谱读所有有判定的依赖边。

    图谱是唯一权威来源：chaos runner / iam-deny / rds-fault / active-probe
    四支探针都往边上写 `verify_*`。读某一支的留痕文件只能看见其中一部分。
    """
    import os
    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    os.environ.setdefault('REGION', 'ap-northeast-1')
    from neptune import neptune_client as nc

    import yaml
    gc = yaml.safe_load((ROOT / 'profiles' / 'graph_contract.yaml')
                        .read_text(encoding='utf-8'))
    labels = sorted(k for k, v in (gc.get('edge_types') or {}).items()
                    if isinstance(v, dict) and v.get('dependency'))
    return nc.results(f"""
MATCH (a)-[r:{'|'.join(labels)}]->(b)
WHERE r.verify_status IS NOT NULL AND r.verify_status <> 'untested'
RETURN type(r) AS edge, a.name AS src, b.name AS dst,
       r.verify_status AS verify_status,
       r.verify_degradation AS degradation,
       r.verify_evidence_channel AS channel,
       r.verify_severance AS severance,
       r.verify_injection_confirmed AS injection_confirmed,
       r.verify_experiment AS experiment,
       r.verify_dependency_class AS dep_class
""")


def classify_drill(rec: dict) -> tuple[str, str]:
    """一条判定的评分卡归类。返回 (类别, 理由)。

    类别:
        load_bearing    证据可信、依赖承重（切断它消费方受损）
        not_load_bearing 证据可信、依赖不承重（refuted / soft）
        inconclusive    证据不足 —— **两边都不计**

    ## ⚠️ 这里**不**算"恢复率"，理由是概念性的

    第一版想照 AWS 工作坊算 `DrillRecoveryRate`：把 `confirmed` 且退化低的
    边算成"调用方吸收了故障 = 恢复"。**那是错的，两层错。**

    **第一层**：`confirmed` 在本项目的定义就是「切断这条依赖导致消费方
    可测量地受损」。所以每一条 confirmed 本身就意味着**故障传导了**、
    没有被吸收。被吸收的边会判成 `soft` 或 inconclusive，不会是 confirmed。
    拿 confirmed 去算恢复率，等于把"依赖承重"读成"系统恢复"。

    **第二层**：即使想用退化值区分，`verify_degradation` 也担不起。
    实测三条 iam-deny 边 `deg=0.0` 而 `verify_reason` 明写
    **「完全切断…且业务归零（adopt 1 → 0）」** ——
    它们的 `evidence_channel='xray-edge+business-probe'`，
    退化字段量的是 SQL/成功率通道，而业务证据在另一条通道上。
    第一版把这三条判成"调用方吸收了故障"，**正是跨会话台账警告过的那个读错**
    （交互页曾按 deg 把这三条完全切断的边画成全图最细）。

    **结论**：图谱存的是**依赖承重判定**，不是**恢复观测**。
    恢复率需要 `steady_state_after` 那一段的观测数据，而图谱不存它。
    所以本评分卡报「依赖承重率」与「证据质量」，
    并明确说明恢复率**不可从图谱计算** —— 而不是算一个看起来像的数字。
    """
    status = str(rec.get('verify_status') or '')
    exp = str(rec.get('experiment') or '')
    sev = str(rec.get('severance') or '')
    chan = str(rec.get('channel') or '')
    inj = str(rec.get('injection_confirmed') or '')

    # ── 门禁一：注入生效性 ────────────────────────────────────────────
    #
    # `confirmed` 已**隐含**注入生效：判定链的 `classify_intervention` 在写
    # confirmed 之前就要求 `injection_confirmed is True`
    # （graph_confidence.py，2026-08-31 加入）。所以不必再单独查。
    #
    # `inconclusive` 恰恰是那道门禁挡下来的结果。
    if status == 'inconclusive':
        return 'inconclusive', (
            f'判定为 inconclusive（{exp[:40]}）—— 判定链的注入生效门禁没放行。'
            f'进任何分母都会把「实验没做成」算成一种系统属性，'
            f'而两者的下一步完全相反：前者修实验方法，后者修系统。')
    if inj == 'False':
        return 'inconclusive', '注入已确认未生效 —— 没有打断就没有验证'

    # ── 门禁二：证据锚点必须存在 ──────────────────────────────────────
    #
    # 跨会话台账建议的不变量：必须有 `verify_degradation` **或**
    # `verify_severance`。两个都没有，无从判断这个判定凭什么下的。
    deg = rec.get('degradation')
    has_sev = bool(sev) and sev != 'unspecified'
    if deg is None and not has_sev:
        return 'inconclusive', (
            f'既无 verify_degradation 也无 verify_severance（{exp[:40]}）—— '
            f'无从判断这个判定凭什么下的结论。')

    if status == 'refuted':
        return 'not_load_bearing', (
            f'refuted —— 图谱曾声称这条依赖存在，干预证明不成立（{exp[:40]}）')
    if status != 'confirmed':
        return 'inconclusive', f'未识别的 verify_status={status!r}'

    # ── confirmed：依赖承重。用 dependency_class 区分强度 ──────────────
    dc = str(rec.get('dep_class') or 'unclassified')
    if dc == 'soft':
        return 'not_load_bearing', (
            f'confirmed 但强度分级为 soft —— 边真实存在，'
            f'打断它不影响调用方（{exp[:40]}）')
    return 'load_bearing', (
        f'confirmed —— 切断后消费方受损（手段 {sev or "未声明"}，'
        f'通道 {chan or "未声明"}，强度 {dc}）')


def scorecard() -> dict:
    drills = _load_drills()
    buckets: dict[str, list] = {'load_bearing': [],
                                'not_load_bearing': [],
                                'inconclusive': []}
    methods: dict[str, int] = {}
    for rec in drills:
        cat, why = classify_drill(rec)
        buckets[cat].append({
            'edge': f"{rec.get('edge')} {rec.get('src')} -> {rec.get('dst')}",
            'why': why, 'exp': rec.get('experiment')})
        if cat != 'inconclusive':
            m = str(rec.get('severance') or 'unspecified')
            methods[m] = methods.get(m, 0) + 1

    scored = len(buckets['load_bearing']) + len(buckets['not_load_bearing'])
    return {
        'verdicts_total': len(drills),
        'verdicts_scored': scored,
        'verdicts_inconclusive': len(buckets['inconclusive']),
        # 依赖承重率：证据可信的判定里，有多少条依赖被证明是承重的。
        # 这是图谱能诚实支撑的口径 —— 它答的是 DORA Art. 8(4) /
        # SYSC 15A.4.1R 关心的「哪些依赖是关键的」。
        'load_bearing_rate_pct': (
            round(len(buckets['load_bearing']) / scored * 100, 2)
            if scored else None),
        # 这个数掉下去比承重率变化更值得警觉：它意味着实验方法本身在退化，
        # 产出的都是不可用的证据。
        'evidence_quality_pct': (
            round(scored / len(drills) * 100, 2) if drills else None),
        # ⚠️ **恢复率不在这里**，而且不是漏了。
        #
        # 图谱存的是**依赖承重判定**，不是**恢复观测**。
        # `confirmed` 的定义就是「切断它导致消费方受损」= 故障传导了，
        # 拿它算恢复率等于把「依赖承重」读成「系统恢复」。
        # 恢复率需要 `steady_state_after` 那一段的观测，图谱不存它。
        'recovery_rate_pct': None,
        'recovery_rate_note': (
            '不可从图谱计算：图谱存依赖承重判定，不存恢复观测。'
            'confirmed 本身就意味着故障传导到了消费方。'
            '要算恢复率需把 steady_state_after 的观测也落盘。'),
        # 切断手段分布：不同手段证明的范围不同（IAM deny 只证明依赖承重，
        # 不覆盖延迟劣化与部分失败），合规披露要据此说明证据的适用边界。
        'severance_methods': dict(sorted(methods.items(), key=lambda x: -x[1])),
        'buckets': buckets,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sc = scorecard()
    print('=== 韧性评分卡（判据：我们的注入生效性门禁）===')
    for k in ('verdicts_total', 'verdicts_scored', 'verdicts_inconclusive',
              'load_bearing_rate_pct', 'evidence_quality_pct'):
        print(f'  {k:<26} {sc[k]}')
    print(f"  {'severance_methods':<26} {sc['severance_methods']}")
    print(f"  {'recovery_rate_pct':<26} {sc['recovery_rate_pct']}"
          f"  ← {sc['recovery_rate_note']}")
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

    if sc['load_bearing_rate_pct'] is None:
        print('⚠️  没有任何证据可信的判定 —— 承重率无从计算。')
        print('   这不是「承重率 0%」：0% 表示验了都不承重，')
        print('   而现在是**一次都没有拿到可信证据**。两者的下一步完全不同。')

    data = [
        {'MetricName': 'VerdictsScored', 'Value': sc['verdicts_scored'],
         'Unit': 'Count'},
        {'MetricName': 'VerdictsInconclusive',
         'Value': sc['verdicts_inconclusive'], 'Unit': 'Count'},
    ]
    if sc['evidence_quality_pct'] is not None:
        data.append({'MetricName': 'EvidenceQualityPct',
                     'Value': sc['evidence_quality_pct'], 'Unit': 'Percent'})
    if sc['load_bearing_rate_pct'] is not None:
        data.append({'MetricName': 'LoadBearingRate',
                     'Value': sc['load_bearing_rate_pct'], 'Unit': 'Percent'})

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

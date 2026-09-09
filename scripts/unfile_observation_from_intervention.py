#!/usr/bin/env python3
"""把「被动观测」从干预槽里撤出来 —— 它们从未做过故障注入。

## 缺陷

3 条 `Delegates` / `Retrieves` 边带着 `verify_status=confirmed`、
`verify_degradation=100.0`、`verify_confirm_count=1`、`verify_by=chaos-runner`：

    WaggleAIOrchestrator -[Delegates]-> WaggleAIAdoption
    WaggleAIOrchestrator -[Delegates]-> WaggleAINutrition
    WaggleAINutrition    -[Retrieves]-> waggle-ai-nutrition-kb

但它们的 `verify_experiment` 自述的是**观测**，不是干预：

    agent-invoke:orchestrator log adoption x19 in one request
    agent-invoke:orchestrator log nutrition_advisor x5 in one request
    agent-invoke:bedrock KB Invocations=7 during probe window

「日志里数到 19 次调用」「探测窗口内 Bedrock KB Invocations=7」都是被动计数。
**没有任何东西被打断，也就没有任何退化被测量。**

## 两处后果

**① 置信度虚高。** 契约里 `intervention_confirmed = 4.0`，
`observed_per_source = 0.5` —— 对数几率上差 **8 倍**。
实测置信度 0.9890，按纯观测重算是 0.6225。

**② `verify_degradation = 100.0` 是编造的数字。** 这是本项目要抓的那一类：
一个 agent 读 `q22_edge_verification_verdicts` 会看到「退化 100%」，
于是报告「故障注入以 100% 退化确认了这条依赖」。那是假的 ——
什么都没注入。`mcp/provenance.py` 的第一条规则正是
「不要编造本 server 没有返回的数值」，而这里是图**自己**在提供编造的数值。

## 为什么改成 untested 而不是别的

`verify_status` 是**干预层**的判定轴（在依赖目标端注入、观测源端是否退化）。
这三条边从未被注入，所以干预轴上诚实的值就是 `untested` ——
和另外 88 条一样。

观测证据**不会丢**：它在边的 `calls` 属性上（deepflow 标记），
`evidence_from_props` 照样把它算成 1 个观测源，
`q20_dependency_verification`（观测层查询）本来就是它该出现的地方。

## 为什么不新造一个 verify_method 属性

契约里 `verify_*` 只允许 6 个属性（status / confidence / last / by /
experiment / degradation），加第七个要改 `profiles/graph_contract.yaml`
并同步 ETL 与门禁 —— 为了记一句「这条判定是怎么来的」去动契约，
爆炸半径不成比例。判定方法的分类记在
`todo/injection-found-defects_20260831-1705.md` 与本文件里。

## 来源

跨会话笔记 `todo/CROSS-SESSION-NOTE_20260905-0630.md` 里另一个会话
独立发现了同一处，并明确写「这是语义判断，留给你定」。这就是那个决定。

用法::

    NEPTUNE_ENDPOINT=... REGION=... \\
        python3.11 scripts/unfile_observation_from_intervention.py --dry-run
    # 核对无误后去掉 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / 'rca'),
           str(_ROOT / 'infra' / 'lambda' / 'shared' / 'python'),
           str(_ROOT / 'chaos' / 'code')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 只处理这一类：verify_experiment 自述是观测的。
#: 刻意**不**用 `verify_by=chaos-runner` 当判据 —— 这三条边上它也是
#: chaos-runner，而那正是错的地方（chaos runner 没产生它们）。
#: 判据必须对准现象本身：experiment 字段自述的取证方法。
MARKER = 'agent-invoke'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    from graph_confidence import confidence                      # noqa: E402
    from neptune import neptune_client as nc                      # noqa: E402
    from runner.edge_verification import evidence_from_props      # noqa: E402

    rows = nc.results(
        "MATCH (s)-[r]->(t) "
        f"WHERE r.verify_experiment STARTS WITH '{MARKER}' "
        "RETURN s.name AS sn, t.name AS tn, type(r) AS et, "
        "id(r) AS eid, properties(r) AS p")
    if not rows:
        print('  没有匹配的边 —— 可能已经修过了')
        return 0

    print(f'  匹配 {len(rows)} 条边\n')
    plan = []
    for r in rows:
        p = r['p'] or {}
        st, obs, _cc, rc = evidence_from_props(p)
        new_conf = confidence(static_sources=st, observing_sources=obs,
                              interventions_confirmed=0, interventions_refuted=rc)
        plan.append({
            'eid': r['eid'], 'sn': r['sn'], 'tn': r['tn'], 'et': r['et'],
            'before': {k: p.get(k) for k in (
                'verify_status', 'verify_confidence', 'verify_degradation',
                'verify_experiment', 'verify_by', 'verify_confirm_count')},
            'after': {'verify_status': 'untested',
                      'verify_confidence': new_conf,
                      'verify_degradation': '(删除)',
                      'verify_experiment': '(删除)',
                      'verify_confirm_count': 0},
            'observation_kept': {'calls': p.get('calls'),
                                 'observing_sources': obs},
        })
        print(f"  {r['sn']} -[{r['et']}]-> {r['tn']}")
        print(f"     verify_status      confirmed → untested")
        print(f"     verify_confidence  {p.get('verify_confidence')} → {new_conf}")
        print(f"     verify_degradation {p.get('verify_degradation')} → 删除（没有注入，就没有退化）")
        print(f"     verify_experiment  删除（自述是观测，不是实验）")
        print(f"     观测证据保留        calls={p.get('calls')}，仍计为 {obs} 个观测源")
        print()

    out = Path('/home/ec2-user/.kiro/crew/scratch/unfile_observation_plan.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str),
                   encoding='utf-8')
    print(f'  计划已写入 {out}')

    if args.dry_run:
        print('  [dry-run] 未写入图谱')
        return 0

    for item in plan:
        # 逐条按 id 更新，不用 name 匹配 —— 同名节点会误伤。
        # verify_by 保留 chaos-runner：契约的 authority 白名单只允许它，
        # 而这次修正本身就是混沌验证链路的一部分（撤销一条它写错的判定）。
        nc.query(
            "MATCH ()-[r]->() WHERE id(r) = $eid "
            "SET r.verify_status = 'untested', "
            "    r.verify_confidence = $conf, "
            "    r.verify_confirm_count = 0 "
            "REMOVE r.verify_degradation, r.verify_experiment",
            {'eid': item['eid'], 'conf': item['after']['verify_confidence']})
        print(f"  ✅ 已修正 {item['sn']} → {item['tn']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

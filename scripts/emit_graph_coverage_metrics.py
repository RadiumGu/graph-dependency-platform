#!/usr/bin/env python3
"""把「依赖图验证覆盖率」与「agent skill 守卫」发成 CloudWatch 指标。

## 为什么需要这个

2026-09-09：本会话里依赖边总数被观察到 **119 → 113 → 97 → 115**，
验证覆盖率 **10.92% → 11.5% → 13.4%**。方向是对的（并发会话在清假边），
但事后无法回答「什么时候变的、变了多少」——

  · `q19_topology_changes` 只记录 **deepflow 观测到的**依赖出现/消失
    （`active` true→false 的状态转变）。实测最近 24h **0 条事件**，
    而边数在同一天里从 97 变到 115 —— 它按构造看不见 aws-etl / cfn-etl /
    清理作业增删的边，而那正是这几天实际发生的事。
  · 图里**没有任何聚合量的历史**：`GraphSnapshot` / `GraphStats` 等
    节点类型实查都是 0 个。

## 为什么发 CloudWatch，而不是往图里写快照节点

依赖图应该建模**被观测的系统**，不该建模**它自己的指标**。往图里塞
`:CoverageSnapshot` 会让「节点类型数」这类契约数字把运维遥测也算进去，
而那些数字正是首页用来和契约对账的。CloudWatch 是时序数据的正确归属，
而且免费给了留存、告警与看板 —— 本项目已经重度使用它。

## agent skill 守卫为什么用哈希而不是重跑采样

`mcp/agent_skill/dependency-verification-graph.md` 注册进 agent space 之后，
自发查询率从 25% 升到 100%（20 次真实调用，见
`demo/fixtures/agent_unaided_answer.json`）。

要监测这个成果是否退化，直觉做法是定期重跑那 8 次采样。但一次采样
**45~60 秒、烧 Bedrock token**，而且 skill 生效时结果恒为 100% —— 信噪比极低。

最可能的回归是**有人删了或改了那个 skill**。所以日常防线是：
资产还在吗 / 内容哈希变了吗 / 状态还是 ACTIVE 吗 —— 三次只读 API 调用，
零模型成本。哈希变了再去跑全量采样，那时候它才有信息量。

用法::

    NEPTUNE_ENDPOINT=... REGION=ap-northeast-1 \\
        python3.11 scripts/emit_graph_coverage_metrics.py [--dry-run]

`--dry-run` 只打印不发指标 —— cron 上线前先用它确认数字对得上。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 路径顺序照 tests/conftest.py 的约定：`rca` 里的 neptune_client 会
# `from shared import get_region`，而 `shared/` 在仓库根 —— 只加 rca 会
# 报 ModuleNotFoundError: No module named 'shared'。
for _p in (str(_ROOT), str(_ROOT / 'rca')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

NAMESPACE = 'GraphDependency/Coverage'
SKILL_FILE = _ROOT / 'mcp' / 'agent_skill' / 'dependency-verification-graph.md'
AGENT_SPACE = '60c2f48f-b6e3-4dce-a0a3-4144228b2051'
SKILL_NAME = 'dependency-verification-graph'

#: 与 chaos/code/runner/edge_verification.py 的 _OBSERVER_MARKERS 同源。
#: 少一个标记就会把有观测的边算成可判伪，覆盖数字会偏大。
#: 门禁 tests/test_65_coverage_metrics.py::m02 从上游解析后逐个比对。
_OBSERVER_PROPS = (
    'xray_call_count', 'xray_last_seen',
    'nfm_flow_count', 'nfm_last_seen',
    'calls', 'error_rate',
    'image_ref',
)


def graph_coverage() -> dict:
    """算出这一刻的验证覆盖画像。全部现查，不读快照。"""
    from neptune import neptune_client as nc                    # noqa: E402
    from neptune.neptune_queries import _dependency_edge_labels  # noqa: E402

    labels = ', '.join(f"'{x}'" for x in _dependency_edge_labels())
    no_obs = ' AND '.join(f'r.{p} IS NULL' for p in _OBSERVER_PROPS)

    # 四类互斥分桶。注意 blocked 必须**优先于** untested 判：
    # 被阻断的边本来就没有 verify_status，用 coalesce 会把它们算成 untested，
    # 于是「再跑几轮就能覆盖」看起来成立，实际上永远轮不到。
    # （2026-09-09 实测：13 条 AgentCore 边就是这样藏在 untested 里的。）
    rows = nc.results(
        f"MATCH ()-[r]->() WHERE type(r) IN [{labels}] "
        "RETURN CASE "
        "  WHEN r.verify_status IN ['confirmed','refuted','inconclusive'] "
        "    THEN r.verify_status "
        "  WHEN r.verify_blocked_reason IS NOT NULL THEN 'blocked_by_backend' "
        "  ELSE 'untested' END AS vs, count(*) AS c")
    by_status = {r['vs']: int(r['c']) for r in rows}
    total = sum(by_status.values())

    gap = nc.results(
        f"MATCH ()-[r]->() WHERE type(r) IN [{labels}] AND {no_obs} "
        "RETURN coalesce(r.verify_status,'untested') AS vs, count(*) AS c")
    refutable = {r['vs']: int(r['c']) for r in gap}
    refutable_total = sum(refutable.values())

    targets = nc.results(
        f"MATCH ()-[r]->(t) WHERE type(r) IN [{labels}] AND {no_obs} "
        "AND coalesce(r.verify_status,'untested')='untested' "
        "RETURN count(DISTINCT t.name) AS c")
    decided = by_status.get('confirmed', 0) + by_status.get('refuted', 0)

    blocked = by_status.get('blocked_by_backend', 0)

    # 阻断桶必须按类别展开 —— 两类性质完全不同，混着报会再犯一次
    # 「错误的努力方向」的错：
    #   unreachable_by_any_backend  永久工具天花板，不该进待办
    #   precondition_unmet          环境前提（如源 Lambda 24h 零调用），
    #                               该进待办，但待办事项是**造流量**不是跑注入
    # 2026-09-09 实测：13 条属前者，26 条属后者。
    kls = nc.results(
        f"MATCH ()-[r]->() WHERE type(r) IN [{labels}] "
        "AND r.verify_blocked_class IS NOT NULL "
        "RETURN r.verify_blocked_class AS k, count(*) AS c")
    by_class = {r['k']: int(r['c']) for r in kls}

    # 「用现有后端**可能**验到的边」= 总数 - 被阻断。
    # 这是进度该对齐的分母：拿含不可能项的分母算比率，会把一个
    # 永久的工具天花板混进「还没做完」里，读数的人得不到正确的努力方向。
    #
    # 注意 precondition_unmet **也**从分母里扣掉：零流量链路上无从打断，
    # 它现在确实验不了。但它与 unreachable 的区别在于**会变** ——
    # 造出流量后这条边会自动回到分母里，所以这个分母是浮动的，
    # 这正是要把两类分开报的原因。
    addressable = total - blocked

    return {
        'dependency_edges_total': total,
        'confirmed': by_status.get('confirmed', 0),
        'refuted': by_status.get('refuted', 0),
        'inconclusive': by_status.get('inconclusive', 0),
        'untested': by_status.get('untested', 0),
        # 2026-09-09 新增：用**任何**后端都打不到的边（AgentCore 托管运行时、
        # Neptune 等）。与 untested 的区别是能力维度而非进度维度 ——
        # untested 会随战役推进而下降，这个数只会因**获得新注入能力**而下降。
        'blocked_by_backend': blocked,
        # 永久工具天花板：只会因**获得新注入能力**而下降。
        # 这个数不该被读成待办 —— 再跑一万轮注入也不会动。
        'blocked_unreachable': by_class.get('unreachable_by_any_backend', 0),
        # 环境前提不满足（源 Lambda 零调用等）：该进待办，
        # 但待办事项是**造流量**，不是跑注入。有流量后会自动回到分母。
        'blocked_precondition': by_class.get('precondition_unmet', 0),
        'blocked_needs_compound': by_class.get('needs_compound_experiment', 0),
        'blocked_no_observer': by_class.get('no_observer', 0),
        'addressable_edges': addressable,
        # 真正做过主动干预并得出结论的比例。业界这个数字无从计算 ——
        # 没有持久化的边实体，也没有故障注入后端。
        #
        # ⚠️ 分母刻意仍是 total（含被阻断的边），**不要改**：
        # CloudWatch 是时序数据，悄悄换分母会让改动前后的历史数据不可比，
        # 而这条曲线正是用来看战役进度的。要按可达分母看就用下面那条新指标。
        'verified_ratio_pct': round(decided / total * 100, 2) if total else 0.0,
        # 同一个分子、换成可达分母。两条并排看才有信息量：
        # 二者的差就是**永久工具天花板**贡献的那部分，
        # 它不会因为多跑几轮注入而缩小。
        'verified_ratio_addressable_pct': (
            round(decided / addressable * 100, 2) if addressable else 0.0),
        # 判伪通道对多少条边是可达的（零独立观测源）。这个数掉下去
        # 比 refuted 恒为 0 更值得警觉：它意味着「能被推翻的边」在变少。
        'refutable_edges': refutable_total,
        'refutable_untested': refutable.get('untested', 0),
        'refutable_targets': int(targets[0]['c']) if targets else 0,
    }


def skill_guard() -> dict:
    """agent skill 资产的存在性与内容一致性。三次只读调用，零模型成本。"""
    import boto3                                                 # noqa: E402

    local = SKILL_FILE.read_text(encoding='utf-8') if SKILL_FILE.exists() else ''
    local_sha = hashlib.sha256(local.encode('utf-8')).hexdigest()[:16]
    out = {'skill_present': 0, 'skill_active': 0,
           'skill_matches_repo': 0, 'local_sha': local_sha}
    if not local:
        out['note'] = f'仓库里没有 {SKILL_FILE.name}'
        return out
    try:
        c = boto3.client('devops-agent',
                         region_name=os.environ.get('REGION', 'ap-northeast-1'))
        items = c.list_assets(agentSpaceId=AGENT_SPACE).get('items', [])
        mine = [a for a in items
                if a.get('assetType') == 'skill'
                and (a.get('metadata') or {}).get('name') == SKILL_NAME]
        out['skill_present'] = 1 if mine else 0
        if not mine:
            out['note'] = (f'agent space 里找不到 skill `{SKILL_NAME}` —— '
                           '自发查询率很可能已经掉回基线，需要重跑采样确认')
            return out
        meta = mine[0].get('metadata') or {}
        out['skill_active'] = 1 if meta.get('status') == 'ACTIVE' else 0
        out['asset_id'] = mine[0].get('assetId')
        out['asset_version'] = mine[0].get('version')
        # 内容比对：远端是 zip，取出正文再算哈希。取不到就只报存在性，
        # 不猜 —— 报一个错的「一致」比报「未知」糟得多。
        try:
            import ast
            import io
            import zipfile
            raw = (c.get_asset_content(agentSpaceId=AGENT_SPACE,
                                       assetId=mine[0]['assetId'])
                   .get('content', {}).get('zipFile'))
            if isinstance(raw, str):
                raw = ast.literal_eval(raw)
            if hasattr(raw, 'read'):
                raw = raw.read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            body = z.read(z.namelist()[0]).decode('utf-8')
            out['remote_sha'] = hashlib.sha256(
                body.encode('utf-8')).hexdigest()[:16]
            out['skill_matches_repo'] = 1 if out['remote_sha'] == local_sha else 0
            if not out['skill_matches_repo']:
                out['note'] = ('远端 skill 内容与仓库不一致 —— '
                               '有人在控制台改过它，或仓库这份没同步上去')
        except Exception as exc:                                 # noqa: BLE001
            out['note'] = f'取不到远端正文（{type(exc).__name__}），只报存在性'
    except Exception as exc:                                     # noqa: BLE001
        out['note'] = f'devops-agent 调用失败：{type(exc).__name__}: {exc}'
    return out


def emit(metrics: dict, dry_run: bool) -> None:
    import boto3                                                 # noqa: E402

    data = [{'MetricName': k, 'Value': float(v),
             'Unit': 'Percent' if k.endswith('_pct') else 'Count'}
            for k, v in metrics.items() if isinstance(v, (int, float))]
    if dry_run:
        print(f'  [dry-run] 本会发 {len(data)} 个指标到 {NAMESPACE}')
        return
    cw = boto3.client('cloudwatch',
                      region_name=os.environ.get('REGION', 'ap-northeast-1'))
    for i in range(0, len(data), 20):        # PutMetricData 单次上限 20
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 20])
    print(f'  已发 {len(data)} 个指标到 {NAMESPACE}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='只打印不发指标')
    args = ap.parse_args()

    cov = graph_coverage()
    guard = skill_guard()
    merged = {**cov, **{k: v for k, v in guard.items()
                        if isinstance(v, (int, float))}}

    print('=== 依赖图验证覆盖 ===')
    for k, v in cov.items():
        print(f'  {k:26s} {v}')
    print('=== agent skill 守卫 ===')
    for k, v in guard.items():
        print(f'  {k:26s} {v}')

    emit(merged, args.dry_run)

    # 退出码给 cron 用：0 正常，2 表示有值得看一眼的事。
    # 刻意不因「覆盖率低」退非零 —— 低覆盖是现状不是故障，
    # 天天报警只会让人把告警关掉。
    bad = (guard.get('skill_present') == 0
           or guard.get('skill_active') == 0
           or guard.get('skill_matches_repo') == 0
           or cov['refutable_edges'] == 0)
    if bad:
        print('\n⚠️ 有需要关注的项：' + json.dumps(
            {'skill': guard.get('note', ''),
             'refutable_edges': cov['refutable_edges']},
            ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""清掉网关 target 被误建成 AgentTool 留下的残留（5 个节点 + 5 条 RoutesTo 边）。

## 残留是怎么产生的

`etl_agentcore` 曾把 Gateway 的每个 target 一律建成 `AgentTool` 节点 + 一条
`AgentGateway -[RoutesTo]-> AgentTool` 边。但控制面实测这 5 个 target 的
`targetType` 全是 `AGENTCORE_RUNTIME`，`targetConfiguration.http.agentcoreRuntime.arn`
指向的是**已经建模过的 AgentRuntime** —— 于是图里多出 5 个不存在的「工具」
（orchestrator / concierge / nutrition / ordering / adoption）。

提交 `2be2048` 已改成建 `RoutesToRuntime -> AgentRuntime`，但那份改动直到
2026-09-15 才真正生效 —— 因为部署侧的 botocore 太旧，
把 `targetConfiguration` 这个 tagged union 的 `http` 成员**静默剥掉**了
（日志「Received a tagged union response with member unknown to client: http」），
`_runtime_arn_from_target` 因此拿不到 arn，一直走老分支。
加挂 `botocore-current:1` 层后修复生效，`RoutesToRuntime` 出现 5 条。

## 为什么必须手动清

新代码**停止产生**这些残留，但不会删已有的。而且两者的过期行为不同：

    AgentTool 节点   expires_seconds = 604800（7 天）→ 会自然失活
    RoutesTo 边      expires_seconds = None        → **永不过期**

所以放着不管，7 天后会剩下 5 条指向已失活节点的边，且永久留存。

**不给 RoutesTo 加 TTL** 的原因：这个标签同时承载 16 条合法的
`LoadBalancer -[RoutesTo]-> TargetGroup`（CFN 静态声明，本来就不该有 TTL）。
`expires_seconds` 是**边类型级**的，加了会误伤那 16 条。

## 判据（刻意用身份键，不用 name）

`AgentTool` 的身份键是 `tool_key`，形如 `{owner_arn}#{tool_name}`：

    幽灵（网关 target）  ...:gateway/waggleaigateway-th4m2rp46p#adoption
    真工具（runtime 注册）...:runtime/WaggleAIOrchestrator-K85tG867Xt#adoption

**两者 name 相同**（都叫 `adoption`），只有 tool_key 能分开。按 name 删会连真工具
一起删掉 —— 而那个真工具上挂着 `WaggleAIOrchestrator -[InvokesTool]-> adoption`。
本脚本因此只按 `:gateway/` 前缀匹配，并在删除前逐条打印 tool_key 供核对。

## 安全性

- 删除前把 5 条边与 5 个节点的全部属性写到备份文件
- 先 `--dry-run` 看清单，确认后再 `--apply`
- 删除后复验：RoutesTo 应只剩 LoadBalancer -> TargetGroup 那批，
  且 `RoutesToRuntime` / `InvokesTool` 条数不变（证明没误伤）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / 'rca')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from neptune import neptune_client as nc          # noqa: E402

# 只匹配「网关 target 误建」这一类。刻意写成完整的 ARN 片段而不是 'gateway'
# 一个词 —— 后者会匹配到任何名字里带 gateway 的真工具。
_PHANTOM_KEY_MARK = ':gateway/'


def survey() -> dict:
    """盘点残留，并同时取真工具的计数作为「没误伤」的对照基线。"""
    phantom_edges = nc.results(
        "MATCH (g:AgentGateway)-[e:RoutesTo]->(t:AgentTool) "
        "RETURN g.name AS gw, t.name AS tool, t.tool_key AS tool_key, "
        "properties(e) AS edge_props, properties(t) AS node_props")
    baseline = {}
    for label in ('RoutesToRuntime', 'RoutesVia', 'InvokesTool', 'Delegates'):
        baseline[label] = nc.results(
            f"MATCH ()-[e:{label}]->() RETURN count(e) AS c")[0]['c']
    baseline['RoutesTo_total'] = nc.results(
        "MATCH ()-[e:RoutesTo]->() RETURN count(e) AS c")[0]['c']
    baseline['RoutesTo_lb_to_tg'] = nc.results(
        "MATCH (:LoadBalancer)-[e:RoutesTo]->(:TargetGroup) "
        "RETURN count(e) AS c")[0]['c']
    baseline['AgentTool_total'] = nc.results(
        "MATCH (t:AgentTool) RETURN count(t) AS c")[0]['c']
    return {'phantom_edges': phantom_edges, 'baseline': baseline}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真的删除（默认只列清单）')
    ap.add_argument('--backup',
                    default='/home/ec2-user/.kiro/crew/scratch/'
                            'phantom_gateway_targets_backup.json')
    args = ap.parse_args()

    s = survey()
    edges, base = s['phantom_edges'], s['baseline']

    print(f'=== 残留清单（{len(edges)} 条 RoutesTo + 同数量 AgentTool 节点）===')
    for e in edges:
        key = str(e.get('tool_key') or '')
        mark = '幽灵 ✅' if _PHANTOM_KEY_MARK in key else '⚠️ 判据不匹配，跳过'
        print(f"  {e['gw']} -RoutesTo-> {str(e['tool']):14s} {mark}")
        print(f"      tool_key = {key}")

    todo = [e for e in edges if _PHANTOM_KEY_MARK in str(e.get('tool_key') or '')]
    if len(todo) != len(edges):
        print(f"\n⚠️ {len(edges) - len(todo)} 条不符合 tool_key 判据，不会被删除。"
              " 先查清它们是什么再继续。")

    print('\n=== 对照基线（删除后这些数字必须不变，证明没误伤）===')
    for k, v in base.items():
        print(f'  {k:22s} {v}')

    if not args.apply:
        print('\n（--dry-run 模式，什么都没改。加 --apply 执行。）')
        return 0

    if not todo:
        print('\n没有符合判据的残留，无需处理。')
        return 0

    pathlib.Path(args.backup).write_text(
        json.dumps({'edges': edges, 'baseline': base},
                   ensure_ascii=False, indent=1, default=str))
    print(f'\n已备份到 {args.backup}')

    # 逐条按 tool_key 删 —— 不用一条批量语句，便于失败时知道停在哪。
    for e in todo:
        key = e['tool_key']
        nc.query(
            "MATCH (g:AgentGateway)-[r:RoutesTo]->(t:AgentTool {tool_key: $k}) "
            "DELETE r", {'k': key})
        nc.query("MATCH (t:AgentTool {tool_key: $k}) DETACH DELETE t", {'k': key})
        print(f"  已删 边+节点: {e['tool']}  ({key[-46:]})")

    after = survey()['baseline']
    print('\n=== 删除后复验 ===')
    ok = True
    for k, v in base.items():
        now = after[k]
        if k == 'RoutesTo_total':
            expect = v - len(todo)
        elif k == 'AgentTool_total':
            expect = v - len(todo)
        else:
            expect = v
        flag = '✅' if now == expect else '❌'
        if now != expect:
            ok = False
        print(f'  {k:22s} {v} → {now}  期望 {expect}  {flag}')
    left = nc.results(
        "MATCH (g:AgentGateway)-[e:RoutesTo]->(t:AgentTool) "
        "RETURN count(e) AS c")[0]['c']
    print(f'  残留的 AgentGateway->AgentTool RoutesTo: {left}  '
          f"{'✅' if left == 0 else '❌'}")
    return 0 if (ok and left == 0) else 1


if __name__ == '__main__':
    raise SystemExit(main())

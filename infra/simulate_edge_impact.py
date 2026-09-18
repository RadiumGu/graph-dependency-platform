#!/usr/bin/env python3
"""依赖图蒙特卡洛仿真：在**不注入**的前提下给依赖边排序。

## 出处与动机

方法出自 arXiv:2506.11176（Krasnovsky & Zorkin, 2025，
*Model Discovery and Graph Simulation: A Lightweight Alternative to Chaos
Engineering*，代码与数据在 Zenodo 15396047）。论文的做法是：从 trace 抽出有向
依赖图 → 在图上跑蒙特卡洛随机失效 → 预测请求成功率 → 再用真实混沌实验对照验证。

论文给出的保真度是**可引用的**：DeathStarBench Social Network 上无副本时实测韧性
0.186 vs 图预测 0.161，有副本时实测与预测均收敛至 0.305，**平均绝对误差 ≤ 0.0004**。

**为什么对本项目有用**：真实注入的成本很高（每轮 3 分钟注入 + Chaos Mesh 路径还要
删 Pod 重建），而当前 100+ 条依赖边里绝大多数未验证。仿真能在零注入成本下先算出
「哪条边最值得花一次真实注入」，把有限的注入预算投到影响面最大的边上。

## 本实现与论文的差别（如实说明）

论文预测的是**系统可用性**并与实测韧性对照；本实现只做**排序**，不声称能预测
可用性数值 —— 因为：

1. 本图**不建模冗余**（fallback / 缓存 / 副本）。无冗余的图会系统性**高估**故障
   影响：任何一条边断掉，下游就被算作不可达，而现实中重试与回退会吸收一部分。
   所以输出只能当**相对排序**用，绝对值无意义。
2. 论文的入口点来自 trace 的请求起点；本实现用**入度为 0 的节点**近似入口，
   并可用 --entry 覆盖。

## 同时给出的第二个指标：割点

除蒙特卡洛外还算**割点（articulation point，Tarjan O(V+E)）**——移除它会使图分裂
的节点。这是纯图算法给出的 SPOF 候选，与蒙特卡洛互为对照：两者都指向同一条边时
置信度更高，只有一个指向时说明该边的影响依赖具体路径分布。

用法：
    NEPTUNE_ENDPOINT=... python3.11 infra/simulate_edge_impact.py --trials 2000
"""
from __future__ import annotations

import argparse
import pathlib
import random
import sys
from collections import defaultdict, deque

REPO = pathlib.Path(__file__).resolve().parents[1]
for p in (REPO / 'infra' / 'lambda' / 'shared' / 'python',):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from neptune_client_base import neptune_query  # noqa: E402

DEP_LABELS = ('AccessesData', 'Calls', 'DependsOn')


def _flat(q):
    rows = neptune_query(q)['result']['data']['@value']
    if not rows:
        return []
    out = []
    for r in rows[0]['@value']:
        it = iter(r['@value'])
        d = dict(zip(it, it))
        out.append({k: (v['@value'] if isinstance(v, dict) else v) for k, v in d.items()})
    return out


def load_graph() -> tuple[list[dict], dict, set]:
    """拉出依赖边与节点集。返回 (edges, adj, nodes)。"""
    labels = ','.join(f"'{x}'" for x in DEP_LABELS)
    # 端点必须 coalesce：Gremlin 的 project 在某个 by() 无结果时会**整键缺失**，
    # 而实测确有依赖边的端点节点没有 name 属性 —— 不兜住会直接 KeyError，
    # 兜住后还能把这类边数出来（见 main 里的告警）。
    edges = _flat(
        f"g.E().hasLabel({labels})"
        ".project('eid','src','dst','label','status','conf')"
        ".by(__.id())"
        ".by(__.coalesce(__.outV().values('name'), __.constant('<unnamed>')))"
        ".by(__.coalesce(__.inV().values('name'), __.constant('<unnamed>')))"
        ".by(__.label())"
        ".by(__.coalesce(__.values('verify_status'), __.constant('untested')))"
        ".by(__.coalesce(__.values('verify_confidence'), __.constant(-1.0)))"
        ".fold()")
    adj = defaultdict(set)
    nodes = set()
    for e in edges:
        adj[e['src']].add(e['dst'])
        nodes.add(e['src'])
        nodes.add(e['dst'])
    return edges, adj, nodes


def reachable(adj: dict, entries: set, blocked_edge=None) -> set:
    """从 entries 出发能到达的节点集；blocked_edge=(src,dst) 表示该边被打断。"""
    seen, q = set(entries), deque(entries)
    while q:
        n = q.popleft()
        for m in adj.get(n, ()):
            if blocked_edge and (n, m) == blocked_edge:
                continue
            if m not in seen:
                seen.add(m)
                q.append(m)
    return seen


def monte_carlo_edge_impact(edges, adj, nodes, entries, trials: int, seed: int = 42):
    """每条边被打断时，从入口可达的节点数减少多少（多轮随机采样取均值）。

    随机性来自：每一轮随机选一个入口子集（模拟不同请求类的流量分布）。
    刻意不随机化「边是否存在」—— 那会把「图本身对不对」与「这条边多要紧」
    两个问题混在一起，而前者正是真实注入要回答的。
    """
    rng = random.Random(seed)
    entries = sorted(entries)
    if not entries:
        return {}
    base_tot = 0
    impact = defaultdict(float)
    for _ in range(trials):
        k = rng.randint(1, len(entries))
        sub = set(rng.sample(entries, k))
        base = reachable(adj, sub)
        base_tot += len(base)
        for e in edges:
            pair = (e['src'], e['dst'])
            if e['src'] not in base:
                continue        # 这一轮该边根本不在可达路径上，不计
            lost = len(base) - len(reachable(adj, sub, blocked_edge=pair))
            if lost:
                impact[e['eid']] += lost
    for eid in impact:
        impact[eid] /= trials
    return impact


def articulation_points(adj: dict, nodes: set) -> set:
    """无向化后的割点（Tarjan，O(V+E)）。移除它会使图分裂 —— SPOF 候选。"""
    und = defaultdict(set)
    for u, vs in adj.items():
        for v in vs:
            und[u].add(v)
            und[v].add(u)
    disc, low, parent, ap = {}, {}, {}, set()
    timer = [0]

    def dfs(u):
        # 迭代式，避免深图爆栈
        stack = [(u, iter(sorted(und[u])))]
        disc[u] = low[u] = timer[0]; timer[0] += 1
        children = defaultdict(int)
        while stack:
            node, it = stack[-1]
            advanced = False
            for v in it:
                if v not in disc:
                    parent[v] = node
                    children[node] += 1
                    disc[v] = low[v] = timer[0]; timer[0] += 1
                    stack.append((v, iter(sorted(und[v]))))
                    advanced = True
                    break
                if v != parent.get(node):
                    low[node] = min(low[node], disc[v])
            if not advanced:
                stack.pop()
                if stack:
                    p = stack[-1][0]
                    low[p] = min(low[p], low[node])
                    if parent.get(p) is not None and low[node] >= disc[p]:
                        ap.add(p)
        if children[u] > 1:
            ap.add(u)

    for n in sorted(nodes):
        if n not in disc:
            dfs(n)
    return ap


def main() -> int:
    ap_ = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument('--trials', type=int, default=1000)
    ap_.add_argument('--top', type=int, default=20)
    ap_.add_argument('--entry', action='append',
                     help='显式指定入口节点（可多次）。默认取入度为 0 的节点')
    args = ap_.parse_args()

    edges, adj, nodes = load_graph()
    indeg = defaultdict(int)
    for e in edges:
        indeg[e['dst']] += 1
    entries = set(args.entry) if args.entry else {n for n in nodes if indeg[n] == 0}

    print(f"依赖边 {len(edges)} 条 / 节点 {len(nodes)} 个 / 入口 {len(entries)} 个")
    unnamed = [e for e in edges if '<unnamed>' in (e['src'], e['dst'])]
    if unnamed:
        print(f"⚠️ {len(unnamed)} 条依赖边的端点节点**没有 name 属性** —— "
              f"契约要求每个节点类型都有身份键，缺 name 说明这些端点要么身份键不是 "
              f"name（此时仿真按 <unnamed> 合并，结果失真），要么是脏数据。"
              f"示例: {[(e['label'], e['src'], e['dst']) for e in unnamed[:3]]}")
    print(f"入口（入度 0）: {sorted(entries)[:8]}{' ...' if len(entries) > 8 else ''}\n")

    impact = monte_carlo_edge_impact(edges, adj, nodes, entries, args.trials)
    aps = articulation_points(adj, nodes)
    print(f"割点（Tarjan，移除即使图分裂）{len(aps)} 个: {sorted(aps)[:10]}\n")

    ranked = sorted(edges, key=lambda e: -impact.get(e['eid'], 0.0))
    print(f"{'排名':<4}{'源':<24}{'目标':<22}{'仿真影响':<9}{'割点':<5}{'状态':<13}{'置信'}")
    print('-' * 92)
    for i, e in enumerate(ranked[:args.top], 1):
        imp = impact.get(e['eid'], 0.0)
        is_ap = '是' if e['dst'] in aps or e['src'] in aps else ''
        conf = e['conf']
        conf = '未验证' if conf in (None, -1.0) else f"{float(conf):.3f}"
        print(f"{i:<4}{str(e['src'])[:22]:<24}{str(e['dst'])[:20]:<22}"
              f"{imp:<9.3f}{is_ap:<5}{str(e['status']):<13}{conf}")

    untested = [e for e in ranked if e['status'] in ('untested', 'inconclusive')]
    print(f"\n未判定/未决的边里影响最大的前 5 条 —— 这就是下一批注入该投的地方：")
    for i, e in enumerate(untested[:5], 1):
        print(f"  {i}. {e['src']} -[{e['label']}]-> {e['dst']}"
              f"   仿真影响={impact.get(e['eid'], 0.0):.3f}  状态={e['status']}")
    print("\n⚠️ 绝对值无意义，只看相对排序：本图不建模冗余（fallback/缓存/副本），"
          "会系统性高估故障影响。见本文件头部说明。")
    return 0


if __name__ == '__main__':
    sys.exit(main())

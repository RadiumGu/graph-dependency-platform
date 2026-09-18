#!/usr/bin/env python3
"""把已被删除的工作负载从图谱里摘掉 —— 只摘活集群里确实已不存在的。

    python3 scripts/prune_deleted_workloads.py            # dry-run
    python3 scripts/prune_deleted_workloads.py --apply    # 实写

## 为什么需要这个工具

ETL 是**增量**的：它把观测到的东西写进图谱，但没有东西负责把消失的对象摘掉。
这是刻意的 —— 「当轮没观测到」不等于「已经不存在」（那正是 `drift_status` 里
`observed_then_silent` 要表达的意思，见 `etl_deepflow` 的判据）。

代价是：真正被删掉的工作负载会永远留在图谱里，而且 `scope=observed`。
2026-09-13 实测的 `awesomeshop` 就是这样 —— namespace 里 6 个 Service 的
endpoints 全空、零 Deployment、零 Pod、179 天没有任何东西在跑，但图谱里
17 个节点 40 条边一应俱全，其中 4 条 `AccessesData` 边还进了合规报告的
一跳依赖表，**污染了证据覆盖率的分母**。

打不断一个没在跑的东西 —— 这类边结构上不可验证，留在分母里只会让覆盖率
永远达不到目标，而且给读者一个「这里有依赖待验证」的错误印象。

## 判据：只摘活集群里已不存在的

这是本工具唯一的删除依据，也是它按构造不会过度删除的原因：

    图谱里有、活集群里没有  → 可摘
    活集群里还有            → 一律不摘（哪怕它看起来是死的）

**不用名字清单做判据。** 名字清单会腐烂，而且 `auth-service` 这种通用名
极易和别的 namespace 撞车（petsite 侧就有同名风险）。活集群的实际状态是
唯一不会说谎的来源。

## 三道守卫

1. **namespace 必须已从 K8s 消失** —— 否则拒绝执行。防的是「namespace 还在，
   只是当轮 kubectl 抖了一下」被误读成已删除。
2. **CFN 栈必须已删除或不存在** —— 栈还在说明 AWS 侧资源还在，`aws-etl`
   下一轮会把节点重新学回来，此时摘掉只是徒劳且会掩盖真实状态。
3. **审计清单先落盘再删** —— 删了什么必须可追溯。清单写进
   `todo/pruned-workloads_<时刻>.json`。

## 为什么不是「标记」而是「删除」

本仓库整体偏向「披露而非删除」。但这类节点留着是在**保留一个谎**：
`scope=observed` 而实际什么都不存在。合规报告的留存要求（SYSC 15A.6.2R）
针对的是**已出具的报告版本**，不是图谱历史 —— 已发出的报告仍然记录着当时
为真的内容，所以摘掉当前图谱里的死节点不违反留存。
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "rca"))
sys.path.insert(0, str(_ROOT / "infra" / "lambda" / "shared" / "python"))

from neptune import neptune_client as nc  # noqa: E402

#: 声明式的清理目标。每项 = 一个已下线的工作负载组。
#:
#: 做成声明式而不是命令行参数，是为了让「摘过什么」留在版本库里可审计 ——
#: 一次性命令行调用不留痕迹，而这类操作必须留痕。
#: 由 tests/test_72_prune_deleted_workloads.py 校验结构。
PRUNE_TARGETS = (
    {
        "namespace": "awesomeshop",
        "cfn_stack": "AwesomeShopInfra",
        "why": "2026-03-18 部署的独立 demo，179 天零 Pod；"
               "2026-09-13 经用户确认彻底下线，K8s namespace 与 CFN 栈均已删除",
    },
)


def _kubectl_json(*args) -> dict | None:
    """跑一条只读 kubectl，返回 JSON。失败返回 None（区别于「返回空」）。"""
    r = subprocess.run(["kubectl", *args, "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _namespace_gone(ns: str) -> tuple[bool, str]:
    """守卫 1：namespace 必须已从 K8s 消失。

    区分「不存在」与「查不到」—— 后者是工具失败，不能当成已删除。
    这是本仓库反复吃过的教训：连不上就 skip 只对连接失败放行，
    把工具失败读成「空结果」会导致误删。
    """
    r = subprocess.run(["kubectl", "get", "ns", ns],
                       capture_output=True, text=True)
    if "NotFound" in r.stderr or "not found" in r.stderr:
        return True, "namespace 已不存在"
    if r.returncode == 0:
        return False, "namespace 仍然存在 —— 拒绝执行"
    return False, "kubectl 查询失败（%s）—— 拒绝执行，工具失败不等于已删除" % r.stderr.strip()[:80]


def _stack_gone(stack: str) -> tuple[bool, str]:
    """守卫 2：CFN 栈必须已删除或不存在。"""
    r = subprocess.run(
        ["aws", "cloudformation", "describe-stacks", "--stack-name", stack,
         "--region", os.environ.get("REGION", "ap-northeast-1"), "--output", "json"],
        capture_output=True, text=True)
    if r.returncode != 0:
        if "does not exist" in r.stderr:
            return True, "栈不存在"
        return False, "describe-stacks 失败（%s）—— 拒绝执行" % r.stderr.strip()[:80]
    try:
        st = json.loads(r.stdout)["Stacks"][0]["StackStatus"]
    except Exception:
        return False, "无法解析栈状态 —— 拒绝执行"
    if st == "DELETE_COMPLETE":
        return True, "栈状态 DELETE_COMPLETE"
    return False, "栈状态 %s —— AWS 侧资源可能还在，aws-etl 会把节点学回来" % st


def _live_k8s_names() -> set[str] | None:
    """活集群里所有 Service / Deployment / HPA / Pod 的名字。

    返回 None 表示查询失败 —— 调用方必须据此中止，绝不能当成空集，
    否则「活集群里没有」这个判据会对所有节点成立。
    """
    names: set[str] = set()
    for kind in ("svc", "deploy", "hpa", "pods", "statefulsets", "daemonsets"):
        d = _kubectl_json("get", kind, "-A")
        if d is None:
            return None
        for it in d.get("items", []):
            names.add(it["metadata"]["name"])
    return names


def _graph_nodes_for(ns: str) -> list[dict]:
    """图谱里归属该 namespace 的节点闭包。

    三条来路，覆盖三类节点：

      a) **`n.namespace = <ns>`** —— 主判据。K8s 派生节点（Microservice /
         K8sService / Deployment / HPA）都带这个属性，一条查询精确抓全。
      b) 名字里含该 namespace 的 —— 抓 AWS 侧资源（ECRRepository 叫
         `awesomeshop/xxx`、RDSInstance 叫 `awesomeshopinfra-xxx`、
         TargetGroup 叫 `awesomeshop-frontend`），以及 Namespace 节点自身。
      c) 通过 OwnedBy 指向该 Namespace 的 —— 兜底，防 a) 的属性缺失。

    ## 前两版都漏了节点，判据换了两次

    第一版只有 b) + c)：漏了 6 个 K8sService + 6 个 Deployment + 6 个 HPA ——
    它们既无 OwnedBy、名字也不含 namespace（就叫 `auth-service`、`frontend`）。

    第二版改成「从 Microservice 结构性地取一跳伴生对象」：仍漏 `frontend`
    三件套，因为 DeepFlow 从未观测到 `frontend` 这个服务，压根没有对应的
    Microservice 节点可作起点。

    第三版发现节点上本来就有 `namespace` 属性 —— 一条等值查询全中。
    **教训是先看清数据模型再设计遍历**：两版遍历都是在绕一个本来不存在的问题。
    """
    out: dict[str, dict] = {}
    esc = ns.replace("'", "\\'")

    def add(rows):
        for r in rows:
            if not r.get("name"):
                continue
            out.setdefault("%s|%s" % (r.get("lbl"), r.get("name")), r)

    _ret = ("RETURN labels(n)[0] AS lbl, n.name AS name, n.scope AS scope, "
            "       n.source AS source")

    # a) 主判据：namespace 属性
    add(nc.results("MATCH (n) WHERE n.namespace = '%s' %s" % (esc, _ret)))
    # b) AWS 侧资源与 Namespace 节点自身
    add(nc.results("MATCH (n) WHERE n.name IS NOT NULL "
                   "AND n.name CONTAINS '%s' %s" % (esc, _ret)))
    # c) 兜底：OwnedBy
    add(nc.results("MATCH (n)-[:OwnedBy]->(x) WHERE x.name = '%s' %s"
                   % (esc, _ret)))
    return list(out.values())


def _edges_of(label: str, name: str) -> list[dict]:
    esc = name.replace("'", "\\'")
    q = ("MATCH (n:%s)-[d]-(o) WHERE n.name = '%s' "
         "RETURN type(d) AS et, o.name AS other, labels(o)[0] AS olbl, "
         "       d.source AS src" % (label, esc))
    return nc.results(q)


def main() -> int:
    apply = "--apply" in sys.argv
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")

    live = _live_k8s_names()
    if live is None:
        print("✗ 无法读取活集群对象清单 —— 中止。")
        print("  「活集群里没有」是本工具唯一的删除判据；查询失败时该判据会对")
        print("  所有节点成立，那会摘掉整张图。宁可不跑也不要在这种状态下跑。")
        return 2
    print("活集群对象名 %d 个（Service/Deployment/HPA/Pod/STS/DS）" % len(live))
    print()

    audit: list[dict] = []
    total_nodes = total_edges = 0

    for tgt in PRUNE_TARGETS:
        ns, stack = tgt["namespace"], tgt["cfn_stack"]
        print("── 目标 %s ──" % ns)
        print("   理由: %s" % tgt["why"])

        ok, why = _namespace_gone(ns)
        print("   守卫1 namespace: %s（%s）" % ("通过" if ok else "拒绝", why))
        if not ok:
            continue
        ok, why = _stack_gone(stack)
        print("   守卫2 CFN 栈:    %s（%s）" % ("通过" if ok else "拒绝", why))
        if not ok:
            continue

        nodes = _graph_nodes_for(ns)
        print("   图谱闭包 %d 个节点" % len(nodes))
        prunable, kept = [], []
        for n in nodes:
            if n.get("name") in live:
                kept.append(n)          # 活集群里还有 → 一律不摘
            else:
                prunable.append(n)
        for n in kept:
            print("   ⏸ 保留 %-14s %-34s（活集群里仍存在）"
                  % (n.get("lbl"), str(n.get("name"))[:34]))

        for n in prunable:
            edges = _edges_of(n["lbl"], n["name"])
            total_edges += len(edges)
            total_nodes += 1
            audit.append({
                "namespace": ns, "label": n["lbl"], "name": n["name"],
                "scope": n.get("scope"), "source": n.get("source"),
                "edges": edges,
            })
            print("   %s %-14s %-34s 边 %d 条"
                  % ("🗑" if apply else "·", n["lbl"], str(n["name"])[:34], len(edges)))

    print()
    print("%s 待摘 %d 个节点 / %d 条边" %
          ("[实写]" if apply else "[dry-run]", total_nodes, total_edges))

    if not audit:
        print("没有可摘的节点。")
        return 0

    # 审计清单先落盘，再删 —— 顺序不能反。
    out_dir = _ROOT / "todo"
    out_dir.mkdir(exist_ok=True)
    manifest = out_dir / ("pruned-workloads_%s.json" % stamp)
    manifest.write_text(json.dumps({
        "taken_at": stamp, "applied": apply,
        "targets": [t["namespace"] for t in PRUNE_TARGETS],
        "node_count": total_nodes, "edge_count": total_edges,
        "nodes": audit,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("审计清单: %s" % manifest.relative_to(_ROOT))

    if not apply:
        print()
        print("这是 dry-run。确认清单无误后加 --apply 实写。")
        return 0

    for item in audit:
        esc = item["name"].replace("'", "\\'")
        nc.results("MATCH (n:%s) WHERE n.name = '%s' DETACH DELETE n"
                   % (item["label"], esc))
    print("已摘除 %d 个节点及其全部边。" % total_nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())

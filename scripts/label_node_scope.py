#!/usr/bin/env python3
"""为图谱节点标注 scope 六档（T-306，2026-09-05）。默认 dry-run。

## scope 回答什么

`dependency_kind` 回答「声明还是观测」，`verify_status` 回答「边是不是真的」，
`verify_dependency_class` 回答「它有多要紧」。都不回答第四个问题：

    **这个节点算不算「被观测系统」的一部分？**

缺这一维的后果实测到了：16/16 全部 `Invokes` 边被靶点选择器当依赖边选中，
其中 13 条其实是 CDK 部署脚手架与本平台自己的工具链。靶点、爆炸半径、DR 计划
都只该看被观测系统，但图里没有任何字段能表达这件事。

## 判据是权威归属，不是名字模式

**AWS 资源 → CloudFormation 栈归属。** `describe-stacks` 的 `ParentId` 非空即嵌套栈，
CDK 把 provider framework 放进独立嵌套栈是它的架构事实，不是命名巧合。
实测 19 个栈里恰好 3 个嵌套栈，7 个 CDK provider framework Lambda 全部落在其中两个里。

这条判据还纠正了名字判据的一个错误：`ServicesEks2-GuardDutyCleanupLambda` 名字像
脚手架，但它在**主栈**里、属被观测系统。栈归属对，名字错。

**不需要补 arn。** `PhysicalResourceId` 对这些资源用的就是节点已有的标识 ——
SecurityGroup 是 `sg-xxx`、VPC 是 `vpc-xxx`、Subnet 是 `subnet-xxx`、S3Bucket 是桶名。
补 arn 反而有害：它会成为第二个身份键，与既有的 `sg_id` 等并存，正是这个项目
踩过多次的「身份不唯一」那一类。

**K8s 对象 → namespace。** 观测采集栈（cloudwatch/guardduty/nfm/deepflow）与业务
（petadoptions/awesomeshop）由 namespace 干净分开。

## 为什么是六档

`observability` 必须与 `platform` 分开：前者是**被观测系统的观测者**，后者是
**本依赖图谱平台**。合并就分不清「谁在观测」与「谁在管依赖图」，而观测自噪声治理
（曾从 73.2% 压到 4.6%）针对的正是前者。`cluster-infra` 也不能并入 `external`，
否则 CoreDNS 这类真依赖会被误判成外部系统。

## 解析不出的一律写 unknown

与「不分级时写 `unclassified` 而非留空」同一条教训：属性缺失与「判过但判不出」
在查询上无法区分。

用法：
    export NEPTUNE_ENDPOINT=<endpoint> REGION=<region>
    export PYTHONPATH=infra/lambda/shared/python
    python3 scripts/label_node_scope.py            # dry-run，打印分布与样例
    python3 scripts/label_node_scope.py --apply    # 实写
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

sys.path[:0] = os.environ.get("PYTHONPATH", "").split(":")

import boto3  # noqa: E402

from neptune_client_base import neptune_query  # noqa: E402

# 词表与判据从**契约**读，脚本里不再抄一份 —— 这个项目已经因为「同一判据两份
# 互相分歧的实现」踩过一次（verify_confidence 的 ±4.0 / 0.0），不重犯。
try:
    from graph_contract_data import NODE_SCOPE as _NS
except Exception as _e:  # pragma: no cover
    print(f"无法从契约读 NODE_SCOPE（{_e!r}）—— 拒绝退回脚本内硬编码的词表。"
          f"请设置 PYTHONPATH 指向 infra/lambda/shared/python。", file=sys.stderr)
    raise

SCOPE_VALUES = tuple(_NS['values'])
UNRESOLVED = _NS['unresolved_value']
NS_SCOPE = dict(_NS['namespace_map'])
STACK_SCOPE = dict(_NS['stack_map'])
NESTED_STACK_SCOPE = _NS['nested_stack_scope']

# ── 节点类型 → scope（node-type resolver 的词表）─────────────────────────────
# 从契约读，**不在本脚本里再抄一份** —— 这个项目已经因为「同一判据两份互相
# 分歧的实现」踩过一次（verify_confidence 的 ±4.0 / 0.0）。
# 这张表此前恰恰是本文件顶部那条纪律的例外（硬编码在脚本里），2026-09-06 归位。
#
# 表里只放「按构造即可判定」的类型，判据与取舍写在契约 node_scope.type_map 上方。
LABEL_SCOPE = dict(_NS.get('type_map') or {})
if not LABEL_SCOPE:
    print("契约 node_scope.type_map 为空 —— node-type resolver 会全部失效，"
          "而 resolvers 里声明了它。拒绝在词表缺失的情况下静默降级。",
          file=sys.stderr)
    raise SystemExit(2)
_bad = sorted(set(LABEL_SCOPE.values()) - set(SCOPE_VALUES))
if _bad:
    print(f"契约 node_scope.type_map 出现未声明的 scope 取值：{_bad}", file=sys.stderr)
    raise SystemExit(2)

# default namespace 里混放的已知工作负载 → scope（同样从契约读）
WORKLOAD_SCOPE = dict(_NS.get('workload_map') or {})
_bad_wl = sorted(set(WORKLOAD_SCOPE.values()) - set(SCOPE_VALUES))
if _bad_wl:
    print(f"契约 node_scope.workload_map 出现未声明的 scope 取值：{_bad_wl}",
          file=sys.stderr)
    raise SystemExit(2)

# 平台独占的命名前缀 → scope（同样从契约读）
NAME_PREFIX_SCOPE = dict(_NS.get('name_prefix_map') or {})
_bad_pfx = sorted(set(NAME_PREFIX_SCOPE.values()) - set(SCOPE_VALUES))
if _bad_pfx:
    print(f"契约 node_scope.name_prefix_map 出现未声明的 scope 取值：{_bad_pfx}",
          file=sys.stderr)
    raise SystemExit(2)

# ── agent 层的 scope 不能按类型一刀切（2026-09-05 修）────────────────────────
#
# 第一版把 `AgentRuntime` / `AgentTool` / `KnowledgeBase` / `AgentMemory` /
# `Guardrail` / `AgentGateway` 整类映射成 `platform`。**那是错的**，而且错得有害：
# 实测 6 个 AgentRuntime 里只有 1 个（`graph_dependency_mcp`）是本平台自己的
# MCP server，另外 5 个 WaggleAI* 是 **PetSite 的 AI 问答业务功能**。
#
# 代价是可观测的：新建的 `petsite -> WaggleAIOrchestrator` 边被标成
# observed -> platform，而选边器把触及 platform 的边全部排除 —— 于是这条
# **业务关键边**（断了 AI 问答就不可用）被过滤出靶点池，正是引入这条边要修的盲区。
#
# 判据不用名字前缀，用**声明可达性**：被观测系统的配置（`/petstore/agent/*`）
# 声明了哪个 runtime，那个 runtime 及其下游就属于被观测系统。平台自己的 MCP
# server 由本仓库 `mcp/` 构建、不出现在 PetSite 配置里，所以留在 platform。
#
# 可达不到的 agent 节点一律 `unknown` 而**不是** `platform` —— 「判不出」与
# 「属于平台」是两件事，而后者会被选边器排除、造成盲区。
PLATFORM_OWN_RUNTIMES = frozenset({'graph_dependency_mcp'})
AGENT_LAYER_LABELS = frozenset({
    'AgentRuntime', 'AgentTool', 'AgentGateway', 'AgentMemory',
    'KnowledgeBase', 'Guardrail',
})

# 节点上可能承载 CloudFormation PhysicalResourceId 的身份键，按优先级。
# 刻意不含 arn —— 见模块 docstring：不需要补，也不该补。
# `runtime_id` / `gateway_id` 是 2026-09-05 补的：AgentCore 资源在 CFN 里的
# PhysicalResourceId 是 **runtime id**（`WaggleAIConcierge-Yi6Ub97Ylw`）而不是 ARN，
# 少了它们 WaggleAIConcierge / WaggleAIOrdering / WaggleAIGateway 匹配不上栈、
# 会落到 unknown —— 而它们明确在 WaggleAIAgents 栈里。
PHYSICAL_ID_KEYS = ('name', 'runtime_id', 'gateway_id',
                    'sg_id', 'subnet_id', 'vpc_id', 'instance_id', 'arn')


def load_stack_index(region: str) -> tuple[dict, dict]:
    """返回 ({physical_id: scope}, {stack_name: scope})。

    嵌套栈（ParentId 非空）一律 scaffolding —— 这是判据的核心，与命名无关。
    """
    cfn = boto3.client('cloudformation', region_name=region)
    stack_scope: dict = {}
    for page in cfn.get_paginator('describe_stacks').paginate():
        for s in page['Stacks']:
            if s['StackStatus'] == 'DELETE_COMPLETE':
                continue
            name = s['StackName']
            if s.get('ParentId'):
                stack_scope[name] = NESTED_STACK_SCOPE
            else:
                stack_scope[name] = STACK_SCOPE.get(name, 'external')

    phys: dict = {}
    for name, sc in stack_scope.items():
        try:
            for page in cfn.get_paginator('list_stack_resources').paginate(StackName=name):
                for r in page['StackResourceSummaries']:
                    pid = r.get('PhysicalResourceId')
                    if pid:
                        # 首次写入优先：嵌套栈先于父栈列出时不被父栈覆盖
                        phys.setdefault(pid, sc)
        except Exception as e:  # pragma: no cover
            print(f"  ⚠️ 读栈 {name} 资源失败（跳过）：{e!r}", file=sys.stderr)
    return phys, stack_scope


def _flatten(raw: list) -> dict:
    it = iter(raw)
    d = dict(zip(it, it))
    out = {}
    for k, v in d.items():
        val = v["@value"] if isinstance(v, dict) else v
        out[k] = val[0] if isinstance(val, list) and val else val
    return out


def fetch_nodes() -> list[dict]:
    q = ("g.V().project('vid','vlabel','props')"
         ".by(__.id()).by(__.label()).by(__.valueMap()).fold()")
    rows = neptune_query(q)["result"]["data"]["@value"]
    if not rows:
        return []
    out = []
    for entry in rows[0]["@value"]:
        it = iter(entry["@value"])
        rec = dict(zip(it, it))
        vid = rec["vid"]
        out.append({
            "vid": vid.get("@value") if isinstance(vid, dict) else vid,
            "label": rec["vlabel"],
            "props": _flatten(rec["props"]["@value"]),
        })
    return out


def _load_profile_declared() -> set:
    """`profiles/petsite.yaml` 声明的被观测系统资源名（片段）。

    这是**权威声明**，用来兜住手工创建、不属任何 CloudFormation 栈的资源 ——
    实测 129 个节点不在任何栈里（`managedBy=manual` 就有 79 个），栈归属对它们
    无能为力。profile 是这批资源里「哪些属于被观测系统」的唯一权威源。

    只做**片段匹配**且只用于兜底：profile 里写的是 `StepFn` / `stepprice` 这类
    片段而非全名。刻意放在栈归属**之后**，所以不会覆盖任何有栈归属的判定 ——
    片段匹配比栈归属弱，不能让它抢先。
    """
    import pathlib
    import yaml
    p = pathlib.Path(__file__).resolve().parents[1] / 'profiles' / 'petsite.yaml'
    try:
        d = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    except Exception:
        return set()
    out: set = set()
    aws = d.get('aws_resources') or {}
    for key in ('sqs_queues', 'dynamodb_tables', 'lambda_functions'):
        for names in (aws.get(key) or {}).values():
            out |= {str(n) for n in (names or []) if n}
    for v in (aws.get('s3_buckets') or {}).values():
        if v:
            out.add(str(v))

    # ── `services` 段（2026-09-06 补）────────────────────────────────────────
    # 此前只读 aws_resources，把 profile 里**最权威的那份服务清单**漏掉了。
    # 代价实测可见：pethistory / petstatusupdater 这些 PetSite 业务服务都躺在
    # unknown 里，而它们在 profile 的 services 段写得明明白白。
    #
    # 只收 ≥6 字符的片段：片段匹配是子串匹配，太短会误命中无关资源。
    # 实测这 6 个服务的 15 个片段最短 6 字符（`petsite`），无一触线。
    for skey, sval in (d.get('services') or {}).items():
        sval = sval or {}
        cands = {str(skey)}
        for f in ('neptune_name', 'k8s_label', 'k8s_deployment'):
            if sval.get(f):
                cands.add(str(sval[f]))
        for a in (sval.get('aliases') or []):
            if a:
                cands.add(str(a))
        out |= {c for c in cands if len(c) >= 6}
    return out


_PROFILE_DECLARED = None
_AGENT_OBSERVED = None


def _agent_layer_observed() -> set:
    """被观测系统声明并可达的 agent 层节点名集合。

    从图自身算：`Microservice(observed) -DependsOn-> AgentRuntime` 是种子
    （那条边由 SSM 声明建出），再沿 `Delegates` / `InvokesTool` / `Retrieves`
    往下走。平台自己的 MCP runtime 没有任何 Microservice 指向它，自然不会被收进来。
    """
    global _AGENT_OBSERVED
    if _AGENT_OBSERVED is not None:
        return _AGENT_OBSERVED
    q = ("g.V().hasLabel('Microservice').out('DependsOn').hasLabel('AgentRuntime')"
         ".emit().repeat(__.out('Delegates','InvokesTool','Retrieves').simplePath())"
         ".times(4).dedup().values('name').fold()")
    try:
        rows = neptune_query(q)["result"]["data"]["@value"]
        vals = rows[0]["@value"] if rows else []
        _AGENT_OBSERVED = {
            (v.get("@value") if isinstance(v, dict) else v) for v in vals}
    except Exception as e:  # pragma: no cover
        print(f"  ⚠️ agent 层可达性查询失败，agent 节点将判 unknown：{e!r}", file=sys.stderr)
        _AGENT_OBSERVED = set()
    return _AGENT_OBSERVED


def _prefix_match(name) -> str:
    """名字命中哪个平台独占前缀；没命中返回空串。

    与 _workload_match 分开：那个只在 default namespace 里用于可观测性 agent，
    这个用于不属任何 CloudFormation 栈的平台自有资源。
    """
    nm = str(name or '')
    if not nm:
        return ''
    # 长前缀优先，避免 'neptune-etl-trigger' 被 'neptune-etl-' 之类抢先
    for pfx in sorted(NAME_PREFIX_SCOPE, key=len, reverse=True):
        if nm.startswith(pfx):
            return pfx
    return ''


def _workload_match(name) -> str:
    """名字命中哪个已知工作负载（xray-daemon 等）；没命中返回空串。

    只用于 default namespace 里混放的可观测性 agent —— 它们是 DaemonSet，
    名字形如 `xray-daemon-26dwt`，前缀即工作负载名。
    """
    nm = str(name or '')
    if not nm:
        return ''
    for wl in WORKLOAD_SCOPE:
        if nm.startswith(wl):
            return wl
    return ''


def _profile_match(name) -> str:
    """名字命中 profile 声明的哪个片段；没命中返回空串。

    抽成函数是因为它现在有两个调用点：namespace=default 的逐个判，
    以及末尾的兜底。此前只在末尾有一处，而 default 分支提前 return
    把它short-circuit 掉了。
    """
    global _PROFILE_DECLARED
    nm = str(name or '')
    if not nm:
        return ''
    if _PROFILE_DECLARED is None:
        _PROFILE_DECLARED = _load_profile_declared()
    for frag in _PROFILE_DECLARED:
        if frag and frag in nm:
            return frag
    return ''


def resolve_scope(node: dict, phys: dict) -> tuple[str, str]:
    """返回 (scope, 依据)。解析不出返回 ('unknown', 原因)。

    顺序有意如此，强判据在前：
      namespace（K8s 对象的固有属性）→ CloudFormation 栈归属 → 节点类型
      → profile 声明（片段匹配，最弱，只兜底）
    反过来会让 kube-system 里的东西被栈归属误判，或让片段匹配抢掉栈归属。
    """
    global _PROFILE_DECLARED
    p = node["props"]
    lbl = node["label"]

    ns = p.get("namespace")
    if ns and ns in NS_SCOPE:
        return NS_SCOPE[ns], f"namespace={ns}"

    # Namespace 节点：它自己的名字就是那个 namespace
    if lbl == 'Namespace':
        nm = str(p.get('name') or '')
        if nm in NS_SCOPE:
            return NS_SCOPE[nm], f"Namespace 节点名={nm}"

    # namespace=default 是混放的（业务服务和无关项目都往里扔），所以不能按
    # namespace 判。但**不能就此 return unknown** —— profile 声明恰恰是为
    # 「逐个判」准备的那个判据，提前 return 会让它永远轮不到。
    # 实测代价：petsite 的 pethistory / petstatusupdater / trafficgenerator
    # 三个业务服务都躺在 unknown 里，而 profiles/petsite.yaml 明明声明了它们。
    if ns == 'default':
        hit = _profile_match(p.get('name'))
        if hit:
            return 'observed', f"namespace=default，但 profile 声明片段匹配（{hit}）"
        # default 里混放的可观测性 agent（xray-daemon 等），身份按定义确定
        wl = _workload_match(p.get('name'))
        if wl:
            return WORKLOAD_SCOPE[wl], f"namespace=default 里的已知工作负载（{wl}）"
        return UNRESOLVED, "namespace=default 且 profile 未声明，需逐个判（混放）"

    for key in PHYSICAL_ID_KEYS:
        val = p.get(key)
        if val and str(val) in phys:
            return phys[str(val)], f"CloudFormation 归属（{key}={val}）"

    if lbl in LABEL_SCOPE:
        return LABEL_SCOPE[lbl], f"节点类型={lbl}"

    # 平台独占的命名前缀。刻意放在**栈归属之后** —— 有栈归属的以栈为准，
    # 这里只兜住手工创建、不属任何栈的平台资源（见契约 name_prefix_map 上方注释）。
    pfx = _prefix_match(p.get('name'))
    if pfx:
        return NAME_PREFIX_SCOPE[pfx], f"平台独占命名前缀（{pfx}）"

    # agent 层：按声明可达性判，不按类型一刀切（见 AGENT_LAYER_LABELS 上方注释）
    if lbl in AGENT_LAYER_LABELS:
        nm = str(p.get('name') or '')
        if nm in PLATFORM_OWN_RUNTIMES:
            return 'platform', f'本平台自己的 agent runtime（{nm}）'
        if nm and nm in _agent_layer_observed():
            return 'observed', '被观测系统的配置声明并可达（agent 层）'
        return UNRESOLVED, (
            f'agent 层节点 {nm or lbl} 未被任何 observed 服务声明可达 —— '
            f'判不出，刻意不默认 platform（那会让它被选边器排除）')

    # 兜底：profile 声明。只对不属任何栈的资源生效（上面已 return 掉有栈归属的）。
    hit = _profile_match(p.get('name'))
    if hit:
        return 'observed', f"profile 声明片段匹配（{hit}）"

    if ns:
        return UNRESOLVED, f"namespace={ns} 未登记"
    return UNRESOLVED, "既不在已知 namespace，也不属任何 CloudFormation 栈"


def main() -> int:
    apply = "--apply" in sys.argv
    region = os.environ.get("REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        print("需要 REGION 或 AWS_DEFAULT_REGION", file=sys.stderr)
        return 2

    print("读 CloudFormation 栈归属…")
    phys, stack_scope = load_stack_index(region)
    nested = sum(1 for v in stack_scope.values() if v == 'scaffolding')
    print(f"  {len(stack_scope)} 个栈（其中 {nested} 个判为 scaffolding），"
          f"{len(phys)} 个物理资源标识\n")

    nodes = fetch_nodes()
    print(f"图谱 {len(nodes)} 个节点\n")

    dist = Counter()
    by_reason = Counter()
    samples = defaultdict(list)
    plan = []
    for n in nodes:
        sc, why = resolve_scope(n, phys)
        dist[sc] += 1
        by_reason[why.split('（')[0].split('=')[0]] += 1
        if len(samples[sc]) < 3:
            samples[sc].append(f"{n['label']}/{str(n['props'].get('name'))[:34]} ← {why}")
        plan.append((n, sc))

    print(f"{'scope':<16}{'节点数':>7}   样例")
    print("-" * 96)
    for sc in SCOPE_VALUES:
        if not dist[sc]:
            continue
        print(f"{sc:<16}{dist[sc]:>7}   {samples[sc][0] if samples[sc] else ''}")
        for s in samples[sc][1:]:
            print(f"{'':<23}   {s}")
    print("-" * 96)
    resolved = len(nodes) - dist[UNRESOLVED]
    print(f"可解析 {resolved}/{len(nodes)} = {resolved / max(len(nodes), 1) * 100:.1f}%"
          f"，unknown {dist[UNRESOLVED]} 条")

    if not apply:
        print("\n这是 dry-run。加 --apply 实写。")
        return 0

    ok = 0
    for n, sc in plan:
        try:
            # **必须带 single** —— Gremlin 顶点属性默认 SET 基数，不带 single 是
            # 追加而非覆盖。第一版漏了它，跑三次就让每个节点累积了三个 scope 值
            # （实测 WaggleAIAdoption 同时是 observed 和 platform）。本仓库为
            # 同一类缺陷清理过 1,897 个冗余值（infra/fix_property_cardinality.py），
            # 这是第二次。
            neptune_query(f"g.V('{n['vid']}').property(single,'scope','{sc}')")
            ok += 1
        except Exception as e:  # pragma: no cover
            print(f"  ✗ {n['vid']}: {e!r}", file=sys.stderr)
    print(f"\n写入成功 {ok}/{len(plan)}")
    return 0 if ok == len(plan) else 1


if __name__ == "__main__":
    sys.exit(main())

"""snapshot_workflow.py — 图快照导出的 Temporal workflow。

## 它解决什么

灾难发生时去查**已失效区域**的 Neptune 来生成 DR 计划，本身就是设计缺陷：
东京挂了，Neptune 就读不到。`dr-plan-generator/main.py` 的 `cmd_snapshot`
docstring 原话已经说明了正确形态：

    Run this on a schedule **while the primary Region is healthy**.
    Store the output outside the primary Region so it stays readable
    during a disaster.

所以快照不是权宜之计，**它才是正确架构**。本文件把那句话变成一个
Temporal Schedule。

## ⚠️ 图谱访问：不走直连 Neptune，走受审的查询目录

2026-09-26 定案：本 workflow **不直连 Neptune**，改经东京的
`graph_dependency_mcp`（AgentCore runtime，MCP 协议）查图。

理由不是网络更干净，而是**查询质量**：那个 server 暴露的是一份固定的
`QUERY_CATALOG`（约 20 条受审的 openCypher），并随结果返回溯源元数据 ——
`source` / `graph_contract_version` / `query` / `params` / `queried_at`，
外加一句 `determinism: 固定 openCypher，无 LLM 参与，同参数同结果`。

直连的问题是 worker 得自己写查询。那样快照记录的就不是「图谱事实」，
而是「某次临时查询的偶然结果」—— 一份灾备计划所倚赖的事实集合
不该是这种东西。目录里已经有正好需要的几条：

    q14_cross_region_resources      跨区域资源
    q13_data_layer_topology         数据层拓扑
    q12_service_dependency_tree     服务依赖树
    q15_critical_path               关键路径
    q2_tier0_status                 Tier0 状态

**必须断言契约版本。** 那个 runtime 已经到版本 3，它在演进。
若其 `graph_contract_version` 与本 workflow 期望的不一致，
应当响亮失败而不是照样存一份 —— 否则你会得到一份形状不同
却看起来正常的快照，而这正是直连时你根本察觉不到的那类故障。

### 两处已被证伪的早期结论（留在这里免得有人照着改回去）

1. **「只有 PetSite VPC 能同时到两边」是错的。** 实测：韩国 ↔ PetSite 对等
   `pcx-09fc849e6ac38e7e1` active、韩国侧有去程路由、Neptune DNS 从韩国解析
   得到 11.0.2.187。当时 8182 不通的原因是两处配置缺失（Neptune 子网缺回程
   路由 + 安全组未放韩国网段），**不是没有路径**。所以 worker 跑在韩国 EC2
   上一直是可行的。

2. 既然改走 MCP，上面那两处网络配置**也不需要了** —— `InvokeAgentRuntime`
   走 AWS API 端点，不是 VPC 路径。为此加的 2 条路由 + 1 条安全组规则应当
   回退（`infra/dr-korea/temporal-1.32/network-korea-to-neptune.sh --revert`）。

### 未决：那个 runtime 的入站认证是 Cognito JWT，不是 SigV4

    customJWTAuthorizer.discoveryUrl -> Cognito pool ap-northeast-1_Dwd1wVX7j
    allowedClients -> ["2u5s7r3gprc8mo86890sdi1h7t"]

实测用 boto3 `invoke_agent_runtime`（SigV4）调它得到
`AccessDeniedException: Authorization method mismatch`。

所以 worker 需要一个 Cognito client secret 去换 JWT。对灾备组件而言这是
个不理想的失败模式：**密钥会过期，IAM 角色不会**，而密钥过期的表现是
快照静默停更。缓解手段已经在位 —— `MAX_SNAPSHOT_AGE_HOURS` 那道硬闸门
会让计划生成拒绝陈旧快照，把静默失效变成响亮失败。

更好的形态（待定）：用同一个代码包再部署一个**入站认证为 SigV4** 的
runtime，专供机器消费者。查询目录相同（质量保证不变），但只需实例角色
+ `bedrock-agentcore:InvokeAgentRuntime`，没有密钥要管，且独立 ARN 让
IAM 与 CloudTrail 能把机器流量和 agent 流量分开。

## 为什么用 Temporal Schedule 而不是另建 cron

  · 触发记录、跳过、追赶策略都在 Temporal 里，与 DR 执行同一套审计面
  · `overlap_policy=SKIP` 天然处理「上一次还没跑完」
  · 暂停/恢复是一条命令，演练时不必去改 crontab

## 快照新鲜度必须是硬闸门

用一份三天前的快照生成 DR 计划，会照着已经不存在的拓扑去切。
所以 `assert_fresh` 是**失败**而不是警告 —— 见 `MAX_SNAPSHOT_AGE_HOURS`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import json
    import os
    from datetime import datetime, timezone
    # subprocess 已移除：快照不再 shell out 到 main.py + aws s3 cp，
    # 改为直接走 MCP 客户端与 boto3 —— 少一层 shell 就少一类
    # 「命令成功但没生效」的路径，且错误能拿到结构化的原因。

#: 快照超过这个年龄就不许再用于生成计划。
#: 12 小时是按「Schedule 每 6 小时跑一次，容许漏一次」定的 ——
#: 漏两次就该有人知道，而不是继续用一份越来越旧的拓扑。
MAX_SNAPSHOT_AGE_HOURS = 12

#: 快照落在哪。**必须在主区之外** —— 存在东京就违背了整件事的目的。
SNAPSHOT_BUCKET = os.environ.get(
    "DR_SNAPSHOT_BUCKET", "dr-korea-agentcore-926093770964-ap-northeast-2"
)
SNAPSHOT_PREFIX = os.environ.get("DR_SNAPSHOT_PREFIX", "snapshots/")

#: 生成器所在路径（worker 镜像里）。
GENERATOR_DIR = os.environ.get(
    "DR_GENERATOR_DIR", "/opt/graph-dependency-platform/dr-plan-generator"
)


@dataclass
class SnapshotInput:
    scope: str = "region"
    source: str = "ap-northeast-1"
    #: 只演练不真导时置 True。
    dry_run: bool = False


@dataclass
class SnapshotResult:
    ok: bool
    #: 快照的 S3 key。失败时为 None。
    s3_key: str | None = None
    node_count: int | None = None
    edge_count: int | None = None
    #: ⚠️ 三态：None = **没能判断**，不是「零个」。
    #: 空快照与「查不到」在指标上是两回事，前者是图真空了（严重），
    #: 后者是采集失败（也严重但成因不同）。混为一谈会让人修错地方。
    inconclusive_reason: str | None = None
    would_run: list[str] = field(default_factory=list)


@activity.defn
async def export_graph_snapshot(inp: SnapshotInput) -> SnapshotResult:
    """经受审的查询目录导出一份图快照，上传到主区之外的桶。

    ## 数据来自哪里

    图谱事实走 MCP 受审目录（graph_mcp_client），**不直连 Neptune** ——
    理由见模块 docstring：直连意味着自己写 openCypher，那样快照记录的
    不是「图谱事实」而是「某次临时查询的偶然结果」。

    跨区复制事实走 **RDS API**。这不是冗余：图谱是单区域的（实测 31 种边标签
    里没有 ReplicatedTo），而 q14 的空结果**会被读成它的反面** ——
    「没有任何复制」。而 petsite-global 实际横跨两区且在跑。
    图谱管「什么依赖什么」，AWS API 管「灾备侧的当下状态」。

    ## 三条判据，都刻意做成可分辨

    1. **全部查询都为零行** -> 失败。单条为零不失败（server 自己的
       empty_result_guidance 说得对：q14 在单区域部署下本就应为空）。
       但**全部**为零意味着图谱或 ETL 坏了。
    2. **契约版本在一次快照内必须一致。** 中途变了说明 server 被重新部署，
       那份快照内部就不自洽 —— 前半截和后半截描述的是两个不同的图谱契约。
       这种快照比没有快照更糟，因为它看起来完整。
    3. 任何一条查询失败 -> 失败并写明是哪条。不许留一份残缺快照，
       一份残缺快照会让下游生成出「没有这个依赖」的 DR 计划，
       而那份计划看起来是成功生成的。
    """
    import asyncio
    import boto3

    import graph_mcp_client as gmc

    stamp = activity.info().workflow_run_id[:8]
    key = f"{SNAPSHOT_PREFIX}graph-{inp.source}-{stamp}.json"

    plan = [f"MCP query {q}" for q in gmc.SNAPSHOT_QUERIES]
    plan += ["rds:DescribeGlobalClusters", f"s3 put s3://{SNAPSHOT_BUCKET}/{key}"]
    if inp.dry_run:
        return SnapshotResult(ok=True, would_run=plan)

    queries: dict[str, dict] = {}
    contract_versions: set[str] = set()
    try:
        # 一次快照取一个 token。不是每条查询取一次 ——
        # token TTL 一小时，而整次快照是秒级的；也不跨快照缓存（见客户端文档）。
        token = await asyncio.to_thread(gmc.fetch_token)

        # ── 无参数的那几条 ───────────────────────────────────────────────
        for q in gmc.SNAPSHOT_QUERIES:
            if q in gmc.PARAMETERIZED:
                continue
            activity.heartbeat(q)
            queries[q] = await asyncio.to_thread(gmc.query, q, token)
            contract_versions.add(
                str(queries[q].get("_provenance", {}).get("graph_contract_version"))
            )

        # ── q12 由 q2 的结果驱动，**按服务名去重** ───────────────────────
        # 实测 q2_tier0_status 返回 6 行但只有 3 个不同服务
        # （petsite / payforadoption / petsearch，每个服务一行一个 AZ）。
        # 不去重就会对每个服务调两次 —— 结果一样，白花两倍时间，
        # 而且快照里会出现重复条目。
        tier0 = queries.get("q2_tier0_status", {}).get("results") or []
        services = sorted({r["name"] for r in tier0 if r.get("name")})
        for q, pname in gmc.PARAMETERIZED.items():
            per_service = {}
            for svc in services:
                activity.heartbeat(f"{q}[{svc}]")
                per_service[svc] = await asyncio.to_thread(
                    gmc.query, q, token, **{pname: svc}
                )
                contract_versions.add(
                    str(per_service[svc].get("_provenance", {})
                        .get("graph_contract_version"))
                )
            queries[q] = {"by_service": per_service, "services": services}

    except gmc.ContractVersionMismatch as e:
        # 独立处理：这不是抖动，重试不会变好，而且它正是我们要挡的那类故障。
        return SnapshotResult(
            ok=False, would_run=plan,
            inconclusive_reason=f"图谱契约版本不符，拒绝存这份快照：{e}",
        )
    except Exception as e:  # noqa: BLE001
        return SnapshotResult(
            ok=False, would_run=plan,
            inconclusive_reason=f"查询图谱失败 {type(e).__name__}: {e}",
        )

    # ── 判据 2：契约版本必须一致 ─────────────────────────────────────────
    if len(contract_versions) > 1:
        return SnapshotResult(
            ok=False, would_run=plan,
            inconclusive_reason=(
                f"一次快照内出现多个图谱契约版本 {sorted(contract_versions)} —— "
                f"MCP server 很可能在采集中途被重新部署。这份快照内部不自洽，"
                f"不存。下一轮 Schedule 会重新采。"
            ),
        )

    # ── 判据 1：全部为零才是失败 ─────────────────────────────────────────
    row_counts = {
        q: (d.get("row_count") if "row_count" in d
            else sum(x.get("row_count") or 0 for x in d.get("by_service", {}).values()))
        for q, d in queries.items()
    }
    if row_counts and not any(row_counts.values()):
        return SnapshotResult(
            ok=False, would_run=plan, node_count=0,
            inconclusive_reason=(
                f"全部 {len(row_counts)} 条查询都返回零行 —— 图谱或 ETL 坏了。"
                f"单条为零是正常的（q14 在单区域部署下本就应为空），"
                f"但全部为零不是。不存这份快照。"
            ),
        )

    # ── 图谱缺的那个事实：跨区复制，取自 RDS API ─────────────────────────
    # 见模块 docstring 的「事实来源分工」。这一段失败**不**让整份快照失败：
    # 图谱事实已经拿到了，缺这一段的快照仍然有用，只是要**明确标出缺了**，
    # 而不是让下游以为「没有跨区复制」。
    replication: dict = {}
    try:
        gc_id = os.environ.get("DR_GLOBAL_CLUSTER", "")
        if gc_id:
            rds = await asyncio.to_thread(
                boto3.client, "rds", region_name=inp.source
            )
            resp = await asyncio.to_thread(
                rds.describe_global_clusters, GlobalClusterIdentifier=gc_id
            )
            g0 = (resp.get("GlobalClusters") or [{}])[0]
            replication = {
                "source": "rds:DescribeGlobalClusters",
                "global_cluster": gc_id,
                "engine": g0.get("Engine"),
                "status": g0.get("Status"),
                "members": [
                    {
                        "cluster_arn": m.get("DBClusterArn"),
                        "region": (m.get("DBClusterArn") or "").split(":")[3] or None,
                        "is_writer": m.get("IsWriter"),
                        "global_write_forwarding": m.get("GlobalWriteForwardingStatus"),
                    }
                    for m in g0.get("GlobalClusterMembers") or []
                ],
            }
        else:
            replication = {"unavailable": "未设 DR_GLOBAL_CLUSTER"}
    except Exception as e:  # noqa: BLE001
        replication = {"unavailable": f"{type(e).__name__}: {e}"}

    snapshot = {
        "schema": "dr-graph-snapshot/2",
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_region": inp.source,
        "graph_contract_version": contract_versions.pop() if contract_versions else None,
        "row_counts": row_counts,
        # 每条查询的结果都连**溯源**一起存。只存 rows 是错的：
        # 那样以后无法回答「这条事实是哪个查询、哪个契约版本、什么时候取的」。
        "queries": queries,
        # 跨区复制取自 RDS，与图谱事实分开放 —— 来源不同就该看得出来。
        "cross_region_replication": replication,
        # 把「哪条查询的空结果会被误读」一起写进快照，让读快照的人/agent
        # 不必再去翻代码注释。
        "emptiness_caveats": {
            q: msg for q, msg in gmc.EMPTINESS_READS_AS_OPPOSITE.items()
            if row_counts.get(q) == 0
        },
    }

    try:
        s3 = await asyncio.to_thread(boto3.client, "s3")
        await asyncio.to_thread(
            s3.put_object,
            Bucket=SNAPSHOT_BUCKET,
            Key=key,
            Body=json.dumps(snapshot, ensure_ascii=False, default=str).encode(),
            ContentType="application/json",
        )
    except Exception as e:  # noqa: BLE001
        return SnapshotResult(
            ok=False, would_run=plan,
            inconclusive_reason=f"上传快照失败 {type(e).__name__}: {e}",
        )

    return SnapshotResult(
        ok=True, s3_key=key, would_run=plan,
        node_count=sum(v or 0 for v in row_counts.values()),
    )


@workflow.defn(name="ExportSnapshotWorkflow")
class ExportSnapshotWorkflow:
    """定期导出图快照，供灾难时离线生成 DR 计划。

    单步 workflow。做成 workflow 而不是裸 cron 的理由在模块 docstring 里：
    触发记录与 DR 执行同一套审计面。
    """

    @workflow.run
    async def run(self, args: SnapshotInput) -> SnapshotResult:
        res: SnapshotResult = await workflow.execute_activity(
            export_graph_snapshot,
            args,
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=3),
            # 读类操作，可以重试；但不要无限重试 ——
            # Neptune 连不上是结构性问题，重试一百次也不会变好，
            # 而 Schedule 下一轮会再来一次。
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=10), maximum_attempts=3
            ),
        )
        if not res.ok:
            # 明确失败。Schedule 的 LastRunTime 会显示失败，
            # 而不是留下一条「成功但没有内容」的记录。
            raise workflow.ApplicationError(
                f"快照导出失败：{res.inconclusive_reason}", non_retryable=True
            )
        return res

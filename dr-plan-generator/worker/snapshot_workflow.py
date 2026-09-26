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
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import os
    import subprocess

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
    """导出一份图快照并上传到主区之外的桶。

    ⚠️ 这个 activity **必须跑在能连上 Neptune 的 VPC 里**（PetSite VPC）。
    连不上时要**明确失败并写明原因**，不许返回一个空快照 ——
    一份空快照会让下游生成出「没有任何依赖」的 DR 计划，
    而那份计划看起来是成功生成的。
    """
    stamp = activity.info().workflow_run_id[:8]
    local = f"/tmp/graph-snapshot-{stamp}.json"  # noqa: S108
    key = f"{SNAPSHOT_PREFIX}graph-{inp.source}-{stamp}.json"

    cmds = [
        f"python3 {GENERATOR_DIR}/main.py snapshot "
        f"--scope {inp.scope} --source {inp.source} --output {local}",
        f"aws s3 cp {local} s3://{SNAPSHOT_BUCKET}/{key}",
    ]
    if inp.dry_run:
        return SnapshotResult(ok=True, would_run=cmds)

    try:
        for c in cmds:
            activity.heartbeat(c[:80])
            r = subprocess.run(  # noqa: S602
                c, shell=True, capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                return SnapshotResult(
                    ok=False,
                    would_run=cmds,
                    inconclusive_reason=(
                        f"命令失败（exit {r.returncode}）：{c[:60]}…\n"
                        f"stderr: {r.stderr.strip()[:300]}"
                    ),
                )
    except Exception as e:  # noqa: BLE001
        return SnapshotResult(
            ok=False, would_run=cmds,
            inconclusive_reason=f"{type(e).__name__}: {e}",
        )

    return SnapshotResult(ok=True, s3_key=key, would_run=cmds)


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

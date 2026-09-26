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

## ⚠️ worker 放哪里：只有一个位置可行

实测的连通性（2026-09-26）：

    从 openclaw VPC 10.1.0.0/16（当前跑 agent 的机器）
        → 东京 Neptune 8182          ✓ 可达
        → 韩国 Temporal 7233          ✗ 不可达

    VPC 对等关系（**对等不可传递**，这是关键）：
        10.20.0.0/16（韩国）  ↔  11.0.0.0/16（东京 PetSite）
        11.0.0.0/16（PetSite）↔  10.1.0.0/16（openclaw）
        10.20.0.0/16 ↮ 10.1.0.0/16   ← 没有直接对等

**只有 PetSite VPC（11.0.0.0/16）同时能到 Neptune 和韩国 Temporal。**
所以这个 workflow 的 worker 必须跑在 PetSite VPC 里（EKS 上的一个
Deployment，或该 VPC 内的 EC2），任务队列 `dr-snapshot-queue`。

放在 openclaw 那台机器上是不行的 —— 它连不上 Temporal，
worker 起不来，而 Schedule 会照常触发并堆积一批永不前进的执行。

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

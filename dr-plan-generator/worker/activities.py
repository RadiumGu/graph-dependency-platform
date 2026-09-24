"""
activities.py — 切换步骤的 activity 实现。

## 当前阶段:只有 dry_run 真正可用

写类 activity 里调 AWS 的那几行都在 `if not inp.dry_run:` 后面,而
worker 的实例角色目前**只有 Describe 权限**(见
`infra/dr-korea/02-temporal.yaml`)。所以 dry_run=False 现在会因为
AccessDenied 而失败 —— **这是刻意的**:权限按步骤逐个放开,
在步骤还没跑通之前给宽权限等于先开口子。

## 为什么不用 `moto` 之类做假

因为本项目的教训是「测试环境与线上不一致会让错误路径当五个月实际路径」。
dry_run 不假装调用成功,而是**如实报告「没有调用」**,并把将要执行的
命令原文带出来 —— 那样人能看见它究竟打算做什么。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity


@dataclass
class ActivityInput:
    plan_ref: str
    dry_run: bool = True
    #: 仅 promote_database 用:ordered / allow_data_loss。
    decision: str | None = None


@dataclass
class StepResult:
    """一步的结果。

    ⚠️ `verified` 是三态:True / False / **None=未核实**。
    不允许用 False 表示「没查」—— 那就是把「什么都没测到」写成
    「测到的是否」,本项目同类缺陷已四次。
    """

    step: str
    executed: bool
    #: dry_run 时带出将要执行的命令原文,让人能看见它打算做什么。
    would_run: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    #: 无法判断时写明原因 —— 空着等于让读的人自己猜。
    inconclusive_reason: str | None = None
    #: 这一步的核实**测到的到底是什么**。
    #:
    #: 加这个字段是因为 2026-09-24 演练暴露的一件事:`verified=True` 很容易
    #: 被读成「这一步达到目的了」,而实际上它只说明「我测的那个量达标了」。
    #: 两者不同时,必须把差别写出来 —— 否则读的人会做出错误判断。
    detail_note: str | None = None


# ── 环境 ──────────────────────────────────────────────────────────────────

_REGION = os.environ.get("DR_TARGET_REGION", "ap-northeast-2")
_PRIMARY_REGION = os.environ.get("DR_PRIMARY_REGION", "ap-northeast-1")
_PLAN_BUCKET = os.environ.get("DR_PLAN_BUCKET", "")
_EKS_CLUSTER = os.environ.get("DR_EKS_CLUSTER", "dr-korea-petsite")
_NODEGROUP = os.environ.get("DR_EKS_NODEGROUP", "")
_GLOBAL_CLUSTER = os.environ.get("DR_GLOBAL_CLUSTER", "petsite-global")
_SECONDARY_CLUSTER_ARN = os.environ.get("DR_SECONDARY_CLUSTER_ARN", "")


def _boto3():
    """延迟导入 boto3。

    workflow 沙箱不许在模块顶层导入带副作用的东西,而 activity 虽然不受
    沙箱约束,保持一致的写法能避免以后把 activity 误挪进 workflow 模块。
    """
    import boto3  # noqa: PLC0415

    return boto3


# ── ① 取计划正文 ──────────────────────────────────────────────────────────


@activity.defn
async def fetch_plan_body(inp: ActivityInput) -> StepResult:
    """从 S3 取计划正文。

    计划正文不进 workflow 的 input/memo —— 保留期只有 86400s(实测),
    而 payload 上限在本服务端拿不到(describe_namespace 没有 limits 字段)。
    """
    key = f"plans/{inp.plan_ref}.md"
    cmd = f"aws s3 cp s3://{_PLAN_BUCKET or '<DR_PLAN_BUCKET 未设>'}/{key} -"

    if not _PLAN_BUCKET:
        return StepResult(
            step="fetch_plan_body",
            executed=False,
            would_run=[cmd],
            verified=None,
            inconclusive_reason="DR_PLAN_BUCKET 未设置，无法确认计划正文是否存在",
        )

    if inp.dry_run:
        # dry_run 也做一次**只读**的存在性检查 —— 这不改任何东西,
        # 却能在真切换前发现「计划根本不在那儿」。
        try:
            s3 = _boto3().client("s3", region_name=_REGION)
            head = s3.head_object(Bucket=_PLAN_BUCKET, Key=key)
            return StepResult(
                step="fetch_plan_body",
                executed=False,
                would_run=[cmd],
                detail={"size": head["ContentLength"], "etag": head.get("ETag")},
                verified=True,
            )
        except Exception as e:  # noqa: BLE001
            return StepResult(
                step="fetch_plan_body",
                executed=False,
                would_run=[cmd],
                verified=False,
                inconclusive_reason=f"计划正文不可读：{type(e).__name__}: {e}",
            )

    s3 = _boto3().client("s3", region_name=_REGION)
    obj = s3.get_object(Bucket=_PLAN_BUCKET, Key=key)
    body = obj["Body"].read().decode("utf-8")
    return StepResult(
        step="fetch_plan_body",
        executed=True,
        detail={"bytes": len(body), "lines": body.count("\n") + 1},
        verified=True,
    )


# ── ② 拉起 EKS 节点组 ─────────────────────────────────────────────────────


@activity.defn
async def scale_up_nodegroup(inp: ActivityInput) -> StepResult:
    """把守夜灯站点的节点组从 0 拉到 2。

    ⚠️ 核实**不看 update-nodegroup-config 的返回**,而是数真实节点数 ——
    本项目已实测六次「命令成功但没生效」。
    """
    ng = _NODEGROUP or "<DR_EKS_NODEGROUP 未设>"
    cmd = (
        f"aws eks update-nodegroup-config --region {_REGION} "
        f"--cluster-name {_EKS_CLUSTER} --nodegroup-name {ng} "
        f"--scaling-config minSize=2,desiredSize=2,maxSize=3"
    )

    if inp.dry_run:
        # 只读:把当前形态查出来,让人看见「从什么变到什么」。
        detail: dict[str, Any] = {}
        reason = None
        try:
            eks = _boto3().client("eks", region_name=_REGION)
            if not _NODEGROUP:
                names = eks.list_nodegroups(clusterName=_EKS_CLUSTER)["nodegroups"]
                detail["nodegroups_found"] = names
                reason = "DR_EKS_NODEGROUP 未设置，无法确定要拉起哪个节点组"
            else:
                d = eks.describe_nodegroup(
                    clusterName=_EKS_CLUSTER, nodegroupName=_NODEGROUP
                )["nodegroup"]
                detail["current_scaling"] = d["scalingConfig"]
                detail["status"] = d["status"]
        except Exception as e:  # noqa: BLE001
            reason = f"查当前节点组形态失败：{type(e).__name__}: {e}"

        return StepResult(
            step="scale_up_nodegroup",
            executed=False,
            would_run=[cmd],
            detail=detail,
            verified=None if reason else True,
            inconclusive_reason=reason,
        )

    eks = _boto3().client("eks", region_name=_REGION)
    eks.update_nodegroup_config(
        clusterName=_EKS_CLUSTER,
        nodegroupName=_NODEGROUP,
        scalingConfig={"minSize": 2, "desiredSize": 2, "maxSize": 3},
    )
    activity.heartbeat("update_nodegroup_config 已提交，开始等节点真的起来")

    # 核实用与执行不同的手段：数 EC2 实例，而不是看上面那个调用的返回。
    ec2 = _boto3().client("ec2", region_name=_REGION)
    filters = [
        {"Name": "tag:eks:cluster-name", "Values": [_EKS_CLUSTER]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    found = 0
    for _ in range(40):  # 最多约 10 分钟
        activity.heartbeat(f"已 running 的节点数：{found}")
        resp = ec2.describe_instances(Filters=filters)
        found = sum(len(r["Instances"]) for r in resp["Reservations"])
        if found >= 2:
            break
        import asyncio  # noqa: PLC0415

        await asyncio.sleep(15)

    return StepResult(
        step="scale_up_nodegroup",
        executed=True,
        detail={"running_nodes": found},
        # ⚠️ 这里的 verified 只说明「ASG 扩容成功」，**不说明集群获得了
        # 可调度容量**。节点可能起来了却没成为 Ready —— 对灾备切换来说
        # 这个差别极大：你可能有两台 EC2 和零个可调度节点。
        #
        # 2026-09-24 演练时确认了这个局限。为什么暂时没做到：集群
        # endpointPublicAccess=false，从 VPC 外查不到 k8s 节点状态。
        # 正确的修法是让 worker 去查 —— 它就在 VPC 内，能访问私有 endpoint。
        verified=found >= 2,
        inconclusive_reason=(
            None
            if found >= 2
            else f"等待超时，只看到 {found} 个 running 节点"
        ),
        # 把局限如实带进结果，别让读的人把「ASG 扩了」当成「集群能跑活」。
        detail_note=(
            "running_nodes 数的是 EC2 实例，不是 Ready 的 k8s 节点。"
            "两台 EC2 起来了仍可能没有可调度容量。"
        ),
    )


# ── ③ 提升数据库 ──────────────────────────────────────────────────────────


@activity.defn
async def promote_database(inp: ActivityInput) -> StepResult:
    """把韩国从集群提升为可写。

    ⚠️ 两个变体的差别是**会不会丢数据**,所以 decision 必须由上游的
    signal 给,这里不设默认值、不做推断。
    """
    from workflows import DECISION_ALLOW_DATA_LOSS, DECISION_ORDERED  # noqa: PLC0415

    if inp.decision not in (DECISION_ORDERED, DECISION_ALLOW_DATA_LOSS):
        # 不猜。没有合法裁决就失败。
        raise ValueError(
            f"promote_database 需要明确裁决（{DECISION_ORDERED} 或 "
            f"{DECISION_ALLOW_DATA_LOSS}），收到 {inp.decision!r}。"
            "这一步不设默认值：两个变体的差别是会不会丢数据。"
        )

    ordered = inp.decision == DECISION_ORDERED
    base = (
        f"aws rds failover-global-cluster --region {_PRIMARY_REGION} "
        f"--global-cluster-identifier {_GLOBAL_CLUSTER} "
        f"--target-db-cluster-identifier {_SECONDARY_CLUSTER_ARN or '<未设>'}"
    )
    cmd = base if ordered else f"{base} --allow-data-loss"

    if inp.dry_run:
        detail: dict[str, Any] = {"decision": inp.decision, "ordered": ordered}
        reason = None
        try:
            rds = _boto3().client("rds", region_name=_PRIMARY_REGION)
            gc = rds.describe_global_clusters(
                GlobalClusterIdentifier=_GLOBAL_CLUSTER
            )["GlobalClusters"][0]
            detail["members"] = [
                {"arn": m["DBClusterArn"], "is_writer": m["IsWriter"]}
                for m in gc.get("GlobalClusterMembers", [])
            ]
        except Exception as e:  # noqa: BLE001
            reason = f"查全局集群成员失败：{type(e).__name__}: {e}"

        return StepResult(
            step="promote_database",
            executed=False,
            would_run=[cmd],
            detail=detail,
            verified=None if reason else True,
            inconclusive_reason=reason,
        )

    # ⚠️ 真执行路径。当前 worker 角色没有 rds:FailoverGlobalCluster,
    #    走到这里会 AccessDenied —— 刻意的,权限按步骤逐个放开。
    rds = _boto3().client("rds", region_name=_PRIMARY_REGION)
    kwargs: dict[str, Any] = {
        "GlobalClusterIdentifier": _GLOBAL_CLUSTER,
        "TargetDbClusterIdentifier": _SECONDARY_CLUSTER_ARN,
    }
    if not ordered:
        kwargs["AllowDataLoss"] = True
    rds.failover_global_cluster(**kwargs)
    activity.heartbeat("failover_global_cluster 已提交")

    return StepResult(
        step="promote_database",
        executed=True,
        detail={"decision": inp.decision},
        # 核实留给 ④ —— 提升是异步的，这里不假装已经完成。
        verified=None,
        inconclusive_reason="提升是异步的，由 verify_step 独立核实写入端点",
    )


# ── ④ 核实 ────────────────────────────────────────────────────────────────


@activity.defn
async def verify_step(inp: ActivityInput) -> StepResult:
    """独立核实切换结果。

    纪律:**核实必须用与执行不同的手段**。所以这里查的是
    「全局集群里谁是 writer」,而不是重读上一步调用的返回。
    """
    detail: dict[str, Any] = {}
    reason = None
    verified: bool | None = None

    try:
        rds = _boto3().client("rds", region_name=_PRIMARY_REGION)
        gc = rds.describe_global_clusters(
            GlobalClusterIdentifier=_GLOBAL_CLUSTER
        )["GlobalClusters"][0]
        members = [
            {"arn": m["DBClusterArn"], "is_writer": m["IsWriter"]}
            for m in gc.get("GlobalClusterMembers", [])
        ]
        detail["members"] = members
        writers = [m for m in members if m["is_writer"]]

        if inp.dry_run:
            # dry_run 下什么都没改，所以「韩国还不是 writer」是预期的，
            # 不是失败。如实说明，不给一个会被误读的 True/False。
            verified = None
            reason = "dry_run：未执行提升，writer 仍应是主 region —— 这不是失败"
        elif len(writers) != 1:
            verified = None
            reason = f"writer 数量异常（{len(writers)} 个），无法判定切换是否完成"
        else:
            verified = _REGION in writers[0]["arn"]
            if not verified:
                reason = f"writer 仍在 {writers[0]['arn']}，未切到 {_REGION}"
    except Exception as e:  # noqa: BLE001
        verified = None
        reason = f"核实查询失败，无法判断：{type(e).__name__}: {e}"

    return StepResult(
        step="verify_step",
        executed=True,
        detail=detail,
        verified=verified,
        inconclusive_reason=reason,
    )

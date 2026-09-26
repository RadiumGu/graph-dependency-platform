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


def count_asg_by_lifecycle() -> tuple[dict[str, int] | None, str | None]:
    """按 LifecycleState 统计节点组 ASG 里的实例。返回 (计数字典, 无法判断的原因)。

    ## 为什么缩容核实**不能看 EC2 的 State**

    2026-09-24 实测,缩到 desired=0 之后:

        ASG LifecycleState:  Terminating:Wait   ← 真相
        EC2 State:           running            ← 这一层分不出来

    节点组的排空钩子 `Terminate-LC-Hook` 的 `HeartbeatTimeout=1800`
    (30 分钟)、`DefaultResult=CONTINUE`。所以**一台正在优雅排空的实例和
    一台缩容失败卡住的实例,在 EC2 那一层长得完全一样** —— 都是 `running`。
    用 EC2 State 做判据,会把「正常排空中」报成「缩容失败」,
    或者更糟:把「卡住了」报成「还在排空,再等等」。

    ## ASG 名字要现查,不能写死

    名字形如 `eks-<nodegroup>-<uuid>`,那个 uuid 是节点组创建时生成的。
    节点组一旦重建,名字就变。所以从 `describe_nodegroup` 的
    `resources.autoScalingGroups[].name` 读 —— 本轮就因为用了记忆里的名字
    而拿到空结果(实际是 AccessDenied 被 `2>/dev/null` 吞了)。
    """
    try:
        eks = _boto3().client("eks", region_name=_REGION)
        ng = eks.describe_nodegroup(
            clusterName=_EKS_CLUSTER, nodegroupName=_NODEGROUP
        )["nodegroup"]
        groups = (ng.get("resources") or {}).get("autoScalingGroups") or []
        if not groups:
            return None, "describe_nodegroup 没有返回 autoScalingGroups"
        asg_name = groups[0]["name"]
    except Exception as e:  # noqa: BLE001
        return None, f"查节点组的 ASG 名失败：{type(e).__name__}: {e}"

    try:
        asg = _boto3().client("autoscaling", region_name=_REGION)
        resp = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups") or []
        if not groups:
            # 名字对但查不到 → 说不出实例状态，是 inconclusive 而不是「零台」。
            return None, f"ASG {asg_name} 查不到（名字可能已变）"
    except Exception as e:  # noqa: BLE001
        # ⚠️ 返回 None 而不是空字典。空字典会被读成「一台都没有了」，
        # 而实际上我们什么都没测到。本项目已四次犯这类错。
        return None, f"查 ASG 生命周期失败：{type(e).__name__}: {e}"

    counts: dict[str, int] = {}
    for inst in groups[0].get("Instances") or []:
        state = inst.get("LifecycleState", "Unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts, None


def _boto3():
    """延迟导入 boto3。

    workflow 沙箱不许在模块顶层导入带副作用的东西,而 activity 虽然不受
    沙箱约束,保持一致的写法能避免以后把 activity 误挪进 workflow 模块。
    """
    import boto3  # noqa: PLC0415

    return boto3


def count_ready_nodes() -> tuple[int | None, str | None]:
    """数集群里 Ready 的 k8s 节点。返回 (数量, 无法判断的原因)。

    ## 为什么需要这个

    2026-09-24 的扩容演练暴露了一个核实缺陷:原来的核实是**数 EC2 实例**。
    那证明 ASG 扩容成功,**不证明集群获得了可调度容量** —— 节点可能起来了
    却没成为 Ready。对灾备切换来说差别极大:你可能有两台 EC2 和零个可调度
    节点,而切换会报「verified=True」继续往下走。

    ## 怎么做到的(都是实测确认的,不是推测)

    集群 `endpointPublicAccess=false`,所以只有 VPC 内的 worker 能查。
    三个前置条件缺一不可:

    1. **访问条目**:`AuthenticationMode=API` 时调 k8s API 要有条目。
       实例角色原本不在条目里。
    2. **控制面 443 入站**:集群安全组原本只放行来自自己的流量,
       实测表现是 `curl` 超时(`timed out` 而非 `refused`,安全组静默丢包)。
    3. **RBAC**:`AmazonEKSViewPolicy` 实测 **403** —— 官方文档的资源表里
       **没有 nodes**;而 `AmazonEKSAdminViewPolicy` 是 `*/*`,文档明确写着
       「包括 Kubernetes Secrets」。所以自定义了一个只含 nodes 的 ClusterRole,
       绑到 `dr-node-readers` 组。实测:列节点 200、读 Secret 403。

    ## 不引入 kubernetes 客户端库

    `aws eks get-token` 出 bearer token + `urllib` 直连即可,少一个依赖。
    TLS 用集群自己的 CA 校验,不跳过校验。
    """
    import json as _json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    import ssl  # noqa: PLC0415
    import base64 as _b64  # noqa: PLC0415

    try:
        eks = _boto3().client("eks", region_name=_REGION)
        c = eks.describe_cluster(name=_EKS_CLUSTER)["cluster"]
        endpoint = c["endpoint"]
        ca_pem = _b64.b64decode(c["certificateAuthority"]["data"])
    except Exception as e:  # noqa: BLE001
        return None, f"查集群 endpoint/CA 失败：{type(e).__name__}: {e}"

    try:
        # aws eks get-token 是它的正规用途：把 SigV4 预签名 URL 包成
        # k8s-aws-v1.<base64url> 形式的 bearer token。自己实现容易出错。
        out = subprocess.run(
            ["aws", "eks", "get-token", "--region", _REGION,
             "--cluster-name", _EKS_CLUSTER, "--output", "json"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        token = _json.loads(out.stdout)["status"]["token"]
    except Exception as e:  # noqa: BLE001
        return None, f"取 k8s token 失败：{type(e).__name__}: {e}"

    try:
        with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as f:
            f.write(ca_pem)
            ca_path = f.name
        ctx = ssl.create_default_context(cafile=ca_path)
        req = urllib.request.Request(
            f"{endpoint}/api/v1/nodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            body = _json.loads(resp.read())
    except Exception as e:  # noqa: BLE001
        # ⚠️ 返回 None 而不是 0。查不到不等于「没有 Ready 节点」——
        # 把「没测到」写成「测到 0 个」是本项目已犯四次的缺陷。
        return None, f"查 k8s 节点失败：{type(e).__name__}: {e}"

    items = body.get("items")
    if items is None:
        return None, f"k8s 返回体没有 items 字段（kind={body.get('kind')}）"

    ready = 0
    for node in items:
        conds = (node.get("status") or {}).get("conditions") or []
        # Ready 条件的 status 是字符串 "True"/"False"/"Unknown"，不是布尔。
        # "Unknown" 表示 kubelet 失联 —— 那种节点不该算进可调度容量。
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
            ready += 1
    return ready, None


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


# ── ①b 写入一版计划正文（计划评审生命周期用） ──────────────────────────────


@dataclass
class PlanVersionInput:
    plan_id: str
    version: int
    body: str
    #: 由 workflow 侧算好传进来 —— 让写入方与审计链用**同一个**摘要，
    #: 而不是各算一次（各算一次时两边不一致就无从判断谁对）。
    sha256: str


@activity.defn
async def put_plan_version(inp: PlanVersionInput) -> StepResult:
    """把一版计划正文写成**不可覆盖**的 S3 对象。

    ## 为什么必须拒绝覆盖

    键是 `plans/<plan_id>/v<N>.md`，与 `fetch_plan_body` 的 `plans/<ref>.md`
    规则一致（`plan_ref = "<plan_id>/v<N>"`），所以执行路径不用改。

    但**允许覆盖会直接毁掉审计**：审计链里记的是「v2 的 sha256 是 X」，
    如果 v2 这个键能被改写，那么"被批准并演练过的 v2"和"实际被执行的 v2"
    可以是两份不同的文件，而 history 里看不出任何异样。所以这里用
    `IfNoneMatch="*"` 让服务端做条件写，冲突时**响亮失败**。

    幂等性靠摘要而不是靠"写了就算"：键已存在且 sha256 相同 -> 视为同一次
    写入已完成（activity 重试是正常的）；相同键但内容不同 -> 拒绝。
    """
    key = f"plans/{inp.plan_id}/v{inp.version}.md"
    cmd = (
        f"aws s3api put-object --bucket {_PLAN_BUCKET or '<DR_PLAN_BUCKET 未设>'} "
        f"--key {key} --if-none-match '*'"
    )

    if not _PLAN_BUCKET:
        # 没有桶时**不能**静默成功：计划评审链会以为正文已落盘，
        # 而真执行时 fetch_plan_body 会取不到。
        raise RuntimeError(
            "DR_PLAN_BUCKET 未设置，无法写入计划正文。"
            "这一步静默跳过的后果是：评审链显示计划已存在，而真切换时取不到正文。"
        )

    s3 = _boto3().client("s3", region_name=_REGION)
    data = inp.body.encode("utf-8")

    try:
        s3.put_object(
            Bucket=_PLAN_BUCKET,
            Key=key,
            Body=data,
            ContentType="text/markdown; charset=utf-8",
            # 条件写：键已存在即失败。S3 在 2024-11 起支持这个头。
            IfNoneMatch="*",
            Metadata={"sha256": inp.sha256, "plan-id": inp.plan_id,
                      "version": str(inp.version)},
        )
        return StepResult(
            step="put_plan_version",
            executed=True,
            would_run=[cmd],
            detail={"s3_key": key, "bytes": len(data), "sha256": inp.sha256},
            verified=True,
            detail_note="条件写成功，说明这个版本号此前不存在（不是覆盖）。",
        )
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise

        # 键已存在。区分两件事：activity 重试（同内容）与真冲突（不同内容）。
        head = s3.head_object(Bucket=_PLAN_BUCKET, Key=key)
        existing = (head.get("Metadata") or {}).get("sha256", "")
        if existing == inp.sha256:
            return StepResult(
                step="put_plan_version",
                executed=False,
                would_run=[cmd],
                detail={"s3_key": key, "bytes": len(data), "sha256": inp.sha256},
                verified=True,
                detail_note=(
                    "该版本已存在且摘要一致 —— 判为本次写入的重试，不是冲突。"
                ),
            )
        raise RuntimeError(
            f"{key} 已存在且内容不同（已有 sha256={existing[:12]}…，"
            f"本次 {inp.sha256[:12]}…）。计划版本是不可覆盖的："
            "允许覆盖意味着「被批准并演练过的那一版」与「真执行时取到的那一版」"
            f"可以是两份不同的文件，而审计链看不出异样。原始错误：{name}"
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

    import asyncio  # noqa: PLC0415

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
        await asyncio.sleep(15)

    # ── 核实升级：数 Ready 的 k8s 节点，不只数 EC2 ────────────────────
    #
    # 2026-09-24 演练时原来的判据是数 EC2 —— 那只证明 ASG 扩容成功，
    # **不证明集群获得了可调度容量**。对切换来说差别极大：你可能有两台
    # EC2 和零个可调度节点，而步骤会报 verified=True 继续往下走。
    ready, ready_err = count_ready_nodes()
    for _ in range(20):
        if ready is not None and ready >= 2:
            break
        activity.heartbeat(f"Ready 的 k8s 节点数：{ready}")
        await asyncio.sleep(15)
        ready, ready_err = count_ready_nodes()

    return StepResult(
        step="scale_up_nodegroup",
        executed=True,
        detail={"running_nodes": found, "ready_k8s_nodes": ready},
        # ⚠️ ready 为 None 表示**查不到**（不是「没有 Ready 节点」），
        # 此时 verified 也必须是 None —— 未测量不能写成测量值。
        verified=(None if ready is None else ready >= 2),
        inconclusive_reason=(
            ready_err
            if ready is None
            else (
                None
                if ready >= 2
                else f"EC2 起了 {found} 台，但只有 {ready} 个 k8s 节点 Ready"
            )
        ),
        detail_note=(
            "running_nodes 数的是 EC2 实例，ready_k8s_nodes 数的是 Ready 的 "
            "k8s 节点。前者只证明 ASG 扩了，后者才证明集群真有可调度容量。"
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
    # 与真实调用保持一致:两边都显式带参数。
    # 早先这里是 `base if ordered else …`(有序分支不带任何标志),
    # 那会让 dry_run 打印的命令和真执行路径**不是同一条命令** ——
    # 而 would_run 的全部价值就在于「照着它跑能复现」。
    cmd = f"{base} --switchover" if ordered else f"{base} --allow-data-loss"

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

    # ⚠️ 真执行路径。
    rds = _boto3().client("rds", region_name=_PRIMARY_REGION)
    kwargs: dict[str, Any] = {
        "GlobalClusterIdentifier": _GLOBAL_CLUSTER,
        "TargetDbClusterIdentifier": _SECONDARY_CLUSTER_ARN,
    }
    # ## 为什么两个分支都**显式**传参
    #
    # API 文档写着「If you don't specify AllowDataLoss, the global database
    # cluster operation defaults to a switchover」—— 所以省略两个参数也能得到
    # 有序切换。但这一步的整个设计前提是「有序 vs 丢数据」必须是一个
    # **明确的裁决**,而依赖一个 API 默认值恰好违背这一点:
    # 上游若哪天改了默认行为,这里会静默地变成另一种语义。
    #
    # AllowDataLoss 与 Switchover 是**互斥**的(API 约束:
    # "Can't be specified together with the Switchover parameter"),
    # 所以正好每个分支各传一个。
    #
    # 显式传参还有一个好处:CloudTrail 里能直接看出当时是哪种语义,
    # 而不是「什么都没传,所以大概是 switchover」。
    if ordered:
        kwargs["Switchover"] = True
    else:
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

"""
activities.py — 计划存取与共享的只读辅助函数。

## 2026-10-05：变更类 activity 已退役

这里原来有 `scale_up_nodegroup` / `promote_database` / `verify_step` /
`fetch_plan_body` 四个切换步骤 activity，由 `DrFailoverWorkflow` 的四个子
workflow 调用。它们已随那套编排一起移除，原因是模型变了：

    **人执行每个变更，Temporal 只负责核实。**

那四个 activity 从来没真执行过（实例角色只有 Describe 权限，`dry_run=False`
会 AccessDenied —— 那是刻意的），所以移除不损失任何线上能力；
它们承担的「核实」职责搬进了 `probes.py`，每个探针一个独立 activity。

留在这里的：
  · `put_plan_version` —— 计划版本的不可覆盖写入（评审链的供给侧）
  · `load_plan_version` —— 按版本取回正文，并**重新计算摘要**核对
  · `put_execution_record` —— 把一次演练/执行的完整记录导出到 S3
  · `count_ready_nodes` / `count_asg_by_lifecycle` —— 探针复用的只读辅助

## 为什么执行记录要导出

命名空间保留期 720h（30 天，2026-10-05 实测），归档刻意未开 —— 一次真实切换的
history 30 天后就会被删掉，而灾备审计要的远不止 30 天。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity


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

    键是 `plans/<plan_id>/v<N>.md`，与 `load_plan_version` 读的是同一个键。

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
        # 而演练/执行时 load_plan_version 会取不到。
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


# ── ①c 按版本取回正文（演练/执行用） ─────────────────────────────────────


@dataclass
class LoadPlanInput:
    plan_id: str
    version: int
    #: 评审链里记下的那一版的摘要。
    sha256: str


@dataclass
class LoadedPlan:
    plan_id: str
    version: int
    sha256: str
    body: str
    s3_key: str


@activity.defn
async def load_plan_version(inp: LoadPlanInput) -> LoadedPlan:
    """取回 `plans/<plan_id>/v<N>.md`，并**按内容重新计算**摘要核对。

    只看对象元数据里的 sha256 不够：元数据是写入方自报的，一个被覆盖的对象
    完全可以带着旧元数据。所以这里对取回的**字节**重新算一遍 ——
    「被批准并演练过的 v2」与「此刻要执行的 v2」必须是同一份内容。

    不一致时抛不可重试的错误：重试一万次也不会让内容变对。
    """
    import hashlib  # noqa: PLC0415

    from temporalio.exceptions import ApplicationError  # noqa: PLC0415

    if not _PLAN_BUCKET:
        raise ApplicationError("DR_PLAN_BUCKET 未设置，取不到计划正文", non_retryable=True)
    key = f"plans/{inp.plan_id}/v{inp.version}.md"
    obj = _boto3().client("s3", region_name=_REGION).get_object(Bucket=_PLAN_BUCKET, Key=key)
    data = obj["Body"].read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != inp.sha256:
        raise ApplicationError(
            f"{key} 的内容摘要 {digest[:12]}… 与评审链记录的 {inp.sha256[:12]}… 不一致。"
            "被批准的那一版与此刻取到的不是同一份文件 —— 拒绝据此演练或执行。",
            non_retryable=True,
        )
    return LoadedPlan(
        plan_id=inp.plan_id, version=inp.version, sha256=digest,
        body=data.decode("utf-8"), s3_key=key,
    )


# ── ①d 导出执行记录 ───────────────────────────────────────────────────────


@dataclass
class ExecutionRecordInput:
    plan_id: str
    #: 一次运行的唯一名（workflow id + run id 前缀）。
    record_id: str
    record: dict[str, Any]


@activity.defn
async def put_execution_record(inp: ExecutionRecordInput) -> StepResult:
    """把一次演练/执行的完整记录写到 `plans/<plan_id>/executions/<record_id>.json`。

    放在 `plans/` 前缀下是刻意的：实例角色的 `dr-plan-write` 只覆盖 `plans/*`，
    不需要为此新开一条写权限。同样用条件写，记录一旦写下就不可覆盖。
    """
    import json  # noqa: PLC0415

    if not _PLAN_BUCKET:
        raise RuntimeError("DR_PLAN_BUCKET 未设置，执行记录无处可写")
    key = f"plans/{inp.plan_id}/executions/{inp.record_id}.json"
    data = json.dumps(inp.record, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    s3 = _boto3().client("s3", region_name=_REGION)
    try:
        s3.put_object(
            Bucket=_PLAN_BUCKET, Key=key, Body=data,
            ContentType="application/json; charset=utf-8", IfNoneMatch="*",
        )
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise
        # 已存在：activity 重试时会走到这里。同一个 record_id 只对应一次运行。
        return StepResult(
            step="put_execution_record", executed=False, verified=True,
            detail={"s3_key": key}, detail_note="记录已存在（判为本次写入的重试）",
        )
    return StepResult(
        step="put_execution_record", executed=True, verified=True,
        detail={"s3_key": key, "bytes": len(data)},
    )

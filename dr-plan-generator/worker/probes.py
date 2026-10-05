"""probes.py — 探针实现。每个探针是一个独立的 activity，注册名 `probe.<name>`。

目录（问题、手段、参数、权限）在 `probe_catalog.py`；本文件只写**怎么测**。

## 三态的边界在这里落实

每个探针内部只做一件事：把一次读结果判成 PASS / FAIL / UNKNOWN。
**FAIL 必须是测到了一个不合格的值**；以下情况一律 UNKNOWN：

- 调用抛异常（AccessDenied、超时、连不上）
- 返回体里缺了要读的字段
- CloudWatch 没有数据点（没有数据 ≠ 延迟为 0）

唯一的例外是「资源确定不存在」（`ImageNotFoundException` 之类）：那是服务端
**明确回答了「没有」**，是一个测到的事实，判 FAIL。这条区分不能抹掉 ——
把「没权限查」写成「镜像不在」，人会去重推镜像，而真正缺的是一条 IAM。

## 探针自己不抛异常

外层 `_run` 把一切异常收成 UNKNOWN 并写明原因。理由是：探针在 workflow 里
作为闸门调用时，抛异常会触发 activity 重试，重试耗尽后 workflow 收到的是
一个 ActivityError —— 「探针坏了」与「被测对象不合格」于是长得一样。
收成 UNKNOWN 后两者在结果里是分开的。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from temporalio import activity

from activities import _boto3, count_asg_by_lifecycle, count_ready_nodes
from probe_catalog import FAIL, PASS, PROBES, UNKNOWN, activity_name, check_params, resolved_params

_REGION = os.environ.get("DR_TARGET_REGION", "ap-northeast-2")
_EKS_CLUSTER = os.environ.get("DR_EKS_CLUSTER", "dr-korea-petsite")
_NODEGROUP = os.environ.get("DR_EKS_NODEGROUP", "")
_GLOBAL_CLUSTER = os.environ.get("DR_GLOBAL_CLUSTER", "petsite-global")
_SECONDARY_CLUSTER_ARN = os.environ.get("DR_SECONDARY_CLUSTER_ARN", "")


@dataclass
class ProbeInput:
    params: dict[str, Any] = field(default_factory=dict)
    #: 这次探测属于哪份计划的哪一版（`<plan_id>/v<N>`），临时探测填 adhoc。
    plan_ref: str = "adhoc"


@dataclass
class ProbeResult:
    probe: str
    verdict: str
    question: str
    means: str
    #: 测到的值。UNKNOWN 时可能为空。
    observed: dict[str, Any] = field(default_factory=dict)
    #: 期望的值 —— 与 observed 并排写，读的人不用去翻目录。
    expected: dict[str, Any] = field(default_factory=dict)
    #: FAIL / UNKNOWN 时的原因；PASS 时可写测量上的局限。
    reason: str = ""
    region: str = _REGION
    measured_at: str = ""
    params: dict[str, Any] = field(default_factory=dict)


Outcome = tuple[str, dict[str, Any], dict[str, Any], str]


def _err_code(e: Exception) -> str:
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")


def _run(name: str, inp: ProbeInput, fn: Callable[[dict[str, Any]], Outcome]) -> ProbeResult:
    spec = PROBES[name]
    base = dict(
        probe=name,
        question=spec.question,
        means=spec.means,
        region=_REGION,
        measured_at=datetime.now(timezone.utc).isoformat(),
        params=dict(inp.params),
    )
    bad = check_params(name, inp.params)
    if bad:
        # 参数错了就没测 —— UNKNOWN，不是 FAIL。
        return ProbeResult(verdict=UNKNOWN, reason="参数不合法：" + "；".join(bad), **base)
    try:
        verdict, observed, expected, reason = fn(resolved_params(name, inp.params))
    except Exception as e:  # noqa: BLE001 —— 一切异常都收成 UNKNOWN，见文件头
        code = _err_code(e)
        hint = ""
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
            hint = f"（实例角色缺权限：{', '.join(spec.permissions) or '未列出'}）"
        return ProbeResult(
            verdict=UNKNOWN,
            reason=f"{type(e).__name__}{f'[{code}]' if code else ''}: {e}{hint}",
            **base,
        )
    return ProbeResult(verdict=verdict, observed=observed, expected=expected, reason=reason, **base)


def _client(service: str):
    return _boto3().client(service, region_name=_REGION)


# ── 数据层 ────────────────────────────────────────────────────────────────


def _global_cluster() -> dict[str, Any]:
    resp = _client("rds").describe_global_clusters(GlobalClusterIdentifier=_GLOBAL_CLUSTER)
    clusters = resp.get("GlobalClusters") or []
    if not clusters:
        raise LookupError(f"describe_global_clusters 没有返回 {_GLOBAL_CLUSTER}")
    return clusters[0]


def _members(gc: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "cluster": m.get("DBClusterArn", "").rsplit(":", 1)[-1],
            "region": m.get("DBClusterArn", "::::").split(":")[3],
            "writer": bool(m.get("IsWriter")),
            "arn": m.get("DBClusterArn", ""),
        }
        for m in gc.get("GlobalClusterMembers") or []
    ]


def _aurora_global_membership(p: dict[str, Any]) -> Outcome:
    if not _SECONDARY_CLUSTER_ARN:
        return UNKNOWN, {}, {}, "DR_SECONDARY_CLUSTER_ARN 未设置，不知道该找哪个成员"
    gc = _global_cluster()
    members = _members(gc)
    status = gc.get("Status")
    observed = {
        "status": status,
        "members": [{k: v for k, v in m.items() if k != "arn"} for m in members],
    }
    expected = {"status": "available", "member": _SECONDARY_CLUSTER_ARN.rsplit(":", 1)[-1]}
    if not any(m["arn"] == _SECONDARY_CLUSTER_ARN for m in members):
        return FAIL, observed, expected, "首尔从集群不在全局集群的成员列表里"
    if status != "available":
        return FAIL, observed, expected, f"全局集群状态是 {status!r}"
    return PASS, observed, expected, ""


def _aurora_writer_region(p: dict[str, Any]) -> Outcome:
    members = _members(_global_cluster())
    writers = [m for m in members if m["writer"]]
    observed = {"writers": [m["region"] for m in writers]}
    expected = {"writer_region": p["expect_region"]}
    if not writers:
        # 切换中途会有一段没有写节点的窗口 —— 那是测到的状态，判 FAIL。
        return FAIL, observed, expected, "全局集群当前没有写节点（可能正处在切换中途）"
    if len(writers) > 1:
        return FAIL, observed, expected, "出现了多个写节点"
    if writers[0]["region"] != p["expect_region"]:
        return FAIL, observed, expected, f"写节点在 {writers[0]['region']}"
    return PASS, observed, expected, "这是控制面视角；数据面是否真可写要另用 pg_is_in_recovery() 核实"


def _aurora_replication_lag(p: dict[str, Any]) -> Outcome:
    if not _SECONDARY_CLUSTER_ARN:
        return UNKNOWN, {}, {}, "DR_SECONDARY_CLUSTER_ARN 未设置"
    cluster_id = _SECONDARY_CLUSTER_ARN.rsplit(":", 1)[-1]
    end = datetime.now(timezone.utc)
    resp = _client("cloudwatch").get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="AuroraGlobalDBReplicationLag",
        Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
        StartTime=end - timedelta(minutes=p["window_minutes"]),
        EndTime=end,
        Period=60,
        Statistics=["Maximum"],
    )
    points = resp.get("Datapoints") or []
    expected = {"max_lag_ms": p["max_lag_ms"]}
    if not points:
        return UNKNOWN, {"datapoints": 0}, expected, (
            "没有数据点 —— 不等于延迟为 0。复制停了、或者指标还没发布，都长这样"
        )
    worst = max(pt["Maximum"] for pt in points)
    observed = {"max_lag_ms": worst, "datapoints": len(points)}
    if worst > p["max_lag_ms"]:
        return FAIL, observed, expected, f"最大复制延迟 {worst:.0f} ms"
    return PASS, observed, expected, ""


def _dynamodb_table_active(p: dict[str, Any]) -> Outcome:
    t = _client("dynamodb").describe_table(TableName=p["table"])["Table"]
    observed = {
        "status": t.get("TableStatus"),
        "replicas": [
            {"region": r.get("RegionName"), "status": r.get("ReplicaStatus")}
            for r in t.get("Replicas") or []
        ],
    }
    expected = {"status": "ACTIVE"}
    if t.get("TableStatus") != "ACTIVE":
        return FAIL, observed, expected, f"首尔副本状态是 {t.get('TableStatus')!r}"
    return PASS, observed, expected, "全局表是双活的 —— 没有「提升」这一步，ACTIVE 即可写"


# ── 制品 ──────────────────────────────────────────────────────────────────


def _ecr_image_present(p: dict[str, Any]) -> Outcome:
    expected = {"repository": p["repository"], "digest": p["digest"]}
    try:
        resp = _client("ecr").describe_images(
            repositoryName=p["repository"], imageIds=[{"imageDigest": p["digest"]}]
        )
    except Exception as e:  # noqa: BLE001
        if _err_code(e) in ("ImageNotFoundException", "RepositoryNotFoundException"):
            # 服务端明确回答「没有」—— 测到的事实，判 FAIL（见文件头的例外说明）。
            return FAIL, {"found": False, "error": _err_code(e)}, expected, (
                "首尔没有这个镜像 —— 跨区复制不追溯存量，需要 put-image 触发一次"
            )
        raise
    details = resp.get("imageDetails") or []
    if not details:
        return FAIL, {"found": False}, expected, "describe_images 返回空"
    d = details[0]
    observed = {
        "found": True,
        "tags": d.get("imageTags") or [],
        "pushed_at": str(d.get("imagePushedAt", "")),
    }
    return PASS, observed, expected, ""


def _ecr_tag_present(p: dict[str, Any]) -> Outcome:
    expected = {"repository": p["repository"], "tag": p["tag"]}
    try:
        resp = _client("ecr").describe_images(
            repositoryName=p["repository"], imageIds=[{"imageTag": p["tag"]}]
        )
    except Exception as e:  # noqa: BLE001
        if _err_code(e) in ("ImageNotFoundException", "RepositoryNotFoundException"):
            return FAIL, {"found": False, "error": _err_code(e)}, expected, "首尔没有这个标签"
        raise
    details = resp.get("imageDetails") or []
    if not details:
        return FAIL, {"found": False}, expected, "describe_images 返回空"
    observed = {"found": True, "digest": details[0].get("imageDigest")}
    # 标签是可变的：这里只能证明「此刻有一个叫这个标签的镜像」，
    # 不能证明它是你以为的那个内容。PASS 也要把这个局限写出来。
    return PASS, observed, expected, "标签可变 —— 只证明存在，不证明内容；能钉 digest 就钉 digest"


def _lambda_function_active(p: dict[str, Any]) -> Outcome:
    expected = {"state": "Active", "last_update_status": "Successful"}
    try:
        c = _client("lambda").get_function_configuration(FunctionName=p["function"])
    except Exception as e:  # noqa: BLE001
        if _err_code(e) == "ResourceNotFoundException":
            return FAIL, {"found": False}, expected, "首尔没有这个函数"
        raise
    observed = {
        "state": c.get("State"),
        "last_update_status": c.get("LastUpdateStatus"),
        "code_sha256": c.get("CodeSha256"),
    }
    if c.get("State") != "Active" or c.get("LastUpdateStatus") != "Successful":
        return FAIL, observed, expected, "函数不是 Active，或最近一次更新没有成功"
    return PASS, observed, expected, "只证明函数可调用；不证明代码是哪一版（见 code_sha256）"


# ── 计算 ──────────────────────────────────────────────────────────────────


def _eks_nodegroup_status(p: dict[str, Any]) -> Outcome:
    if not _NODEGROUP:
        return UNKNOWN, {}, {}, "DR_EKS_NODEGROUP 未设置"
    ng = _client("eks").describe_nodegroup(
        clusterName=_EKS_CLUSTER, nodegroupName=_NODEGROUP
    )["nodegroup"]
    sc = ng.get("scalingConfig") or {}
    observed = {"status": ng.get("status"), "scaling": sc}
    expected: dict[str, Any] = {"status": "ACTIVE"}
    if p.get("expect_desired") is not None:
        expected["desiredSize"] = p["expect_desired"]
    if ng.get("status") != "ACTIVE":
        return FAIL, observed, expected, f"节点组状态是 {ng.get('status')!r}"
    if p.get("expect_desired") is not None and sc.get("desiredSize") != p["expect_desired"]:
        return FAIL, observed, expected, f"desiredSize 是 {sc.get('desiredSize')}"
    return PASS, observed, expected, ""


def _eks_ready_nodes(p: dict[str, Any]) -> Outcome:
    ready, why = count_ready_nodes()
    expected = {"min_ready": p["min_ready"]}
    if ready is None:
        return UNKNOWN, {}, expected, why or "查不到 Ready 节点数"
    observed = {"ready": ready}
    if ready < p["min_ready"]:
        return FAIL, observed, expected, f"只有 {ready} 个 Ready 节点"
    return PASS, observed, expected, ""


def _asg_lifecycle_settled(p: dict[str, Any]) -> Outcome:
    counts, why = count_asg_by_lifecycle()
    expected = {"InService": p["expect_in_service"], "others": 0}
    if counts is None:
        return UNKNOWN, {}, expected, why or "查不到 ASG 生命周期"
    observed = {"lifecycle": counts}
    in_service = counts.get("InService", 0)
    others = {k: v for k, v in counts.items() if k != "InService"}
    if in_service != p["expect_in_service"] or others:
        return FAIL, observed, expected, (
            "ASG 还没稳定（注意：排空中的实例 EC2 State 仍是 running，只有 "
            "LifecycleState 分得出来）"
        )
    return PASS, observed, expected, ""


# ── 流量 ──────────────────────────────────────────────────────────────────


def _alb_target_health(p: dict[str, Any]) -> Outcome:
    elb = _client("elbv2")
    expected = {"min_healthy": p["min_healthy"]}
    try:
        tg = elb.describe_target_groups(Names=[p["target_group"]])["TargetGroups"][0]
    except Exception as e:  # noqa: BLE001
        if _err_code(e) == "TargetGroupNotFound":
            return FAIL, {"found": False}, expected, "目标组不存在"
        raise
    descs = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])[
        "TargetHealthDescriptions"
    ]
    states: dict[str, int] = {}
    for d in descs:
        s = d["TargetHealth"]["State"]
        states[s] = states.get(s, 0) + 1
    healthy = states.get("healthy", 0)
    observed = {"healthy": healthy, "states": states, "health_check_path": tg.get("HealthCheckPath")}
    if healthy < p["min_healthy"]:
        return FAIL, observed, expected, (
            f"只有 {healthy} 个 healthy。注意 ALB 在全部不健康时会 fail-open 继续转发 —— "
            "能访问不等于健康"
        )
    return PASS, observed, expected, ""


def _http_get(p: dict[str, Any]) -> Outcome:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    expected: dict[str, Any] = {"status": p["expect_status"]}
    if p.get("expect_body_contains"):
        expected["body_contains"] = p["expect_body_contains"]
    try:
        with urllib.request.urlopen(p["url"], timeout=10) as r:  # noqa: S310 —— URL 来自已批准的计划
            status, body = r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 服务端给了一个状态码 —— 测到了，按状态码判。
        status, body = e.code, ""
    except Exception as e:  # noqa: BLE001
        # 连不上：分不清「挂了」和「不可达」—— 这正是本项目的核心立场。UNKNOWN。
        return UNKNOWN, {}, expected, (
            f"{type(e).__name__}: {e} —— 连不上时分不清对端是挂了还是网络不可达"
        )
    observed = {"status": status, "body_bytes": len(body)}
    if status != p["expect_status"]:
        return FAIL, observed, expected, f"返回 {status}"
    if p.get("expect_body_contains") and p["expect_body_contains"] not in body:
        return FAIL, observed, expected, "正文里没有期望的片段（注意 302 跟随后的 200 也会走到这里）"
    return PASS, observed, expected, ""


# ── 注册 ──────────────────────────────────────────────────────────────────
#
# 每个探针一个独立的 activity 定义。刻意不做成「一个 dispatcher 按名字分发」：
# 独立的 activity 类型意味着 UI / list_activities 里按类型就能筛出
# 「所有 aurora_writer_region 的历次结论」，dispatcher 会把它们混成一种。

_IMPL: dict[str, Callable[[dict[str, Any]], Outcome]] = {
    "aurora_global_membership": _aurora_global_membership,
    "aurora_writer_region": _aurora_writer_region,
    "aurora_replication_lag": _aurora_replication_lag,
    "dynamodb_table_active": _dynamodb_table_active,
    "ecr_image_present": _ecr_image_present,
    "ecr_tag_present": _ecr_tag_present,
    "lambda_function_active": _lambda_function_active,
    "eks_nodegroup_status": _eks_nodegroup_status,
    "eks_ready_nodes": _eks_ready_nodes,
    "asg_lifecycle_settled": _asg_lifecycle_settled,
    "alb_target_health": _alb_target_health,
    "http_get": _http_get,
}


def _make(name: str):
    impl = _IMPL[name]

    @activity.defn(name=activity_name(name))
    async def _probe(inp: ProbeInput) -> ProbeResult:
        return _run(name, inp, impl)

    _probe.__name__ = f"probe_{name}"
    _probe.__qualname__ = _probe.__name__
    return _probe


ALL_PROBES = [_make(n) for n in PROBES]

# 目录与实现必须一一对应 —— 在导入时就响亮失败，而不是等某次切换跑到那一步。
_missing = set(PROBES) ^ set(_IMPL)
if _missing:
    raise RuntimeError(f"probe_catalog 与 probes._IMPL 不一致：{sorted(_missing)}")

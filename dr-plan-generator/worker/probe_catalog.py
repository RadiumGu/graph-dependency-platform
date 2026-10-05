"""probe_catalog.py — 探针目录。**纯数据，不导入 boto3 / temporalio。**

## 为什么目录与实现分开

计划正文里的 `dr-runbook` 块会引用探针名与参数。`revise_plan` / `approve_plan`
的 validator 要在**进入 history 之前**校验这些引用 —— 而 validator 跑在
workflow 沙箱里，不能导入 boto3。所以目录必须是纯 Python 数据；实现在
`probes.py`，由 `test_128` 守着两边一一对应。

## 每个探针都守四条纪律（这是它们存在的理由）

1. **三态**：PASS / FAIL / UNKNOWN。查不到一律 UNKNOWN，**永远不当成 PASS**。
   FAIL 必须是**测到了**一个不合格的值；「连不上」「没权限」「没数据点」都是 UNKNOWN。
2. **只读、幂等、一个探针只回答一个问题。** 所以重试策略可以放开。
3. **与执行不同的手段核实。** 人执行切换命令，探针从控制面/数据面**另外读**终态，
   不复用那条命令的返回值 —— 本项目已实测六次「命令成功但没生效」。
4. **只测目标区（首尔）。** 真出灾难时东京可能不可达，依赖东京的探针在最需要它的
   时候恰好给不出答案。

## 每个探针是一个独立的 activity

注册名是 `probe.<name>`。同一份实现有两种调用方式：

- **standalone activity**（`client.start_activity`）：人在任何时候临时问一句。
  自带 id 与 search attributes，可单独查、单独重跑，不污染计划的执行历史。
- **workflow 内的 activity**：`DrRunbookWorkflow` 的前置/后置闸门。结果写进
  那次执行的 history —— 这才能证明「第 N 步是在核实通过之后才推进的」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 探针结论的三个取值。
PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
VERDICTS = frozenset({PASS, FAIL, UNKNOWN})

#: activity 注册名前缀。
ACTIVITY_PREFIX = "probe."


@dataclass(frozen=True)
class Param:
    #: "str" / "int"。刻意不支持更多类型 —— 计划正文是人要读、要改的东西。
    type: str
    required: bool = False
    default: object = None
    doc: str = ""


@dataclass(frozen=True)
class ProbeSpec:
    #: 这个探针回答的那**一个**问题。
    question: str
    #: 它用什么手段测 —— 审计时要能看出「核实手段与执行手段不同」。
    means: str
    params: dict[str, Param] = field(default_factory=dict)
    #: 实例角色需要的权限。缺了它，探针会如实返回 UNKNOWN 而不是 FAIL。
    permissions: tuple[str, ...] = ()
    #: 单次执行的超时（秒）。
    timeout_seconds: int = 60


PROBES: dict[str, ProbeSpec] = {
    # ── 数据层 ────────────────────────────────────────────────────────────
    "aurora_global_membership": ProbeSpec(
        question="首尔从集群是否仍是 Aurora 全局集群的成员，且全局集群状态 available？",
        means="rds:DescribeGlobalClusters（首尔端点）",
        permissions=("rds:DescribeGlobalClusters",),
    ),
    "aurora_writer_region": ProbeSpec(
        question="Aurora 全局集群的写节点现在在哪个区？",
        means="rds:DescribeGlobalClusters 的 GlobalClusterMembers[].IsWriter",
        params={
            "expect_region": Param(
                "str", required=True,
                doc="期望的写节点区。切换前填 ap-northeast-1，提升后填 ap-northeast-2。",
            ),
        },
        permissions=("rds:DescribeGlobalClusters",),
    ),
    "aurora_replication_lag": ProbeSpec(
        question="首尔从集群的全局复制延迟是否在阈值内？",
        means="CloudWatch AWS/RDS AuroraGlobalDBReplicationLag（首尔）",
        params={
            "max_lag_ms": Param("int", default=5000, doc="可接受的最大延迟（毫秒）"),
            "window_minutes": Param("int", default=5, doc="取最近多少分钟的最大值"),
        },
        permissions=("cloudwatch:GetMetricStatistics",),
    ),
    "dynamodb_table_active": ProbeSpec(
        question="首尔的 DynamoDB 全局表副本是否 ACTIVE？",
        means="dynamodb:DescribeTable（首尔端点）",
        params={"table": Param("str", required=True, doc="表名")},
        permissions=("dynamodb:DescribeTable",),
    ),
    # ── 制品 ──────────────────────────────────────────────────────────────
    "ecr_image_present": ProbeSpec(
        question="清单引用的那个镜像 digest 在首尔 ECR 里是否存在？",
        means="ecr:DescribeImages（首尔，按 digest 精确查）",
        params={
            "repository": Param("str", required=True, doc="仓库名"),
            "digest": Param(
                "str", required=True,
                doc="sha256:… —— 取自要应用的清单，**不要**取自首尔 ECR 本身（那是循环论证）",
            ),
        },
        permissions=("ecr:DescribeImages",),
    ),
    "ecr_tag_present": ProbeSpec(
        question="清单按**标签**引用的镜像在首尔 ECR 里是否存在？",
        means="ecr:DescribeImages（首尔，按标签查）",
        params={
            "repository": Param("str", required=True, doc="仓库名"),
            "tag": Param("str", required=True, doc="标签"),
        },
        permissions=("ecr:DescribeImages",),
    ),
    "lambda_function_active": ProbeSpec(
        question="首尔的这个 Lambda 是否 Active 且最近一次更新成功？",
        means="lambda:GetFunctionConfiguration（首尔）",
        params={"function": Param("str", required=True, doc="函数名")},
        permissions=("lambda:GetFunctionConfiguration",),
    ),
    # ── 计算 ──────────────────────────────────────────────────────────────
    "eks_nodegroup_status": ProbeSpec(
        question="首尔节点组是否 ACTIVE（可选：desired 是否等于期望值）？",
        means="eks:DescribeNodegroup",
        params={"expect_desired": Param("int", doc="不填则不比较 desired")},
        permissions=("eks:DescribeNodegroup",),
    ),
    "eks_ready_nodes": ProbeSpec(
        question="首尔集群里 Ready 的 k8s 节点数是否达到要求？",
        means="k8s API /api/v1/nodes 的 Ready 条件（不是 EC2 实例数）",
        params={"min_ready": Param("int", required=True, doc="至少多少个 Ready 节点")},
        permissions=("eks:DescribeCluster", "k8s: nodes get/list（dr-node-readers）"),
        timeout_seconds=90,
    ),
    "asg_lifecycle_settled": ProbeSpec(
        question="节点组 ASG 是否稳定在期望的 InService 数，且没有 Pending/Terminating？",
        means="autoscaling:DescribeAutoScalingGroups 的 LifecycleState（不是 EC2 State）",
        params={"expect_in_service": Param("int", required=True, doc="期望的 InService 数")},
        permissions=("eks:DescribeNodegroup", "autoscaling:DescribeAutoScalingGroups"),
    ),
    # ── 流量 ──────────────────────────────────────────────────────────────
    "alb_target_health": ProbeSpec(
        question="这个目标组是否至少有 N 个 healthy 目标？",
        means="elasticloadbalancing:DescribeTargetHealth（首尔）",
        params={
            "target_group": Param("str", required=True, doc="目标组名"),
            "min_healthy": Param("int", default=1, doc="至少多少个 healthy"),
        },
        permissions=(
            "elasticloadbalancing:DescribeTargetGroups",
            "elasticloadbalancing:DescribeTargetHealth",
        ),
    ),
    "http_get": ProbeSpec(
        question="从首尔 worker 发起的 HTTP GET 是否返回期望的状态码（与可选的正文片段）？",
        means="urllib 直连（从首尔 VPC 内）",
        params={
            "url": Param("str", required=True, doc="完整 URL"),
            "expect_status": Param("int", default=200, doc="期望状态码"),
            "expect_body_contains": Param("str", doc="可选：正文必须包含的片段"),
        },
        permissions=(),
        timeout_seconds=30,
    ),
}


def activity_name(probe: str) -> str:
    return f"{ACTIVITY_PREFIX}{probe}"


def check_params(probe: str, params: dict) -> list[str]:
    """校验一次探针引用的参数。返回问题列表（空 = 合法）。

    未知参数一律拒绝 —— 拼错的参数名会被静默忽略，而那个探针就会测一个
    人没想测的东西（例如把 `min_healty` 拼错，就退回默认的 1）。
    """
    spec = PROBES.get(probe)
    if spec is None:
        return [f"未知探针 {probe!r}（目录里有：{', '.join(sorted(PROBES))}）"]
    if not isinstance(params, dict):
        return [f"{probe} 的 params 必须是对象"]
    problems: list[str] = []
    for k in params:
        if k not in spec.params:
            problems.append(
                f"{probe} 不接受参数 {k!r}（合法参数：{', '.join(sorted(spec.params)) or '无'}）"
            )
    for name, p in spec.params.items():
        if name not in params:
            if p.required:
                problems.append(f"{probe} 缺少必填参数 {name!r}：{p.doc}")
            continue
        v = params[name]
        # bool 是 int 的子类 —— 不排除的话 true 会被当成 1。
        ok = (
            isinstance(v, str) if p.type == "str"
            else isinstance(v, int) and not isinstance(v, bool)
        )
        if not ok:
            problems.append(f"{probe}.{name} 应为 {p.type}，实际是 {type(v).__name__}")
    return problems


def resolved_params(probe: str, params: dict) -> dict:
    """补上默认值后的参数。只在 check_params 通过后调用。"""
    spec = PROBES[probe]
    out = {k: p.default for k, p in spec.params.items() if p.default is not None}
    out.update(params)
    return out

"""runbook_draft.py — 东京 → 首尔切换步骤的**半自动**初稿。纯 Python。

## 「半自动」的边界

生成器只从**事实**推导步骤：首尔有哪些资源、清单钉了哪些镜像。凡是它不知道的，
就标 `needs_human_input`，**不编**。带这个标记的计划不能被批准 —— 人必须把它填上。

当前已知它不知道的（所以初稿里一定带着标记）：
  · 前门怎么切（Route 53 记录？CloudFront 源？）—— 仓库里没有一个可推导的事实

它也不替人做判断：Aurora 用有序切换还是允许丢数据，是一个 `decision` 步骤，
没有默认值。这两者的差别是会不会丢数据，而「东京是挂了还是只是不可达」
在指标上无法区分 —— 这正是本项目的核心立场。

## 步骤顺序的理由

    核查数据层 → 核查制品 → 拉起计算 → 部署工作负载 → 裁决 → 提升数据库 → 切前门 → 终态

先把计算与工作负载拉起来、确认目标组健康，再做唯一不可逆的那一步（提升 Aurora）。
这样不可逆的动作发生时，首尔已经被证明能接住流量；而在它之前的每一步都可以回滚。

## 不能被当成事实的东西

DynamoDB 全局表是双活的，**没有「提升」这一步** —— 不为了对称造一个提升步骤，
那会在审计记录里留下一个实际不存在的操作。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from runbook import ProbeRef, Runbook, Step, render_block, summarize


@dataclass
class DraftFacts:
    source_region: str = "ap-northeast-1"
    target_region: str = "ap-northeast-2"
    eks_cluster: str = "dr-korea-petsite"
    nodegroup: str = "dr-korea-workers"
    nodes_desired: int = 2
    nodes_max: int = 3
    global_cluster: str = "petsite-global"
    secondary_cluster_arn: str = ""
    dynamodb_table: str = ""
    #: (仓库, digest) —— 取自要应用的清单，不是取自首尔 ECR。
    images_by_digest: list[tuple[str, str]] = field(default_factory=list)
    #: (仓库, 标签) —— 清单按标签引用的。标签可变，只能核查存在。
    images_by_tag: list[tuple[str, str]] = field(default_factory=list)
    lambdas: list[str] = field(default_factory=list)
    target_groups: list[str] = field(default_factory=list)
    #: 首尔内部 ALB 上可直接访问的健康检查 URL。
    health_urls: list[str] = field(default_factory=list)
    workload_manifests: list[str] = field(default_factory=list)
    #: 生成器读事实时发现的问题 —— 写进正文给审核的人看。
    notes: list[str] = field(default_factory=list)


def draft_tokyo_to_seoul(f: DraftFacts) -> Runbook:
    steps: list[Step] = []

    steps.append(Step(
        id="preflight-data",
        kind="check",
        title="核查首尔数据层：Aurora 从集群仍在全局集群里、写节点仍在东京、复制延迟正常；DynamoDB 副本 ACTIVE",
        probes=[
            ProbeRef("aurora_global_membership"),
            ProbeRef("aurora_writer_region", {"expect_region": f.source_region}),
            ProbeRef("aurora_replication_lag", {"max_lag_ms": 5000}),
            *([ProbeRef("dynamodb_table_active", {"table": f.dynamodb_table})]
              if f.dynamodb_table else []),
        ],
        note=(
            "aurora_writer_region 期望东京：若此刻写节点已不在东京，说明有人已经动过，"
            "这份计划的前提不成立。东京完全失联时这一项会 FAIL 或 UNKNOWN —— "
            "那正是需要人判断「是挂了还是不可达」的信号，不是计划错了。"
        ),
    ))

    artifact_probes = (
        [ProbeRef("ecr_image_present", {"repository": r, "digest": d}) for r, d in f.images_by_digest]
        + [ProbeRef("ecr_tag_present", {"repository": r, "tag": t}) for r, t in f.images_by_tag]
        + [ProbeRef("lambda_function_active", {"function": n}) for n in f.lambdas]
        + [ProbeRef("eks_nodegroup_status")]
    )
    steps.append(Step(
        id="preflight-artifacts",
        kind="check",
        title="核查首尔制品：清单引用的镜像都在首尔 ECR、Lambda 可用、节点组 ACTIVE",
        probes=artifact_probes,
    ))

    steps.append(Step(
        id="scale-nodegroup",
        kind="manual",
        title=f"把首尔节点组从 0 拉到 {f.nodes_desired}",
        command=(
            f"aws eks update-nodegroup-config --region {f.target_region} "
            f"--cluster-name {f.eks_cluster} --nodegroup-name {f.nodegroup} "
            f"--scaling-config minSize={f.nodes_desired},maxSize={f.nodes_max},desiredSize={f.nodes_desired}"
        ),
        reversible=True,
        rollback=(
            f"aws eks update-nodegroup-config --region {f.target_region} "
            f"--cluster-name {f.eks_cluster} --nodegroup-name {f.nodegroup} "
            "--scaling-config minSize=0,maxSize=3,desiredSize=0"
        ),
        pre=[ProbeRef("eks_nodegroup_status")],
        post=[
            ProbeRef("eks_nodegroup_status", {"expect_desired": f.nodes_desired}),
            ProbeRef("eks_ready_nodes", {"min_ready": f.nodes_desired}),
            ProbeRef("asg_lifecycle_settled", {"expect_in_service": f.nodes_desired}),
        ],
        note="核实看 Ready 的 k8s 节点，不看 EC2 实例数 —— 实例起来了不等于集群有可调度容量。",
    ))

    manifests = " ".join(f"-f {m}" for m in f.workload_manifests) or "-f <清单路径>"
    steps.append(Step(
        id="apply-workloads",
        kind="manual",
        title="在首尔集群部署工作负载",
        command=f"kubectl --context <首尔集群> apply {manifests}",
        needs_human_input=not f.workload_manifests,
        reversible=True,
        rollback=f"kubectl --context <首尔集群> delete {manifests}",
        post=[ProbeRef("alb_target_health", {"target_group": tg, "min_healthy": 1})
              for tg in f.target_groups]
        or [ProbeRef("alb_target_health", {"target_group": "<待人填写>"})],
        note=(
            "后置探针要求每个目标组至少 1 个 healthy。ALB 在全部不健康时会 fail-open "
            "继续转发 —— 能访问不等于健康，所以看的是目标组而不是能不能打开页面。"
        ),
    ))

    steps.append(Step(
        id="aurora-mode",
        kind="decision",
        title="裁决 Aurora 切换方式（这是唯一会丢数据的分叉）",
        options=["switchover", "failover-allow-data-loss", "abort"],
        note=(
            "switchover：东京主集群还活着，无数据丢失。failover-allow-data-loss：东京已失联，"
            "未复制的写入会丢。两者在指标上分不出「挂了」与「不可达」—— 必须由人判断。"
            "选 abort 则停止执行。"
        ),
    ))

    sec = f.secondary_cluster_arn or "<首尔从集群 ARN>"
    steps.append(Step(
        id="promote-aurora",
        kind="manual",
        title="提升首尔 Aurora 从集群为写节点（按上一步裁决二选一）",
        command=(
            "按 aurora-mode 的裁决执行其中一条：\n"
            f"  switchover:               aws rds failover-global-cluster --region {f.target_region} "
            f"--global-cluster-identifier {f.global_cluster} --target-db-cluster-identifier {sec} --switchover\n"
            f"  failover-allow-data-loss: aws rds failover-global-cluster --region {f.target_region} "
            f"--global-cluster-identifier {f.global_cluster} --target-db-cluster-identifier {sec} --allow-data-loss"
        ),
        needs_human_input=not f.secondary_cluster_arn,
        reversible=False,
        irreversible_note=(
            "提升后写节点在首尔；回到东京需要重新建立复制关系并再做一次切换，"
            "不是一条命令能撤回的。放在计算与工作负载已被证明健康之后执行，正是为此。"
        ),
        pre=[ProbeRef("aurora_global_membership")],
        post=[ProbeRef("aurora_writer_region", {"expect_region": f.target_region})],
        note=(
            "--switchover 与 --allow-data-loss **互斥**（API 约束），两条命令各带一个，"
            "不依赖「不传就默认 switchover」这个 API 默认值 —— 有序还是丢数据必须是明确的裁决。"
            "后置探针是控制面视角（IsWriter）。数据面是否真的可写，"
            "要另用 SELECT pg_is_in_recovery() = false 核实 —— worker 没有数据库凭据，这一步由人做。"
        ),
    ))

    steps.append(Step(
        id="switch-front-door",
        kind="manual",
        title="把入口流量切到首尔",
        command=None,
        needs_human_input=True,
        reversible=True,
        rollback="<待人填写：把入口指回东京的命令>",
        post=[ProbeRef("http_get", {"url": "<待人填写：公网入口的健康检查 URL>"})],
        note=(
            "生成器不知道前门怎么切（Route 53 记录？CloudFront 源？）—— 仓库里没有可推导的事实。"
            "必须由人填写命令、回滚与后置探针的 URL，否则这份计划不能被批准。"
        ),
    ))

    final = [ProbeRef("http_get", {"url": u}) for u in f.health_urls]
    final += [ProbeRef("aurora_writer_region", {"expect_region": f.target_region})]
    steps.append(Step(
        id="final-state",
        kind="check",
        title="终态核查：首尔服务从内部 ALB 可达、写节点在首尔",
        probes=final,
        note=(
            "健康检查全绿不等于业务能用（本项目实测过 22 天的假成功）。"
            "执行后请再看一次业务结果 SLO（PetAdoptions/AdoptionOutcome）。"
        ),
    ))

    return Runbook(source_region=f.source_region, target_region=f.target_region, steps=steps)


def render_body(f: DraftFacts, rb: Runbook, *, generated_at: str, generator: str) -> str:
    lines = [
        f"# 灾备切换计划：{f.source_region} → {f.target_region}",
        "",
        f"生成：{generator} @ {generated_at}（**半自动初稿，待人审核**）",
        "",
        "## 执行模型",
        "",
        "每个变更由人执行。Temporal 只做两件事：把命令原文展示给你、在你确认执行后",
        "用独立的只读探针核实终态。探针不全 PASS 时停下等你决定 retry / override / abort。",
        "",
        "## 步骤一览（由下面的 dr-runbook 块渲染，块是唯一权威）",
        "",
        *summarize(rb),
        "",
    ]
    if rb.unresolved():
        lines += [
            "## ⚠️ 批准前必须由人填写",
            "",
            *[f"- `{sid}`" for sid in rb.unresolved()],
            "",
        ]
    if f.notes:
        lines += ["## 生成器读事实时的发现", "", *[f"- {n}" for n in f.notes], ""]
    lines += ["## 结构化步骤", "", render_block(rb), ""]
    return "\n".join(lines)

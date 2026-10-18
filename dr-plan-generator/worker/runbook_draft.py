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
    #: (清单, 展示名, 该清单负责的目标组)。一个清单部署一步，各自核实自己的目标组。
    workloads: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: 生成器读事实时发现的问题 —— 写进正文给审核的人看。
    notes: list[str] = field(default_factory=list)


PH_A = "A 前置核查（只读，自动）"
PH_B = "B 计算就绪（可逆，人确认）"
PH_C = "C 工作负载（可逆，人确认）"
PH_D = "D 数据层切换（裁决 + 不可逆）"
PH_E = "E 前门（可逆，人确认）"
PH_F = "F 终态核查（只读，自动）"


def _chk(sid: str, phase: str, title: str, ref: ProbeRef, note: str = "") -> Step:
    """一个只读断言 = 一步。失败时闸门停在**这一件事**上，而不是停在一大步上。"""
    return Step(id=sid, kind="check", phase=phase, title=title, probes=[ref], note=note)


def draft_tokyo_to_seoul(f: DraftFacts) -> Runbook:
    """原子步骤：一步 = 一个动作 + 它自己的前后置核实 + 它自己的回滚（2026-10-18）。

    原来 8 步里有三类「挤在一起」：一步 12 个断言（失败了看不出是哪件事）、一条命令部署
    多个清单（无法只回滚其中一个）、Aurora「按裁决二选一」两条命令写在一步里（判断留给了
    执行的人）。现在每个变更都单独由人确认、单独核实；二选一用 `when` 由工作流按裁决选定。
    """
    steps: list[Step] = []

    # ── A 前置核查 ──────────────────────────────────────────────────────
    steps.append(_chk("check-aurora-member", PH_A, "首尔 Aurora 从集群仍在全局集群里",
                      ProbeRef("aurora_global_membership")))
    steps.append(_chk(
        "check-aurora-writer-tokyo", PH_A, "Aurora 写节点仍在东京",
        ProbeRef("aurora_writer_region", {"expect_region": f.source_region}),
        note=("若此刻写节点已不在东京，说明有人已经动过，这份计划的前提不成立。东京完全失联时"
              "这一项会 FAIL 或 UNKNOWN —— 那正是需要人判断「是挂了还是不可达」的信号。"),
    ))
    steps.append(_chk("check-aurora-lag", PH_A, "Aurora 全局复制延迟 ≤ 5 秒",
                      ProbeRef("aurora_replication_lag", {"max_lag_ms": 5000})))
    if f.dynamodb_table:
        steps.append(_chk("check-dynamodb", PH_A, f"DynamoDB 全局表首尔副本 ACTIVE（{f.dynamodb_table}）",
                          ProbeRef("dynamodb_table_active", {"table": f.dynamodb_table})))
    n = 0
    for repo, digest in f.images_by_digest:
        n += 1
        steps.append(_chk(f"check-image-{n}", PH_A, f"镜像在首尔 ECR：{repo}@{digest[:19]}…",
                          ProbeRef("ecr_image_present", {"repository": repo, "digest": digest})))
    for repo, tag in f.images_by_tag:
        n += 1
        steps.append(_chk(f"check-image-{n}", PH_A, f"镜像在首尔 ECR：{repo}:{tag[:16]}",
                          ProbeRef("ecr_tag_present", {"repository": repo, "tag": tag})))
    for k, fn in enumerate(f.lambdas, 1):
        steps.append(_chk(f"check-lambda-{k}", PH_A, f"Lambda 可用：{fn}",
                          ProbeRef("lambda_function_active", {"function": fn})))
    steps.append(_chk("check-nodegroup-active", PH_A, f"首尔节点组 {f.nodegroup} 为 ACTIVE",
                      ProbeRef("eks_nodegroup_status")))

    # ── B 计算 ──────────────────────────────────────────────────────────
    ng = (f"aws eks update-nodegroup-config --region {f.target_region} "
          f"--cluster-name {f.eks_cluster} --nodegroup-name {f.nodegroup} ")
    steps.append(Step(
        id="scale-nodegroup", kind="manual", phase=PH_B,
        title=f"把首尔节点组从 0 拉到 {f.nodes_desired}",
        command=ng + f"--scaling-config minSize={f.nodes_desired},maxSize={f.nodes_max},desiredSize={f.nodes_desired}",
        reversible=True, rollback=ng + "--scaling-config minSize=0,maxSize=3,desiredSize=0",
        pre=[ProbeRef("eks_nodegroup_status")],
        post=[ProbeRef("eks_nodegroup_status", {"expect_desired": f.nodes_desired}),
              ProbeRef("eks_ready_nodes", {"min_ready": f.nodes_desired}),
              ProbeRef("asg_lifecycle_settled", {"expect_in_service": f.nodes_desired})],
        note="核实看 Ready 的 k8s 节点，不看 EC2 实例数 —— 实例起来了不等于集群有可调度容量。",
    ))

    # ── C 工作负载：一个清单一步 ─────────────────────────────────────────
    workloads = f.workloads or [(m, m.rsplit("/", 1)[-1], list(f.target_groups)
                                 if i == len(f.workload_manifests) - 1 else [])
                                for i, m in enumerate(f.workload_manifests)]
    if not workloads:
        workloads = [("<清单路径>", "工作负载", [])]
    for k, (manifest, label, tgs) in enumerate(workloads, 1):
        steps.append(Step(
            id=f"apply-workload-{k}", kind="manual", phase=PH_C,
            title=f"部署 {label}",
            command=f"kubectl --context <首尔集群> apply -f {manifest}",
            needs_human_input=manifest.startswith("<"),
            reversible=True, rollback=f"kubectl --context <首尔集群> delete -f {manifest}",
            post=[ProbeRef("alb_target_health", {"target_group": tg, "min_healthy": 1}) for tg in tgs]
            or [ProbeRef("eks_ready_nodes", {"min_ready": f.nodes_desired})],
            note=("ALB 在全部不健康时会 fail-open 继续转发 —— 能访问不等于健康，所以看的是目标组。"
                  if tgs else "这个清单没有对应的目标组；后置只能核实集群容量仍在，Pod 是否就绪需人看一眼。"),
        ))

    # ── D 数据层：裁决 → 两个互斥的条件步骤 ─────────────────────────────
    steps.append(Step(
        id="aurora-mode", kind="decision", phase=PH_D,
        title="裁决 Aurora 切换方式（这是唯一会丢数据的分叉）",
        options=["switchover", "failover-allow-data-loss", "abort"],
        note=("switchover：东京主集群还活着，无数据丢失。failover-allow-data-loss：东京已失联，"
              "未复制的写入会丢。两者在指标上分不出「挂了」与「不可达」—— 必须由人判断。选 abort 则停止执行。"),
    ))
    sec = f.secondary_cluster_arn or "<首尔从集群 ARN>"
    base = (f"aws rds failover-global-cluster --region {f.target_region} "
            f"--global-cluster-identifier {f.global_cluster} --target-db-cluster-identifier {sec}")
    irrev = ("提升后写节点在首尔；回到东京需要重新建立复制关系并再做一次切换，不是一条命令能撤回的。"
             "放在计算与工作负载已被证明健康之后执行，正是为此。")
    # --switchover 与 --allow-data-loss **互斥**（API 约束）：两个条件步骤各带一个，
    # 不依赖「不传就默认 switchover」这个 API 默认值 —— 有序还是丢数据必须是明确的裁决，
    # 由工作流按 aurora-mode 的结果选中其中一步、跳过另一步。
    for sid, opt, flag, title in (
        ("promote-aurora-switchover", "switchover", "--switchover", "有序切换：提升首尔 Aurora 为写节点（不丢数据）"),
        ("promote-aurora-failover", "failover-allow-data-loss", "--allow-data-loss",
         "强制切换：提升首尔 Aurora 为写节点（**未复制的写入会丢**）"),
    ):
        steps.append(Step(
            id=sid, kind="manual", phase=PH_D, title=title,
            when={"step": "aurora-mode", "in": [opt]},
            command=f"{base} {flag}", needs_human_input=not f.secondary_cluster_arn,
            reversible=False, irreversible_note=irrev,
            pre=[ProbeRef("aurora_global_membership")],
            post=[ProbeRef("aurora_writer_region", {"expect_region": f.target_region})],
            note=("后置探针是控制面视角（IsWriter）。数据面是否真的可写，要另用 "
                  "SELECT pg_is_in_recovery() = false 核实 —— worker 没有数据库凭据，这一步由人做。"),
        ))

    # ── E 前门 ──────────────────────────────────────────────────────────
    steps.append(Step(
        id="switch-front-door", kind="manual", phase=PH_E, title="把入口流量切到首尔",
        command=None, needs_human_input=True, reversible=True,
        rollback="<待人填写：把入口指回东京的命令>",
        post=[ProbeRef("http_get", {"url": "<待人填写：公网入口的健康检查 URL>"})],
        note="前门走 CloudFront（DNS 托管不变），见 docs/runbooks/dr-arc-temporal-hybrid-design.md §5。",
    ))

    # ── F 终态 ──────────────────────────────────────────────────────────
    for k, u in enumerate(f.health_urls, 1):
        steps.append(_chk(f"check-final-http-{k}", PH_F, f"首尔服务从内部 ALB 可达：{u}",
                          ProbeRef("http_get", {"url": u})))
    steps.append(_chk(
        "check-final-writer-seoul", PH_F, "Aurora 写节点在首尔",
        ProbeRef("aurora_writer_region", {"expect_region": f.target_region}),
        note="健康检查全绿不等于业务能用。执行后请再看一次业务结果 SLO（PetAdoptions/AdoptionOutcome）。",
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
        "每个变更由人执行、**每一步单独确认**。Temporal 只做两件事：把命令原文展示给你、",
        "在你确认执行后用独立的只读探针核实终态。探针不全 PASS 时停下等你决定 retry / override / abort。",
        "只读核查自动跑，不过才停下等人；按裁决二选一的步骤由工作流按裁决选定，另一条跳过。",
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

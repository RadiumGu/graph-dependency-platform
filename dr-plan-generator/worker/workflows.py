"""
workflows.py — 灾备切换的 Temporal workflow 定义。

## 这个 workflow 存在的理由

region 切换要几十分钟、跨多个需要人判断的点。放在一个进程里跑,进程死了
就断在中途,而且没有任何地方记得走到了第几步 —— 那是 `StrandsExecutor`
的形态。交给 Temporal 以后,进度在服务端,worker 重启能接着走。

## ⚠️ 两条从实测里来的硬约束

**① workflow 里不许做任何不确定的事。** Temporal 靠重放历史来恢复状态,
所以 workflow 函数必须是确定性的:不能直接调 AWS、不能 `time.time()`、
不能 `random`。所有副作用走 activity。这不是风格建议,违反了会在重放时
产生不一致而让 workflow 卡死。

**② 启动成功不等于在执行。** 实测(1.29.7):在**没有任何 worker** 的任务
队列上起 workflow 也会返回 `started: true` / `status: RUNNING`,那个执行
会一直挂着等一个不存在的 worker,直到保留期到点。所以发起方必须独立确认
有 worker 接单 —— 见 `executor_temporal.interpret_task_queue_pollers`。

## 决策点的设计

切换里有些步骤**不能由程序自己决定**。最尖锐的是数据库提升:

    有序切换(failover-global-cluster)        无数据丢失,但要求主集群还活着
    --allow-data-loss 变体                   主集群已失联时才用,会丢数据

「用哪个」取决于「主 region 到底是挂了还是只是不可达」—— 而这两者
**在指标上无法区分**,这正是本项目的核心立场。所以这里不猜:
workflow 停下来等一个 signal,由人给出裁决。

超时不放行:等不到 signal 就超时失败,而不是「超时了就按 allow-data-loss 走」。
自动选择丢数据的那条路是最坏的默认值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
# ⚠️ ApplicationError 在 temporalio.exceptions，**不在 temporalio.workflow**。
# 2026-09-26 实测踩到：写 `workflow.ApplicationError(...)` 会得到
#     AttributeError: module 'temporalio.workflow' has no attribute 'ApplicationError'
# 而它只在**失败路径**上才被执行 —— 所以这个缺陷一直潜伏，
# 直到第一次真的失败时才露头。那一刻的表现是：
#   workflow 想报告失败 -> 报告失败的代码自己抛异常 -> activation 无限重试
#   -> 执行显示为「永远 RUNNING」，而不是「失败并写明原因」。
# 也就是**报告失败的机制自己坏了**，而旁边的注释还声称它保证了明确失败。
from temporalio.exceptions import ApplicationError
from temporalio.common import RetryPolicy, SearchAttributeKey

# activity 的导入必须放在 sandbox 豁免里 —— workflow 沙箱会拦截
# 带副作用的模块导入。这是 temporalio 的标准写法,不是绕过检查。
with workflow.unsafe.imports_passed_through():
    from activities import (
        ActivityInput,
        StepResult,
        fetch_plan_body,
        promote_database,
        scale_up_nodegroup,
        verify_step,
    )


# ── Search Attributes ─────────────────────────────────────────────────────
#
# 这五个属性必须**先在服务端注册**才能写:
#
#   temporal operator search-attribute create --name DRStepsExecuted --type KeywordList
#   temporal operator search-attribute create --name DRPlanRef       --type Keyword
#   temporal operator search-attribute create --name DRDryRun        --type Bool
#   temporal operator search-attribute create --name DRStepName      --type Keyword
#   temporal operator search-attribute create --name DRDecision      --type Keyword
#
# 2026-09-26 已在 ap-northeast-2 的 default 命名空间注册完毕。
#
# ## 为什么需要它们
#
# Event History 本身就是审计日志,但**它不可查询**。
# 「哪些运行真的执行过数据库提升」这个问题,靠翻 history 回答不了 ——
# 而这正是事后审计最常问的一句。有了属性就是一条查询:
#
#   temporal workflow list --query 'DRStepsExecuted = "promote_database" AND DRDryRun = false'
#
# 反过来也成立:`DRDryRun = true` 的运行可以一眼从审计范围里排除。

_SA_STEPS_EXECUTED = SearchAttributeKey.for_keyword_list("DRStepsExecuted")
_SA_PLAN_REF = SearchAttributeKey.for_keyword("DRPlanRef")
_SA_DRY_RUN = SearchAttributeKey.for_bool("DRDryRun")
_SA_STEP_NAME = SearchAttributeKey.for_keyword("DRStepName")
_SA_DECISION = SearchAttributeKey.for_keyword("DRDecision")


# ── 决策点的取值 ───────────────────────────────────────────────────────────

DECISION_ORDERED = "ordered"
DECISION_ALLOW_DATA_LOSS = "allow_data_loss"
DECISION_ABORT = "abort"

_VALID_DECISIONS = frozenset({DECISION_ORDERED, DECISION_ALLOW_DATA_LOSS, DECISION_ABORT})


# ── activity 的重试与超时策略 ──────────────────────────────────────────────
#
# 读类 activity 可以放心重试。写类**默认只试一次** —— 一个已经把节点组
# 拉起来的调用重试第二遍未必幂等,而 DR 步骤的重试代价是真实的。
# 需要重试的写操作要自己在 activity 内部做幂等。

_READ_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_attempts=5,
)
_WRITE_RETRY = RetryPolicy(maximum_attempts=1)


# ── 原子步骤的输入 ─────────────────────────────────────────────────────────


@dataclass
class StepInput:
    """一个原子步骤子 workflow 的输入。

    刻意**不传整个 FailoverInput** —— 子 workflow 只该知道自己这一步的事。
    `live` 由父 workflow 算好再传进来,子 workflow 不重新判断放行条件:
    放行判定只有一处,否则两处逻辑会漂移,而漂移的方向是「本该 dry_run 的
    步骤真跑了」。
    """

    plan_ref: str
    #: True = 真执行。父 workflow 用 `_step_is_live` 算出来的结果。
    live: bool = False
    #: 仅 promote_database 用。
    decision: str | None = None
    #: 这一步操作的目标标识（如 aurora-global / ddb-petadoption）。
    #: 同一个步骤类型作用在不同目标上时,靠它区分。
    target: str = ""


# ── 原子步骤:每步一个独立的子 workflow ─────────────────────────────────────
#
# ## 为什么从「一个 workflow 串 5 个 activity」改成子 workflow
#
# 官方默认建议其实是保守的（encyclopedia/child-workflows):
# *"When in doubt, use an Activity."* 但这里的诉求正好命中子 workflow
# 的核心优势:**每个子 workflow 有自己独立的 Event History**。父的 history
# 里只留子的输入、输出与启动/完成三件事,内部全部状态变化在子自己的
# history 里。
#
# 这换来三样在灾备场景里真正要紧的东西:
#
#   ① 独立审计 —— 「第二个数据库的提升到底做了什么」是一条可单独调出来
#      的 history,而不是埋在一条几十个事件的长流水里
#   ② 独立重跑 —— 父流程结束后,可以直接按 workflow type 单独起
#      `PromoteDatabaseWorkflow`,不必碰父流程
#   ③ 独立超时与重试 —— 每步各自设,而不是共享一套
#
# ⚠️ **不是用子 workflow 取代 activity,而是两层都要**:子 workflow 给边界
# 与可审计性,activity 做实际的 AWS 调用与重试。副作用永远在 activity 里。
#
# ## 关于「单步重跑」为什么不用 reset
#
# `temporal workflow reset` 是**终止当前执行、用同一个 workflow ID 新建
# 一个执行**并从选定点重放。它只能重置到 workflow task 边界,重置点之后
# 的一切被丢弃,而之前完成的 activity **不会重跑**（结果直接复用）。
# 文档原话:*"Reset only works if you've fixed the underlying issue"* ——
# 它是为修非确定性 bug 设计的,不是为「重跑某一步」设计的。
#
# 「这个数据库提升失败了,只重跑这一步」的正确做法是**直接以该子 workflow
# type 起一个新执行**,给一个新的 workflow ID。这也是把每步做成独立
# workflow type 的附带好处。
#
# ## 两个来自文档的坑
#
#   · 父 workflow `continue-as-new` 时**子 workflow 不会带过去**
#   · 父如果不 await 子的完成,子可能被连带终止 —— 要 ABANDON 策略才能
#     让子活过父。本文件所有子都是 await 的,不涉及。


@workflow.defn(name="FetchPlanWorkflow")
class FetchPlanWorkflow:
    """① 取计划正文。读操作,可重试。"""

    @workflow.run
    async def run(self, args: StepInput) -> StepResult:
        _tag_step(args, "fetch_plan_body")
        return await workflow.execute_activity(
            fetch_plan_body,
            ActivityInput(plan_ref=args.plan_ref, dry_run=not args.live),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_READ_RETRY,
        )


@workflow.defn(name="ScaleNodegroupWorkflow")
class ScaleNodegroupWorkflow:
    """② 拉起 EKS 节点组。守夜灯站点常态 desired=0,切换时才拉。

    写操作,**只试一次** —— 一个已经把节点组拉起来的调用重试第二遍未必幂等。
    """

    @workflow.run
    async def run(self, args: StepInput) -> StepResult:
        _tag_step(args, "scale_up_nodegroup")
        return await workflow.execute_activity(
            scale_up_nodegroup,
            ActivityInput(plan_ref=args.plan_ref, dry_run=not args.live),
            # 节点起来要几分钟,给宽一点;activity 内部发心跳。
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=_WRITE_RETRY,
        )


@workflow.defn(name="PromoteDatabaseWorkflow")
class PromoteDatabaseWorkflow:
    """③ 提升一个数据库。**按目标参数化,一个目标一个独立执行。**

    ## 为什么参数化而不是写死

    把「提升数据库」做成可传目标的一个 workflow type,意味着 N 个数据库
    就是 N 条独立的子执行、N 条独立的 history、可以各自单独重跑。
    这正是「原子分解、易审计」要的形状。

    ## ⚠️ 关于「两个数据库」这个说法需要修正

    本环境里只有 **Aurora 全局集群**有真正的提升操作
    (`failover-global-cluster` 的 `--switchover` / `--allow-data-loss`)。
    **DynamoDB 全局表是 active-active 的,没有「提升」这一步** ——
    写入任意区都成立。DynamoDB 侧需要的是**核实副本可写**,
    那属于 VerifyWorkflow,不是这里。

    所以不要为了对称而给 DynamoDB 造一个提升步骤:那会在审计记录里
    留下一个实际不存在的操作,比缺这一步更糟。
    """

    @workflow.run
    async def run(self, args: StepInput) -> StepResult:
        _tag_step(args, "promote_database")
        return await workflow.execute_activity(
            promote_database,
            ActivityInput(
                plan_ref=args.plan_ref,
                dry_run=not args.live,
                decision=args.decision,
            ),
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=_WRITE_RETRY,
        )


@workflow.defn(name="VerifyWorkflow")
class VerifyWorkflow:
    """④ 核实。**与执行不同的手段** —— 这是本项目的纪律。

    已实测六次「命令成功但没生效」,所以核实绝不能复用执行路径的返回值。
    """

    @workflow.run
    async def run(self, args: StepInput) -> StepResult:
        _tag_step(args, "verify_step")
        return await workflow.execute_activity(
            verify_step,
            ActivityInput(plan_ref=args.plan_ref, dry_run=not args.live),
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_READ_RETRY,
        )


def _tag_step(args: StepInput, step: str) -> None:
    """给子 workflow 打上可查询的属性。

    每个子执行都自带 `DRStepName` / `DRPlanRef` / `DRDryRun`,
    所以「找出所有真跑过的 promote_database」是一条查询而不是一次翻历史:

        temporal workflow list --query \\
          'DRStepName = "promote_database" AND DRDryRun = false'
    """
    workflow.upsert_search_attributes(
        [
            _SA_STEP_NAME.value_set(step),
            _SA_PLAN_REF.value_set(args.plan_ref),
            _SA_DRY_RUN.value_set(not args.live),
        ]
    )


# ── 决策点 ────────────────────────────────────────────────────────────────

#: 数据库提升方式的裁决。signal 只接受这两个值之一。

@dataclass
class FailoverInput:
    """启动参数。

    ⚠️ 计划正文**不在这里**,只有引用。
    原因:本服务端 workflowExecutionRetentionTtl = 86400s(1 天,实测),
    而 payload 上限拿不到 —— describe_namespace 的 namespaceInfo 里没有
    limits 字段(实测推翻了我原先的假设)。所以正文另存,这里只放 S3 引用。
    """

    plan_ref: str
    dry_run: bool = True
    #: 等人给裁决的上限。等不到就失败,**不自动选择丢数据的那条路**。
    decision_timeout_seconds: int = 1800

    #: ── 要提升哪些数据库 ──────────────────────────────────────────────
    #:
    #: 每个目标起**一个独立的子 workflow 执行**,ID 形如
    #: `<父ID>-promote_database-<目标>`。这样两个数据库的切换是两条独立的
    #: history,能各自审计、各自单独重跑。
    #:
    #: ⚠️ 只列**真有提升操作**的目标。本环境里只有 Aurora 全局集群算:
    #: DynamoDB 全局表是 active-active 的,没有提升这一步 ——
    #: 给它造一个会在审计记录里留下一个实际不存在的操作。
    promote_targets: list[str] = field(default_factory=lambda: ["aurora-global"])

    #: ── 按步骤放行 ────────────────────────────────────────────────────
    #:
    #: 只有名字出现在这里的步骤才会**真执行**,其余一律 dry_run ——
    #: 即使 `dry_run=False`。
    #:
    #: 2026-09-24 加的,动机是安全而不是灵活性:做「拉起节点组」的演练时,
    #: 若只有一个全局 `dry_run=False` 开关,同一次运行就会把
    #: `promote_database` 也真执行 —— 那是**切换生产数据库**。
    #: 一次节点组演练绝不该有能力做那件事。
    #:
    #: 所以放行是**按名字逐个给**的:要执行某一步,必须显式写出它的名字。
    #: 漏写的后果是「那一步没真跑」(安全),而不是「意外跑了」(危险)。
    execute_steps: list[str] = field(default_factory=list)


def _step_is_live(args: FailoverInput, step: str) -> bool:
    """这一步该真执行还是 dry_run。

    两个条件都满足才真执行:全局 `dry_run=False`,**并且**步骤名在
    `execute_steps` 里。两道闸门是刻意的 —— 单独任何一个被误设都不足以
    让危险步骤真跑。
    """
    return (not args.dry_run) and (step in args.execute_steps)


@dataclass
class FailoverResult:
    plan_ref: str
    dry_run: bool
    #: 这次放行了哪些步骤真执行。事后复盘要能看出「这是一次什么演练」。
    executed_steps: list[str] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    #: 人给的裁决。None 表示没走到那个点。
    database_decision: str | None = None
    aborted: bool = False




@workflow.defn(name="DrFailoverWorkflow")
class DrFailoverWorkflow:
    """执行一份灾备计划。

    ⚠️ 当前阶段只跑 dry_run。写类 activity 里真正调 AWS 的部分尚未启用 ——
    worker 的实例角色目前只有 Describe 权限,见 infra/dr-korea/02-temporal.yaml。
    """

    def __init__(self) -> None:
        self._decision: str | None = None
        #: 裁决是从哪个通道进来的（update / signal）。审计要能区分。
        self._decision_channel: str | None = None
        self._steps: list[StepResult] = []

    # ── 人给裁决:两个通道,update 优先 ──────────────────────────────────────
    #
    # ## 为什么同时留 update 和 signal
    #
    # 官方建议很明确（evaluate/features/workflow-message-passing）:
    #
    #   > Send a **Signal** when the caller can move on without a result and
    #   > shouldn't depend on a Worker being available to accept the request.
    #   > Send an **Update** when the caller needs a result or needs to know
    #   > it was accepted.
    #
    # 审批显然属于后者,而 update 还多一样 signal 给不了的东西:**validator
    # 能在请求进入 history 之前拒掉它**。非法裁决因此不会在审计记录里留下
    # 一条废条目 —— 对「容易审计」这个目标是直接的改善。
    #
    # 但 update 是同步的,**需要有 worker 在线才能接受**;signal 不需要。
    # 灾难场景下这个差别是实质的。
    #
    # 两个通道都留的第三个理由是现实约束:**本项目的 temporal-mcp
    # （36 个工具）不支持 Workflow Update**,只有 `signal_workflow` /
    # `signal_with_start_workflow`。所以 agent 经 MCP 驱动时只能走 signal。
    # 砍掉 signal 会直接切断那条路。
    #
    # ## 两个通道并存的歧义怎么消掉
    #
    # **先到先得,后到的一律拒绝**,并记下是哪个通道给的。
    # 否则「两个人从两条路给了不同裁决」会变成一个说不清的状态,
    # 而这一步的分歧后果是「丢不丢数据」。

    @workflow.update(name="database_decision")
    def decide_database(self, decision: str) -> dict[str, Any]:
        """裁决数据库提升方式（推荐通道）。返回是否被接受。"""
        self._decision = decision
        self._decision_channel = "update"
        workflow.logger.info("裁决 %r 经 update 通道接受。", decision)
        return {"accepted": True, "decision": decision, "channel": "update"}

    @decide_database.validator
    def _validate_decision(self, decision: str) -> None:
        """在进入 history 之前挡掉非法与重复的裁决。

        validator 里 raise 是**被支持的**:请求被拒绝、不写入 history、
        调用方拿到明确错误。这和 signal handler 里 raise 完全不同 ——
        后者会让 workflow task 失败并无限重试,把一次手滑变成卡死。
        """
        if decision not in _VALID_DECISIONS:
            raise ValueError(
                f"非法裁决 {decision!r}。合法值：{sorted(_VALID_DECISIONS)}"
            )
        if self._decision is not None:
            raise ValueError(
                f"裁决已由 {self._decision_channel} 通道给出（{self._decision!r}），"
                "不接受第二次。要改裁决请终止本次执行后重新发起 —— "
                "这一步的分歧后果是会不会丢数据，不允许静默覆盖。"
            )

    @workflow.signal(name="database_decision")
    def set_database_decision(self, decision: str) -> None:
        """裁决数据库提升方式（兼容通道,供不支持 update 的调用方）。

        非法值**直接忽略并记日志**,不抛异常 —— signal handler 里抛异常会
        让 workflow task 失败并无限重试该 handler,把一次手滑变成卡死。
        """
        if decision not in _VALID_DECISIONS:
            workflow.logger.warning(
                "收到非法裁决 %r，已忽略。合法值：%s", decision, sorted(_VALID_DECISIONS)
            )
            return
        if self._decision is not None:
            workflow.logger.warning(
                "裁决已由 %s 通道给出（%r），忽略 signal 给的 %r。",
                self._decision_channel, self._decision, decision,
            )
            return
        self._decision = decision
        self._decision_channel = "signal"

    @workflow.query(name="pending_decision")
    def pending_decision(self) -> dict[str, Any]:
        """让外部能查「现在卡在哪、等什么」,而不用翻历史。"""
        return {
            "waiting_for_database_decision": self._decision is None,
            "decision": self._decision,
            "decision_channel": self._decision_channel,
            "steps_done": len(self._steps),
        }

    # ── 主流程 ────────────────────────────────────────────────────────────
    #
    # 父 workflow **只做编排与决策,不做任何副作用**。每一步都是一个独立的
    # 子 workflow,副作用在子 workflow 内部的 activity 里。
    #
    # 父的 history 因此只记每个子的输入、输出与启动/完成 —— 一眼能看出
    # 「这次切换走了哪几步、每步结果如何」,而某一步的细节去看它自己的
    # history。这是「一条一条独立清晰」的落法。

    async def _run_step(
        self,
        wf: Any,
        step: str,
        args: FailoverInput,
        *,
        target: str = "",
        decision: str | None = None,
    ) -> StepResult:
        """起一个步骤子 workflow 并记录结果。

        子 workflow ID 用 `<父ID>-<步骤>[-<目标>]`,理由:
        **它要能被人直接读出来**。事后审计时 `dr-rebuild-179…-promote-aurora`
        自己就说明了是哪次切换的哪一步作用在哪个目标上,不必先查父再查子。
        """
        live = _step_is_live(args, step)
        child_id = f"{workflow.info().workflow_id}-{step}"
        if target:
            child_id = f"{child_id}-{target}"
        res: StepResult = await workflow.execute_child_workflow(
            wf.run,
            StepInput(
                plan_ref=args.plan_ref,
                live=live,
                decision=decision,
                target=target,
            ),
            id=child_id,
            task_queue=workflow.info().task_queue,
        )
        self._steps.append(res)
        return res

    @workflow.run
    async def run(self, args: FailoverInput) -> FailoverResult:
        result = FailoverResult(
            plan_ref=args.plan_ref,
            dry_run=args.dry_run,
            executed_steps=list(args.execute_steps),
        )

        # 一开始就打上可查询属性 —— 不要等结束才写,失败的执行同样需要被审计。
        workflow.upsert_search_attributes(
            [
                _SA_PLAN_REF.value_set(args.plan_ref),
                _SA_DRY_RUN.value_set(args.dry_run),
                _SA_STEPS_EXECUTED.value_set(list(args.execute_steps)),
            ]
        )

        # ① 取计划正文。
        await self._run_step(FetchPlanWorkflow, "fetch_plan_body", args)
        result.steps = list(self._steps)

        # ② 拉起 EKS 节点组。守夜灯站点常态 desired=0,切换时才拉。
        await self._run_step(ScaleNodegroupWorkflow, "scale_up_nodegroup", args)
        result.steps = list(self._steps)

        # ③ ⚠️ 决策点:等人裁决数据库怎么提升。
        #
        #    这里**刻意不设默认值**。等不到就失败,不是「超时按 allow-data-loss
        #    走」—— 自动选择丢数据的那条路是最坏的默认值。
        workflow.logger.info(
            "等待人工裁决数据库提升方式（update 或 signal database_decision，合法值 %s）",
            sorted(_VALID_DECISIONS),
        )
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=args.decision_timeout_seconds),
            )
        except TimeoutError:
            # 明确失败，且把「为什么失败」写进异常 —— 让看到失败的人
            # 知道是没人放行，而不是某个 AWS 调用挂了。
            raise ApplicationError(
                f"等待 database_decision 超过 {args.decision_timeout_seconds}s"
                "（update 与 signal 两个通道都没有收到）。"
                "切换未继续。这是刻意的：数据库提升方式必须由人裁决，"
                "因为「主 region 挂了」与「主 region 只是不可达」在指标上无法区分。",
                non_retryable=True,
            ) from None

        assert self._decision is not None  # wait_condition 已保证
        result.database_decision = self._decision
        # 裁决一到手就写进可查询属性 —— 后续步骤失败也不影响它被审计到。
        workflow.upsert_search_attributes([_SA_DECISION.value_set(self._decision)])

        if self._decision == DECISION_ABORT:
            result.aborted = True
            workflow.logger.info(
                "裁决为 abort（来自 %s 通道），切换在数据库提升前停止。",
                self._decision_channel,
            )
            return result

        # ④ 提升数据库。**每个目标一个独立的子执行。**
        #
        #    ⚠️ 这一步要真执行，必须在 execute_steps 里显式写出
        #    "promote_database"。一次节点组演练不该有能力切换生产数据库。
        #
        #    关于「两个数据库」：本环境只有 Aurora 全局集群有真正的提升操作。
        #    DynamoDB 全局表是 active-active 的，没有提升这一步 —— 它需要的是
        #    核实副本可写，属于第 ⑤ 步。不要为了对称造一个不存在的操作。
        for target in args.promote_targets:
            await self._run_step(
                PromoteDatabaseWorkflow,
                "promote_database",
                args,
                target=target,
                decision=self._decision,
            )
        result.steps = list(self._steps)

        # ⑤ 核实。**与执行不同的手段** —— 这是本项目的纪律:
        #    已实测六次「命令成功但没生效」。
        await self._run_step(VerifyWorkflow, "verify_step", args)
        result.steps = list(self._steps)
        return result

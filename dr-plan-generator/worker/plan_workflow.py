# plan_workflow.py —— 一份灾备计划的完整生命周期，作为一条 Temporal 执行
#
# ## 这个文件回答的问题
#
#   「系统先自动生成一次容灾切换计划，然后人去审核和修改，然后去演练，
#     可能还要再一次修改」
#
# 这是一条**有状态、跨天、多次人工介入**的流程。它不是"执行一次切换"，
# 所以不属于 DrFailoverWorkflow；它是那件事的**上游**。
#
# ## 为什么用一条长命 workflow 承载它，而不是一张表 + 一个 API
#
# 因为诉求是「容易审计」，而 Temporal 的 Event History **本身就是审计链**：
# 每一次修订、每一次审批、每一次演练都是一个不可篡改、带时间戳、
# 带调用方声明身份的事件，顺序天然正确。用表存则要自己实现审计日志，
# 而"自己实现的审计日志"和"被审计的数据"通常由同一段代码写入 ——
# 那不是审计。
#
# 保留期已在 2026-09-26 从 24h 提到 **720h（30 天）**，所以这条链现在
# 真的留得住。此前一次演练的证据不到一天就蒸发。
#
# ## 为什么修改走 Update 而不是 Signal
#
# Update 有 **validator**：非法修订、过期审批、越序执行请求在**进入
# history 之前**就被拒掉，调用方同步拿到原因。这有两重价值：
#
#   ① 安全  —— 越序的执行授权根本不会生效
#   ② 审计  —— history 里不会堆一串废请求；留下的每条都是真发生过的事
#
# Signal 给不了这两样：它没有 validator，非法值照样落进 history，
# 且调用方拿不到结果。所以本文件**刻意不提供 signal 通道** ——
# 与 DrFailoverWorkflow 不同，那里保留 signal 是因为「灾难时 worker 可能
# 不在线，而裁决必须能投递」。计划评审不是灾难时动作，没有这个约束。
#
# temporal-mcp 已于本次补上 `update_workflow`（此前只有 signal_workflow），
# 所以这条路对 agent 调用方也是通的。
#
# ## 唯一的硬闸门：演练必须覆盖被批准的那一版
#
# 这是整个设计里最要紧的一条，也是本文件唯一**强制**的规则：
#
#   真执行要求  drilled_version == approved_version == current_version
#
# 理由很直接：演练了 v2、批准了 v2、然后改到 v4 再去真切换，那次演练
# **对 v4 什么都没证明**。而灾备计划恰恰是最容易出现这种漂移的东西 ——
# 演练完发现问题、改了计划、然后带着"我们演练过"的错觉去执行。
#
# 这条之所以做成硬闸门，是因为它**可验证**：三个版本号都是本 workflow
# 自己的状态，不依赖任何外部声明。
#
# ## 刻意**不**做成闸门的一条：四眼原则
#
# 「修订人不能是审批人」听起来该强制，但 `author` / `approver` 都是
# **调用方自报的字符串**，没有任何认证：AgentCore 不透传调用方身份
# （实测 temporal-mcp 的 agentcore-handler 里零 header 处理），
# Temporal 的 `identity` 字段同样是自报。
#
# 把一个填个别的名字就能绕过的检查做成"授权闸门"，比不做更糟 ——
# 它会让读的人以为有人把过关。所以这里**记录**而不**阻止**：
# `plan_state` 查询直接返回 `same_person_revised_and_approved`，
# 由审计去发现。可验证的强制，不可验证的记录。

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey

with workflow.unsafe.imports_passed_through():
    from activities import StepResult, put_plan_version, PlanVersionInput
    from workflows import DrFailoverWorkflow, FailoverInput, FailoverResult


# ── 可检索属性 ────────────────────────────────────────────────────────────
#
# ⚠️ 这三个必须在 worker 启动时已注册（worker.ensure_search_attributes）。
# 未注册时 `upsert_search_attributes` 让服务端在 **activation 提交阶段**
# 返回 "search attribute ... is not defined"，而那个错误在 workflow 内部
# **接不住**，执行会无限重试 —— RUNNING 但永不前进。2026-09-26 实测踩过。

_SA_PLAN_ID = SearchAttributeKey.for_keyword("DRPlanId")
_SA_PLAN_VERSION = SearchAttributeKey.for_int("DRPlanVersion")
_SA_PLAN_STATE = SearchAttributeKey.for_keyword("DRPlanState")


# ── 状态 ──────────────────────────────────────────────────────────────────

STATE_DRAFT = "draft"
STATE_APPROVED = "approved"
STATE_DRILLING = "drilling"
STATE_DRILLED = "drilled"
STATE_EXECUTING = "executing"
STATE_EXECUTED = "executed"
STATE_CLOSED = "closed"

#: 终态：不再接受任何修改。
_TERMINAL = frozenset({STATE_EXECUTED, STATE_CLOSED})

#: 单版正文上限。
#:
#: 取 256 KiB 而不是 payload 的硬上限（2 MiB）：256 KiB 是服务端开始
#: 告警的量级，而 history 会累积**每一版**正文。刻意把正文放进 update
#: 参数（而不是只放 S3 引用）是为了让审计自洽 —— 只存引用的话，
#: 谁覆盖了 S3 对象，history 里看不出来，而"审计链里没有被审计的东西"
#: 不叫审计链。S3 侧用**版本化不可覆盖的键**兜住执行路径，两边用 sha256 对齐。
MAX_BODY_BYTES = 256 * 1024


@dataclass
class PlanRevision:
    """一次修订。history 里已有同样的信息，这里是为了 query 能一次读全。"""

    version: int
    author: str
    reason: str
    sha256: str
    s3_key: str
    #: generated = 系统自动生成；human = 人改的。
    origin: str
    at: str
    #: 这次修订作废了对哪一版的批准 / 演练。None = 没作废任何东西。
    invalidated_approval_of: int | None = None
    invalidated_drill_of: int | None = None


@dataclass
class PlanApproval:
    version: int
    approver: str
    note: str
    at: str
    #: 修订人与审批人是否同一字符串。**记录，不阻止** —— 见文件头说明。
    same_as_last_reviser: bool = False


@dataclass
class DrillRecord:
    version: int
    child_workflow_id: str
    #: None = 还没跑完 / 无法判断。不许用 False 表示"没测"。
    ok: bool | None = None
    steps: list[StepResult] = field(default_factory=list)
    inconclusive: list[str] = field(default_factory=list)
    at: str = ""
    failure: str | None = None


@dataclass
class PlanInput:
    """启动参数。v1 正文由生成器给出。"""

    plan_id: str
    #: 自动生成的初版正文。
    body: str
    author: str = "dr-plan-generator"
    reason: str = "自动生成的初版"
    #: 执行/演练用的任务队列。
    failover_task_queue: str = "dr-plan-queue"
    promote_targets: list[str] = field(default_factory=lambda: ["aurora-global"])
    #: 空闲多久后自动收口。默认与保留期对齐，避免僵尸 RUNNING 堆积。
    idle_timeout_hours: int = 720


@dataclass
class PlanResult:
    plan_id: str
    final_state: str
    final_version: int
    revisions: list[PlanRevision] = field(default_factory=list)
    approvals: list[PlanApproval] = field(default_factory=list)
    drills: list[DrillRecord] = field(default_factory=list)
    execution: DrillRecord | None = None
    closed_reason: str | None = None


_WRITE_RETRY = RetryPolicy(maximum_attempts=3)


def _sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@workflow.defn(name="DrPlanWorkflow")
class DrPlanWorkflow:
    """一份灾备计划从自动生成到被执行的全过程。

    ## 状态机

        draft ──approve_plan──> approved ──start_drill──> drilling ──> drilled
          ^                        │                                     │
          └──── revise_plan ───────┴─────────────────────────────────────┘
                （修订作废已有批准与演练，退回 draft）

        drilled ──authorize_execution──> executing ──> executed（终态）
        任意非终态 ──close_plan──> closed（终态）

    ## 每个动作对应一次 MCP 调用

        update_workflow --update_name revise_plan        --input '{...}'
        update_workflow --update_name approve_plan       --input '{"version":2,...}'
        update_workflow --update_name start_drill        --input '{...}'
        update_workflow --update_name authorize_execution --input '{...}'
        query_workflow  --query_type plan_state | revision_log | current_body
    """

    def __init__(self) -> None:
        self._plan_id: str = ""
        self._body: str = ""
        self._version: int = 0
        self._state: str = STATE_DRAFT
        self._revisions: list[PlanRevision] = []
        self._approvals: list[PlanApproval] = []
        self._drills: list[DrillRecord] = []
        self._execution: DrillRecord | None = None

        self._approved_version: int | None = None
        self._drilled_version: int | None = None
        #: 主循环要做的下一个动作。update handler 只置位，不做长活。
        self._pending: dict[str, Any] | None = None
        #: 当前在跑的子执行 ID —— 演练/执行时裁决要发给它，不是发给本 workflow。
        self._active_child: str | None = None
        self._closed_reason: str | None = None
        self._cfg: PlanInput | None = None
        #: 修订是 async handler，需要防并发交错 —— 见 _validate_revise。
        self._revise_in_flight: bool = False

    # ── 公共校验 ──────────────────────────────────────────────────────────

    def _reject_if_terminal(self, action: str) -> None:
        if self._state in _TERMINAL:
            raise ValueError(
                f"计划已处于终态 {self._state!r}，不接受 {action}。"
                "终态的计划是审计记录，不允许再改 —— 需要新计划请另起一条执行。"
            )

    def _reject_if_busy(self, action: str) -> None:
        if self._pending is not None or self._active_child is not None:
            raise ValueError(
                f"已有动作在进行中（pending={self._pending}, child={self._active_child}），"
                f"不接受 {action}。并发的演练/执行会让"
                "「这次演练验证的是哪一版」变成说不清的状态。"
            )

    @staticmethod
    def _require_text(value: str, field_name: str) -> str:
        if not value or not value.strip():
            raise ValueError(
                f"{field_name} 不能为空。灾备计划的每一次变动都要有署名与理由 —— "
                "空着等于让以后读审计链的人自己猜。"
            )
        return value.strip()

    # ── ① 修订 ────────────────────────────────────────────────────────────

    @workflow.update(name="revise_plan")
    async def revise_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        """人工修订计划正文，产生新版本。

        修订**作废**已有的批准与演练结论，并把状态退回 draft。
        这不是保守，是正确：改过的计划没有被批准过，也没有被演练过。
        """
        body = args["body"]
        author = args["author"].strip()
        reason = args["reason"].strip()

        invalidated_approval = self._approved_version
        invalidated_drill = self._drilled_version

        # ⚠️ 到第一个 await 之前的这段是**同步**的（Temporal workflow 是
        # 单线程协作式调度），所以版本号自增与在途标记不会被另一个 handler
        # 插进来。顺序在这里是承重的：先置位，再 await。
        self._revise_in_flight = True
        self._version += 1
        self._body = body
        digest = _sha256(body)

        try:
            # 写一个**版本化、不可覆盖**的 S3 键。执行路径（FetchPlanWorkflow）
            # 仍按 plan_ref 从 S3 取正文，所以这一步是它的供给侧。
            put = await workflow.execute_activity(
                put_plan_version,
                PlanVersionInput(
                    plan_id=self._plan_id, version=self._version, body=body, sha256=digest
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_WRITE_RETRY,
            )
        finally:
            self._revise_in_flight = False

        rev = PlanRevision(
            version=self._version,
            author=author,
            reason=reason,
            sha256=digest,
            s3_key=put.detail.get("s3_key", ""),
            origin="human",
            at=workflow.now().isoformat(),
            invalidated_approval_of=invalidated_approval,
            invalidated_drill_of=invalidated_drill,
        )
        self._revisions.append(rev)

        # 修订让此前的批准与演练结论失效。
        self._approved_version = None
        self._drilled_version = None
        self._state = STATE_DRAFT
        self._tag()

        workflow.logger.info(
            "计划 %s 修订到 v%d（%s）：%s", self._plan_id, self._version, author, reason
        )
        return {
            "accepted": True,
            "version": self._version,
            "sha256": digest,
            "s3_key": rev.s3_key,
            "state": self._state,
            "invalidated_approval_of": invalidated_approval,
            "invalidated_drill_of": invalidated_drill,
            "note": (
                "修订已作废此前的批准与演练结论 —— 改过的计划没有被批准过，也没有被演练过。"
                if (invalidated_approval or invalidated_drill)
                else None
            ),
        }

    @revise_plan.validator
    def _validate_revise(self, args: dict[str, Any]) -> None:
        self._reject_if_terminal("修订")
        self._reject_if_busy("修订")
        for key in ("body", "author", "reason"):
            if key not in args:
                raise ValueError(f"缺少必填字段 {key!r}。必填：body / author / reason。")
        self._require_text(args["author"], "author")
        self._require_text(args["reason"], "reason")

        body = args["body"]
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body 不能为空。")
        size = len(body.encode("utf-8"))
        if size > MAX_BODY_BYTES:
            raise ValueError(
                f"正文 {size} 字节，超过上限 {MAX_BODY_BYTES}。"
                "正文进 history 是为了审计自洽，但 history 会累积每一版 —— "
                "过大的正文会把这条执行推向服务端的 history 大小限制，"
                "而那个后果是执行被终止。请精简，或把大段附件另存并在正文里引用。"
            )
        if _sha256(body) == _sha256(self._body):
            raise ValueError(
                f"正文与当前 v{self._version} 完全相同，拒绝记为新版本。"
                "一个什么都没改的版本会污染审计链 —— 读的人会以为这里发生过一次修订。"
            )
        # 修订 handler 是 async 的（要写 S3），所以它会让出控制权。
        # 第二个修订请求的 validator 可能在第一个还在 await 时跑 —— 若不挡，
        # 两次修订会交错，revisions 的顺序与版本号对不上。
        # validator 里只**读**状态（读是允许的，写不是），在途标记由 handler
        # 在第一个 await **之前**同步置位，所以这个检查不会漏。
        if self._revise_in_flight:
            raise ValueError(
                "已有一次修订正在写入，拒绝并发修订。"
                "两次交错的修订会让审计链里的版本顺序与内容对不上。"
            )

    # ── ② 审批 ────────────────────────────────────────────────────────────

    @workflow.update(name="approve_plan")
    def approve_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        """批准**指定版本**。版本号必须显式给，且必须等于当前版本。"""
        approver = args["approver"].strip()
        note = args.get("note", "").strip()
        version = int(args["version"])

        last_reviser = self._revisions[-1].author if self._revisions else ""
        same = bool(last_reviser) and last_reviser == approver

        self._approved_version = version
        self._state = STATE_APPROVED
        self._approvals.append(
            PlanApproval(
                version=version,
                approver=approver,
                note=note,
                at=workflow.now().isoformat(),
                same_as_last_reviser=same,
            )
        )
        self._tag()

        return {
            "accepted": True,
            "approved_version": version,
            "state": self._state,
            # 记录而不阻止 —— 见文件头「刻意不做成闸门的一条」。
            "same_person_revised_and_approved": same,
            "identity_is_authenticated": False,
            "next": "start_drill —— 真执行要求演练覆盖被批准的这一版。",
        }

    @approve_plan.validator
    def _validate_approve(self, args: dict[str, Any]) -> None:
        self._reject_if_terminal("审批")
        self._reject_if_busy("审批")
        for key in ("version", "approver"):
            if key not in args:
                raise ValueError(f"缺少必填字段 {key!r}。必填：version / approver。")
        self._require_text(args["approver"], "approver")

        if self._version == 0:
            raise ValueError("计划还没有任何版本，无从批准。")
        version = int(args["version"])
        if version != self._version:
            # 这条拒绝挡的是**过期审批**：人在 UI 上看着 v2 点了批准，
            # 而这期间别人已经改到了 v3。不显式给版本号就永远发现不了。
            raise ValueError(
                f"要批准的 v{version} 不是当前版本 v{self._version}。"
                "计划在你审阅期间被改过了 —— 请重新读 current_body 再批准。"
                "这正是审批必须显式带版本号的原因。"
            )
        if self._approved_version == version:
            raise ValueError(f"v{version} 已被批准，不重复记录。")

    # ── ③ 演练 ────────────────────────────────────────────────────────────

    @workflow.update(name="start_drill")
    def start_drill(self, args: dict[str, Any]) -> dict[str, Any]:
        """对已批准的那一版跑一次演练（全程 dry_run）。

        本 handler **只置位**，演练由主循环去跑 —— 否则调用方要一直等到
        整场演练结束才拿到响应。
        """
        requested_by = args["requested_by"].strip()
        self._pending = {
            "action": "drill",
            "version": self._approved_version,
            "requested_by": requested_by,
        }
        return {
            "accepted": True,
            "drilling_version": self._approved_version,
            "child_workflow_id": self._child_id("drill", self._approved_version),
            "note": (
                "演练会走到数据库裁决那个决策点并**等人裁决** —— 这是刻意的，"
                "审批环节本身也要被演练。裁决要发给上面那个子执行，不是发给本计划。"
            ),
        }

    @start_drill.validator
    def _validate_drill(self, args: dict[str, Any]) -> None:
        self._reject_if_terminal("演练")
        self._reject_if_busy("演练")
        if "requested_by" not in args:
            raise ValueError("缺少必填字段 'requested_by'。")
        self._require_text(args["requested_by"], "requested_by")
        if self._state != STATE_APPROVED or self._approved_version is None:
            raise ValueError(
                f"当前状态 {self._state!r}，只有已批准（approved）的计划才能演练。"
                "先 approve_plan —— 演练一份没人批准的计划，产出的结论没有归属。"
            )

    # ── ④ 授权真执行：唯一的硬闸门 ────────────────────────────────────────

    @workflow.update(name="authorize_execution")
    def authorize_execution(self, args: dict[str, Any]) -> dict[str, Any]:
        """授权按本计划**真执行**切换。"""
        self._pending = {
            "action": "execute",
            "version": self._approved_version,
            "authorized_by": args["authorized_by"].strip(),
            "execute_steps": list(args["execute_steps"]),
        }
        return {
            "accepted": True,
            "executing_version": self._approved_version,
            "execute_steps": self._pending["execute_steps"],
            "child_workflow_id": self._child_id("exec", self._approved_version),
            "note": "裁决要发给上面那个子执行。未列入 execute_steps 的步骤仍走 dry_run。",
        }

    @authorize_execution.validator
    def _validate_authorize(self, args: dict[str, Any]) -> None:
        self._reject_if_terminal("执行授权")
        self._reject_if_busy("执行授权")
        for key in ("authorized_by", "execute_steps"):
            if key not in args:
                raise ValueError(
                    f"缺少必填字段 {key!r}。必填：authorized_by / execute_steps。"
                )
        self._require_text(args["authorized_by"], "authorized_by")

        steps = args["execute_steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError(
                "execute_steps 不能为空。放行是**按步骤逐个给**的："
                "没写进来的步骤一律 dry_run。一个空列表意味着这次授权什么都不会真执行，"
                "却留下一条「已授权执行」的审计记录 —— 那比拒绝更糟。"
            )

        # ★ 硬闸门：演练必须覆盖被批准的那一版，且它必须仍是当前版本。
        if self._state != STATE_DRILLED:
            raise ValueError(
                f"当前状态 {self._state!r}，只有演练完成（drilled）的计划才能授权真执行。"
            )
        if self._approved_version != self._version:
            raise ValueError(
                f"被批准的是 v{self._approved_version}，当前已是 v{self._version} —— "
                "计划在批准之后被改过了，请重新批准并重新演练。"
            )
        if self._drilled_version != self._approved_version:
            raise ValueError(
                f"演练覆盖的是 v{self._drilled_version}，被批准的是 v{self._approved_version}。"
                "拿一次针对别的版本的演练去支撑本次执行，等于没演练 —— "
                "这正是灾备计划最容易出现的漂移：演练完发现问题、改了计划，"
                "然后带着「我们演练过」的错觉去真切换。"
            )
        last = self._drills[-1] if self._drills else None
        if last is None or last.ok is not True:
            reason = "没有演练记录" if last is None else f"演练结论 ok={last.ok}"
            raise ValueError(
                f"{reason}，不予授权。演练未通过或结论无法判断时，"
                "「未核实」不许当成「已核实」—— 请先把演练跑到通过。"
            )

    # ── 收口 ──────────────────────────────────────────────────────────────

    @workflow.update(name="close_plan")
    def close_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        """归档这份计划（不再接受修改）。"""
        self._closed_reason = f"{args['author'].strip()}: {args['reason'].strip()}"
        self._state = STATE_CLOSED
        self._pending = {"action": "close"}
        self._tag()
        return {"accepted": True, "state": self._state, "reason": self._closed_reason}

    @close_plan.validator
    def _validate_close(self, args: dict[str, Any]) -> None:
        self._reject_if_terminal("归档")
        self._reject_if_busy("归档")
        for key in ("author", "reason"):
            if key not in args:
                raise ValueError(f"缺少必填字段 {key!r}。")
            self._require_text(args[key], key)

    # ── 查询 ──────────────────────────────────────────────────────────────

    @workflow.query(name="plan_state")
    def plan_state(self) -> dict[str, Any]:
        """一眼看清「这份计划现在能做什么、还缺什么」。"""
        last_approval = self._approvals[-1] if self._approvals else None
        return {
            "plan_id": self._plan_id,
            "state": self._state,
            "current_version": self._version,
            "current_sha256": _sha256(self._body) if self._body else None,
            "approved_version": self._approved_version,
            "drilled_version": self._drilled_version,
            # 这三个布尔是硬闸门的分解 —— 让调用方在发 update 之前就知道会不会被拒。
            "drill_covers_approved_version": (
                self._drilled_version is not None
                and self._drilled_version == self._approved_version
            ),
            "approved_version_is_current": self._approved_version == self._version,
            "ready_to_authorize_execution": (
                self._state == STATE_DRILLED
                and self._approved_version == self._version
                and self._drilled_version == self._approved_version
                and bool(self._drills)
                and self._drills[-1].ok is True
            ),
            # 记录而不阻止。
            "same_person_revised_and_approved": (
                last_approval.same_as_last_reviser if last_approval else None
            ),
            "identity_is_authenticated": False,
            "active_child_workflow_id": self._active_child,
            "awaiting_ruling_on": self._active_child,
            "revision_count": len(self._version_list()),
            "drill_count": len(self._drills),
        }

    @workflow.query(name="revision_log")
    def revision_log(self) -> dict[str, Any]:
        """完整审计链，不必翻 history 就能读。"""
        return {
            "plan_id": self._plan_id,
            "revisions": [r.__dict__ for r in self._revisions],
            "approvals": [a.__dict__ for a in self._approvals],
            "drills": [
                {**d.__dict__, "steps": [s.__dict__ for s in d.steps]} for d in self._drills
            ],
            "execution": (
                {**self._execution.__dict__,
                 "steps": [s.__dict__ for s in self._execution.steps]}
                if self._execution
                else None
            ),
        }

    @workflow.query(name="current_body")
    def current_body(self) -> dict[str, Any]:
        """当前正文原文 —— 审批前要读的就是这个。"""
        return {
            "plan_id": self._plan_id,
            "version": self._version,
            "sha256": _sha256(self._body) if self._body else None,
            "body": self._body,
        }

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _version_list(self) -> list[int]:
        return [r.version for r in self._revisions]

    def _child_id(self, kind: str, version: int | None) -> str:
        return f"{workflow.info().workflow_id}-{kind}-v{version}"

    def _tag(self) -> None:
        workflow.upsert_search_attributes(
            [
                _SA_PLAN_ID.value_set(self._plan_id),
                _SA_PLAN_VERSION.value_set(self._version),
                _SA_PLAN_STATE.value_set(self._state),
            ]
        )

    async def _run_failover_child(
        self, kind: str, version: int, *, dry_run: bool, execute_steps: list[str]
    ) -> DrillRecord:
        """跑一条 DrFailoverWorkflow 子执行并把结论记下来。"""
        assert self._cfg is not None
        child_id = self._child_id(kind, version)
        rec = DrillRecord(
            version=version, child_workflow_id=child_id, at=workflow.now().isoformat()
        )
        self._active_child = child_id
        try:
            result: FailoverResult = await workflow.execute_child_workflow(
                DrFailoverWorkflow.run,
                FailoverInput(
                    # 版本化、不可覆盖的引用 —— 执行路径取的正文与审计链里
                    # 那一版的 sha256 对得上，不是"某个叫这个名字的对象"。
                    plan_ref=f"{self._plan_id}/v{version}",
                    dry_run=dry_run,
                    promote_targets=list(self._cfg.promote_targets),
                    execute_steps=list(execute_steps),
                ),
                id=child_id,
                task_queue=self._cfg.failover_task_queue,
            )
            rec.steps = list(result.steps)
            rec.inconclusive = [
                f"{s.step}: {s.inconclusive_reason}"
                for s in result.steps
                if s.verified is None and s.inconclusive_reason
            ]
            # ok 是三态的：任何一步 verified is None 就是**无法判断**，
            # 不是通过。把 inconclusive 记成 pass 是本项目反复踩过的坑。
            if result.aborted:
                rec.ok = False
                rec.failure = f"裁决为 abort（{result.database_decision}）"
            elif any(s.verified is None for s in result.steps):
                rec.ok = None
            else:
                rec.ok = all(s.verified for s in result.steps)
        except Exception as e:  # noqa: BLE001 —— 子执行失败要落进审计链，不能吞掉
            rec.ok = False
            rec.failure = f"{type(e).__name__}: {e}"
            workflow.logger.warning("%s v%d 子执行失败：%s", kind, version, rec.failure)
        finally:
            self._active_child = None
        return rec

    # ── 主流程 ────────────────────────────────────────────────────────────

    @workflow.run
    async def run(self, args: PlanInput) -> PlanResult:
        self._cfg = args
        self._plan_id = args.plan_id

        # v1 由生成器给出，不经审批环节 —— 它就是"待审核的自动生成初版"。
        self._version = 1
        self._body = args.body
        digest = _sha256(args.body)
        put = await workflow.execute_activity(
            put_plan_version,
            PlanVersionInput(
                plan_id=self._plan_id, version=1, body=args.body, sha256=digest
            ),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_WRITE_RETRY,
        )
        self._revisions.append(
            PlanRevision(
                version=1,
                author=args.author,
                reason=args.reason,
                sha256=digest,
                s3_key=put.detail.get("s3_key", ""),
                origin="generated",
                at=workflow.now().isoformat(),
            )
        )
        self._state = STATE_DRAFT
        self._tag()

        idle = timedelta(hours=args.idle_timeout_hours)
        while self._state not in _TERMINAL:
            # 等一个人工动作。空闲超时自动归档，避免僵尸 RUNNING 长期堆积。
            try:
                await workflow.wait_condition(
                    lambda: self._pending is not None, timeout=idle
                )
            except asyncio.TimeoutError:
                self._state = STATE_CLOSED
                self._closed_reason = (
                    f"空闲超过 {args.idle_timeout_hours} 小时自动归档（无人推进）"
                )
                self._tag()
                break

            pending, self._pending = self._pending, None
            action = pending["action"]

            if action == "close":
                break

            if action == "drill":
                version = pending["version"]
                self._state = STATE_DRILLING
                self._tag()
                rec = await self._run_failover_child(
                    "drill", version, dry_run=True, execute_steps=[]
                )
                self._drills.append(rec)
                if rec.ok is True:
                    self._drilled_version = version
                    self._state = STATE_DRILLED
                else:
                    # 演练没通过（或无法判断）就退回 approved —— 不把
                    # drilled_version 置上，硬闸门因此仍然挡着真执行。
                    self._state = STATE_APPROVED
                self._tag()

            elif action == "execute":
                version = pending["version"]
                self._state = STATE_EXECUTING
                self._tag()
                self._execution = await self._run_failover_child(
                    "exec", version, dry_run=False,
                    execute_steps=pending["execute_steps"],
                )
                self._state = STATE_EXECUTED
                self._tag()

        return PlanResult(
            plan_id=self._plan_id,
            final_state=self._state,
            final_version=self._version,
            revisions=self._revisions,
            approvals=self._approvals,
            drills=self._drills,
            execution=self._execution,
            closed_reason=self._closed_reason,
        )

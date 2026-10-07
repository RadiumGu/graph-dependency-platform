"""arc_bridge.py — Temporal 起一次 ARC Region switch 执行、盯着它、再用独立来源核实结果。

分工（2026-10-07 小验证，见 docs/runbooks/dr-arc-temporal-hybrid-design.md）：

    Temporal  前置探针（全部 PASS 才起）→ StartPlanExecution → 轮询状态
              → 后置探针（与 ARC 不同的数据源）→ 从 CloudTrail 查是谁批准的 → 落记录
    ARC       第一步人工审批（审批角色要求 MFA）→ 执行可逆的准备步骤
    人        在 ARC 里批准

为什么 worker 可以调用 StartPlanExecution（test_128 唯一放行的一个写调用）：
起一个 ARC 执行**不等于做了任何变更** —— 计划的每个工作流第一步都是
ManualApproval，审批角色只有带 MFA 的人能扮演（test_131 守着这两条）。
worker 角色只有 Start/Get/List，没有 Approve/Cancel/Update。

为什么要后置探针：ARC 报 completed 只说明它的执行块做完了。Lambda 看的是
ASG 的 InService 数；后置探针看的是 k8s API 里 Ready 的节点 —— 一台 EC2
InService 但 kubelet 没注册进集群，ARC 会报成功，探针会报 FAIL。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities import ExecutionRecordInput, put_execution_record
    from probe_catalog import PASS, PROBES, UNKNOWN, activity_name, check_params
    from probes import ProbeInput, ProbeResult

_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-2"

#: ARC 执行的终态。completedMonitoringApplicationHealth 是「步骤都做完、在看告警」，
#: 对本工作流来说也是终态。
TERMINAL = {
    "completed", "completedWithExceptions", "canceled", "planExecutionTimedOut",
    "failed", "completedMonitoringApplicationHealth",
}
#: 只有这两种算 ARC 侧成功；completedWithExceptions 意味着有步骤被跳过或失败。
ARC_OK = {"completed", "completedMonitoringApplicationHealth"}


def _arc():
    import boto3  # noqa: PLC0415 —— 只在 activity 里导入，workflow 沙箱不碰它

    return boto3.client("arc-region-switch", region_name=_REGION)


# ── activities ────────────────────────────────────────────────────────────


@dataclass
class ArcStartInput:
    plan_arn: str
    target_region: str
    action: str
    mode: str
    comment: str
    #: 用 workflow id 做幂等键：activity 重试时 ARC 不会起第二个执行。
    client_token: str


@dataclass
class ArcStartResult:
    execution_id: str
    plan_version: str = ""
    activate_region: str = ""
    deactivate_region: str = ""


def idem_tag(token: str) -> str:
    """写进 ARC 执行备注里的幂等标记。重试时按它找回已起的执行。"""
    return f"[temporal {token}]"


def find_existing(items: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    tag = idem_tag(token)
    for it in items:
        if tag in (it.get("comment") or ""):
            return it
    return None


def _input_members(client: Any, op: str) -> set[str]:
    return set(client.meta.service_model.operation_model(op).input_shape.members)


@activity.defn(name="arc.start_plan_execution")
async def arc_start_plan_execution(inp: ArcStartInput) -> ArcStartResult:
    """起一个 ARC 执行，重试安全。

    ⚠️ 2026-10-07 首次真跑时失败：worker 钉的 boto3==1.40.47 的 ARC 模型里**没有
    clientToken**（本地 1.43 有，所以测试全绿）。幂等因此不能只靠 clientToken：
      1. 先按备注里的幂等标记找已起的执行 —— 有就直接返回，不再起第二个
      2. 运行时 SDK 支持 clientToken 才传
    """
    c = _arc()
    existing = await asyncio.to_thread(c.list_plan_executions, planArn=inp.plan_arn)
    hit = find_existing(existing.get("items", []), inp.client_token)
    if hit:
        return ArcStartResult(execution_id=hit["executionId"], plan_version=hit.get("version", ""))

    kw: dict[str, Any] = dict(
        planArn=inp.plan_arn, targetRegion=inp.target_region, action=inp.action,
        mode=inp.mode, comment=inp.comment[:1024],
    )
    if "clientToken" in _input_members(c, "StartPlanExecution"):
        kw["clientToken"] = inp.client_token[:128]
    r = await asyncio.to_thread(c.start_plan_execution, **kw)
    return ArcStartResult(
        execution_id=r["executionId"],
        plan_version=r.get("planVersion", ""),
        activate_region=r.get("activateRegion", ""),
        deactivate_region=r.get("deactivateRegion", ""),
    )


@dataclass
class ArcGetInput:
    plan_arn: str
    execution_id: str


@dataclass
class ArcExecutionView:
    state: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    version: str = ""
    actual_recovery_time: str = ""


@activity.defn(name="arc.get_plan_execution")
async def arc_get_plan_execution(inp: ArcGetInput) -> ArcExecutionView:
    r = await asyncio.to_thread(
        _arc().get_plan_execution, planArn=inp.plan_arn, executionId=inp.execution_id
    )
    return ArcExecutionView(
        state=r["executionState"],
        steps=[{"name": s.get("name"), "status": s.get("status")} for s in r.get("stepStates", [])],
        version=r.get("version", ""),
        actual_recovery_time=r.get("actualRecoveryTime", "") or "",
    )


@activity.defn(name="arc.list_execution_events")
async def arc_list_execution_events(inp: ArcGetInput) -> list[dict[str, Any]]:
    c = _arc()
    out: list[dict[str, Any]] = []
    token = None
    while True:
        kw = {"planArn": inp.plan_arn, "executionId": inp.execution_id}
        if token:
            kw["nextToken"] = token
        r = await asyncio.to_thread(c.list_plan_execution_events, **kw)
        for e in r.get("items", []):
            out.append({
                "at": str(e.get("timestamp", "")), "type": e.get("type"),
                "step": e.get("stepName"), "block": e.get("executionBlockType"),
                "error": e.get("error"), "description": e.get("description"),
            })
        token = r.get("nextToken")
        if not token:
            return out


@dataclass
class ApproverLookupInput:
    execution_id: str
    since_iso: str


@activity.defn(name="arc.lookup_approver")
async def arc_lookup_approver(inp: ApproverLookupInput) -> list[dict[str, Any]]:
    """从 CloudTrail 查是谁调用了 ApprovePlanExecutionStep。

    ARC 自己的执行事件里**没有**审批人身份。CloudTrail 里是真实的 IAM 身份
    （扮演审批角色时的会话名、是否带 MFA）—— 这是与 ARC 不同的数据源。
    CloudTrail 有投递延迟，找不到时返回空列表，由调用方决定等多久。
    """
    import json  # noqa: PLC0415

    import boto3  # noqa: PLC0415

    ct = boto3.client("cloudtrail", region_name=_REGION)
    start = datetime.fromisoformat(inp.since_iso)
    r = await asyncio.to_thread(
        ct.lookup_events,
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "ApprovePlanExecutionStep"}],
        StartTime=start,
        EndTime=datetime.now(timezone.utc),
        MaxResults=50,
    )
    hits = []
    for ev in r.get("Events", []):
        d = json.loads(ev.get("CloudTrailEvent", "{}"))
        req = d.get("requestParameters") or {}
        if req.get("executionId") != inp.execution_id:
            continue
        ui = d.get("userIdentity") or {}
        attrs = (ui.get("sessionContext") or {}).get("attributes") or {}
        hits.append({
            "at": d.get("eventTime"),
            "principal": ui.get("arn"),
            "mfa": attrs.get("mfaAuthenticated"),
            "step": req.get("stepName"),
            "approval": req.get("approval"),
            "source_ip": d.get("sourceIPAddress"),
            "error": d.get("errorCode"),
        })
    return hits


ARC_ACTIVITIES = [
    arc_start_plan_execution,
    arc_get_plan_execution,
    arc_list_execution_events,
    arc_lookup_approver,
]


# ── workflow ──────────────────────────────────────────────────────────────


@dataclass
class ArcRunInput:
    plan_arn: str
    target_region: str
    action: str = "activate"
    mode: str = "graceful"
    comment: str = ""
    requested_by: str = ""
    #: [{"probe": "<name>", "params": {...}}]。前置全部 PASS 才起 ARC 执行。
    prechecks: list[dict[str, Any]] = field(default_factory=list)
    #: ARC 报成功之后，用与它不同的数据源再核实一遍。
    postchecks: list[dict[str, Any]] = field(default_factory=list)
    poll_seconds: int = 20
    #: 等 ARC（包括等人审批）的上限。到了就停在原地报告，**不替人做任何事**。
    max_wait_hours: float = 2.0
    #: 后置探针允许重试几次（节点注册进集群要时间）。
    postcheck_attempts: int = 10
    postcheck_interval_seconds: int = 30
    #: 等 CloudTrail 投递审批事件的上限。
    lookup_approver_minutes: int = 20
    export_record: bool = True


_READ_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)
_PROBE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)
#: 带幂等键，重试安全；但次数要少 —— 起不来就让人看，不要反复敲。
_START_RETRY = RetryPolicy(initial_interval=timedelta(seconds=3), maximum_attempts=3)


@workflow.defn
class ArcPlanExecutionWorkflow:
    def __init__(self) -> None:
        self._phase = "init"
        self._args: ArcRunInput | None = None
        self._execution_id = ""
        self._arc_state = ""
        self._awaiting_step: str | None = None
        self._timeline: list[dict[str, Any]] = []
        self._pre: list[dict[str, Any]] = []
        self._post: list[dict[str, Any]] = []
        self._approvers: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._verdict = ""

    @workflow.query
    def arc_state(self) -> dict[str, Any]:
        return {
            "phase": self._phase,
            "verdict": self._verdict,
            "execution_id": self._execution_id,
            "arc_state": self._arc_state,
            # 非空 = ARC 正在等人批准这一步（人去 ARC 里批，Temporal 不能批）
            "awaiting_approval_step": self._awaiting_step,
            "timeline": self._timeline,
            "prechecks": self._pre,
            "postchecks": self._post,
            "approvers": self._approvers,
        }

    def _mark(self, what: str, **kw: Any) -> None:
        self._timeline.append({"at": workflow.now().isoformat(), "what": what, **kw})

    async def _probe(self, ref: dict[str, Any]) -> dict[str, Any]:
        name = ref["probe"]
        try:
            r: ProbeResult = await workflow.execute_activity(
                activity_name(name),
                ProbeInput(params=dict(ref.get("params", {})), plan_ref="arc-bridge"),
                result_type=ProbeResult,
                start_to_close_timeout=timedelta(seconds=PROBES[name].timeout_seconds),
                retry_policy=_PROBE_RETRY,
            )
            return asdict(r)
        except ActivityError as e:
            return {"probe": name, "verdict": UNKNOWN, "reason": f"探针本身没跑成：{e.cause or e}"}

    @workflow.run
    async def run(self, args: ArcRunInput) -> dict[str, Any]:
        self._args = args
        try:
            return await self._body(args)
        finally:
            await self._export()

    async def _body(self, args: ArcRunInput) -> dict[str, Any]:
        # ── 0. 输入校验：探针名与参数在起任何东西之前就核对 ─────────────────
        bad = []
        for ref in args.prechecks + args.postchecks:
            if ref.get("probe") not in PROBES:
                bad.append(f"未知探针 {ref.get('probe')!r}")
            else:
                bad += [f"{ref['probe']}: {x}" for x in check_params(ref["probe"], ref.get("params", {}))]
        if not args.postchecks:
            bad.append("没有后置探针 —— 只信 ARC 自己报的 completed，正是本工作流要避免的")
        if bad:
            self._phase, self._verdict = "refused", "invalid-input"
            self._mark("refused", reasons=bad)
            return self.arc_state()

        # ── 1. 前置探针：任何一项不是 PASS 就不起 ARC 执行 ────────────────
        self._phase = "prechecks"
        for ref in args.prechecks:
            self._pre.append(await self._probe(ref))
        not_pass = [p for p in self._pre if p.get("verdict") != PASS]
        if not_pass:
            self._phase, self._verdict = "refused", "precheck-not-pass"
            self._mark("refused", reasons=[f"{p['probe']}={p.get('verdict')} {p.get('reason','')}" for p in not_pass])
            return self.arc_state()

        # ── 2. 起 ARC 执行（第一步就是人工审批，起执行本身不做任何变更）────
        self._phase = "starting"
        started_at = workflow.now()
        wid = workflow.info().workflow_id
        try:
            res: ArcStartResult = await workflow.execute_activity(
                arc_start_plan_execution,
                ArcStartInput(
                    plan_arn=args.plan_arn, target_region=args.target_region,
                    action=args.action, mode=args.mode,
                    # 幂等标记必须在备注**开头**：备注会被截到 1024 字符
                    comment=f"{idem_tag(wid)} {args.requested_by}: {args.comment}",
                    client_token=wid,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_START_RETRY,
            )
        except ActivityError as e:
            # 起不来是一个要人看的结论，不是 workflow 崩溃 —— 记录照样导出
            self._phase, self._verdict = "done", "arc-start-failed"
            self._mark("arc-start-failed", error=str(e.cause or e)[:500])
            return self.arc_state()
        self._execution_id = res.execution_id
        self._mark("arc-started", execution_id=res.execution_id, plan_version=res.plan_version,
                   activate=res.activate_region, deactivate=res.deactivate_region)

        # ── 3. 轮询，直到终态或超时 ───────────────────────────────────────
        self._phase = "waiting-arc"
        deadline = workflow.now() + timedelta(hours=args.max_wait_hours)
        last_sig = None
        while True:
            v: ArcExecutionView = await workflow.execute_activity(
                arc_get_plan_execution, ArcGetInput(args.plan_arn, res.execution_id),
                result_type=ArcExecutionView,
                start_to_close_timeout=timedelta(seconds=30), retry_policy=_READ_RETRY,
            )
            self._arc_state = v.state
            pending = [s["name"] for s in v.steps if s.get("status") == "pendingApproval"]
            self._awaiting_step = pending[0] if pending else None
            sig = (v.state, tuple((s["name"], s["status"]) for s in v.steps))
            if sig != last_sig:
                self._mark("arc-state", state=v.state, steps=v.steps)
                last_sig = sig
            if v.state in TERMINAL:
                break
            if workflow.now() >= deadline:
                # 停在原地，不取消 ARC 执行 —— 那是人的决定
                self._phase, self._verdict = "stalled", "arc-not-finished-in-time"
                self._mark("stalled", arc_state=v.state, awaiting=self._awaiting_step)
                return self.arc_state()
            await workflow.sleep(timedelta(seconds=args.poll_seconds))

        self._events = await workflow.execute_activity(
            arc_list_execution_events, ArcGetInput(args.plan_arn, res.execution_id),
            start_to_close_timeout=timedelta(seconds=60), retry_policy=_READ_RETRY,
        )

        # ── 4. 谁批准的：CloudTrail（与 ARC 不同的数据源）──────────────────
        # 批准和拒绝都要查（拒绝会让 ARC 执行变成 canceled，同样要知道是谁）；
        # 从没走到审批那一步就不查，免得白等 CloudTrail。
        reached_approval = any(e.get("type") == "stepPendingApproval" for e in self._events)
        if reached_approval:
            self._phase = "who-approved"
            tries = max(1, args.lookup_approver_minutes)
            for i in range(tries):
                self._approvers = await workflow.execute_activity(
                    arc_lookup_approver,
                    ApproverLookupInput(res.execution_id, started_at.isoformat()),
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_READ_RETRY,
                )
                if self._approvers or i == tries - 1:
                    break
                await workflow.sleep(timedelta(minutes=1))
            if not self._approvers:
                self._mark("approver-not-found", note="CloudTrail 在等待上限内没有投递审批事件（不是没人批）")

        if self._arc_state not in ARC_OK:
            self._phase, self._verdict = "done", f"arc-{self._arc_state}"
            return self.arc_state()

        # ── 5. 后置探针：ARC 说做完了，用另一个数据源核实 ──────────────────
        self._phase = "postchecks"
        for attempt in range(1, args.postcheck_attempts + 1):
            self._post = [await self._probe(ref) for ref in args.postchecks]
            if all(p.get("verdict") == PASS for p in self._post):
                break
            if attempt < args.postcheck_attempts:
                await workflow.sleep(timedelta(seconds=args.postcheck_interval_seconds))
        ok = all(p.get("verdict") == PASS for p in self._post)
        self._phase = "done"
        # 两种结论分开写：ARC 成功但核实失败，是这套设计要抓的那一类
        self._verdict = "verified" if ok else "arc-completed-but-postcheck-failed"
        self._mark("done", verdict=self._verdict)
        return self.arc_state()

    async def _export(self) -> None:
        args = self._args
        if args is None or not args.export_record:
            return
        info = workflow.info()
        record = {
            "kind": "arc-plan-execution",
            "workflow_id": info.workflow_id, "run_id": info.run_id,
            "input": asdict(args), **self.arc_state(), "arc_events": self._events,
        }
        try:
            await workflow.execute_activity(
                put_execution_record,
                ExecutionRecordInput(
                    plan_id="arc-" + args.plan_arn.rsplit("/", 1)[-1].split(":")[0],
                    record_id=f"{info.workflow_id}-{info.run_id[:8]}", record=record,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=5),
            )
        except ActivityError as e:
            workflow.logger.warning("执行记录没写成：%s", e)

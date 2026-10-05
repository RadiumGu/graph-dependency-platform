"""runbook_workflow.py — `DrRunbookWorkflow`：人执行，Temporal 核实。

取代 2026-10-05 之前的 `DrFailoverWorkflow` + 四个子 workflow。

## 两种模式

**rehearsal（演练）**：不等任何人。把 runbook 里**每一个**探针真跑一次，回答两件事：

  ① 每个探针现在都**测得到东西**吗？ —— 任何 UNKNOWN 都说明缺权限、缺网络或
     缺数据，那个探针在真切换时同样会测不到。这是演练最该抓出来的东西。
  ② 切换的**前置条件**现在成立吗？ —— 第一个人工步骤之前的 check 步骤必须全 PASS。

  后置探针此刻当然多半是 FAIL（节点还没拉起、写节点还在东京）—— 那是预期的，
  演练只要求它们**可测**，不要求它们通过。

**live（执行）**：逐步推进，每一步：

    check     跑探针 → 全 PASS 才继续
    decision  等人 `decide`（没有默认值，等不到就停住）
    manual    前置探针 → 等人执行命令并 `confirm_step` → 后置探针

  探针不全 PASS 时**停下等人** `resolve_gate`：retry / override（必须写理由）/ abort。
  override 被记录而不被阻止 —— 「可验证的强制，不可验证的记录」，与计划评审链同一原则。

## Temporal 在这里**不执行任何变更**

本文件里没有一行会调用写类 AWS API。命令原文只是展示给人的文本。
`test_128` 用 AST 扫描整个 worker 目录守着这一点。

## 记录一定会被导出

保留期 720h、归档关着，30 天后 history 就没了。所以结束时（含被取消）总是把完整
记录写到 `plans/<plan_id>/executions/`。被取消时也写 —— 2026-10-05 04:35 那次
计划被一个**没留身份**的外部取消请求关掉了，事后什么都查不到；这里至少要留下
「走到了哪一步、谁确认过什么」。
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities import (
        ExecutionRecordInput,
        LoadedPlan,
        LoadPlanInput,
        load_plan_version,
        put_execution_record,
    )
    from probe_catalog import PASS, PROBES, UNKNOWN, activity_name
    from probes import ProbeInput, ProbeResult
    from runbook import ProbeRef, Runbook, RunbookError, Step, extract_runbook

_SA_PLAN_REF = SearchAttributeKey.for_keyword("DRPlanRef")
_SA_DRY_RUN = SearchAttributeKey.for_bool("DRDryRun")
_SA_STEP_NAME = SearchAttributeKey.for_keyword("DRStepName")
#: 人确认「已执行」的 manual 步骤 —— 审计时一条查询找出所有真做过的变更：
#:   DRStepsExecuted = "promote-aurora" AND DRDryRun = false
_SA_STEPS_EXECUTED = SearchAttributeKey.for_keyword_list("DRStepsExecuted")
#: 最近一次裁决。审计时能一条查询找出「哪些执行选了允许丢数据」。
_SA_DECISION = SearchAttributeKey.for_keyword("DRDecision")

#: decision 步骤里的保留选项名：选它即停止执行。
DECISION_ABORT = "abort"

MODE_REHEARSAL = "rehearsal"
MODE_LIVE = "live"

GATE_RETRY = "retry"
GATE_OVERRIDE = "override"
GATE_ABORT = "abort"
_GATE_ACTIONS = (GATE_RETRY, GATE_OVERRIDE, GATE_ABORT)

#: 探针只读、幂等，重试是安全的；它自己不抛异常，所以重试只覆盖 worker 中途崩溃。
_PROBE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)
_READ_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)
_RECORD_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=5)


@dataclass
class RunbookRunInput:
    plan_id: str
    version: int
    #: 评审链里那一版的摘要 —— load_plan_version 按内容重新计算并核对。
    sha256: str
    mode: str
    requested_by: str
    #: 等人的上限（小时，可为小数）。超时**不放行**，停在原地并导出记录。
    human_wait_hours: float = 24
    export_record: bool = True


@dataclass
class RunbookRunResult:
    #: completed / aborted / stalled / cancelled / invalid
    status: str
    #: 三态。None = 无法判断（有 UNKNOWN，或有人 override 过）。
    ok: bool | None
    mode: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    record_key: str | None = None


@workflow.defn(name="DrRunbookWorkflow")
class DrRunbookWorkflow:
    def __init__(self) -> None:
        self._args: RunbookRunInput | None = None
        self._rb: Runbook | None = None
        self._status = "starting"
        self._cursor: int | None = None
        #: 当前在等什么：{"type": "confirm"|"decision"|"gate", "step_id": ..., ...}
        self._awaiting: dict[str, Any] | None = None
        self._reply: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._findings: list[str] = []
        self._overrides: list[dict[str, Any]] = []
        self._executed: list[str] = []
        self._probe_log: list[dict[str, Any]] = []
        self._record_key: str | None = None

    # ── 人的三个动作 ──────────────────────────────────────────────────────

    @workflow.update(name="confirm_step")
    def confirm_step(self, args: dict[str, Any]) -> dict[str, Any]:
        """人回报：这一步的命令我已经执行了。接下来跑后置探针。"""
        self._reply = {
            "step_id": args["step_id"],
            "operator": args["operator"].strip(),
            "note": (args.get("note") or "").strip(),
            "at": workflow.now().isoformat(),
        }
        return {"accepted": True, "next": "后置探针开始运行；全 PASS 才会进入下一步"}

    @confirm_step.validator
    def _v_confirm(self, args: dict[str, Any]) -> None:
        self._expect("confirm", args)
        self._need_text(args, "operator")

    @workflow.update(name="decide")
    def decide(self, args: dict[str, Any]) -> dict[str, Any]:
        """人对 decision 步骤给出裁决。"""
        self._reply = {
            "step_id": args["step_id"],
            "choice": args["choice"],
            "operator": args["operator"].strip(),
            "reason": args["reason"].strip(),
            "at": workflow.now().isoformat(),
        }
        return {"accepted": True, "choice": args["choice"]}

    @decide.validator
    def _v_decide(self, args: dict[str, Any]) -> None:
        self._expect("decision", args)
        self._need_text(args, "operator")
        self._need_text(args, "reason")
        options = self._awaiting.get("options", []) if self._awaiting else []
        if args.get("choice") not in options:
            raise ValueError(f"choice 必须是 {options} 之一，实际是 {args.get('choice')!r}")

    @workflow.update(name="resolve_gate")
    def resolve_gate(self, args: dict[str, Any]) -> dict[str, Any]:
        """探针没有全 PASS 时，人决定 retry / override / abort。"""
        self._reply = {
            "step_id": args["step_id"],
            "phase": args.get("phase"),
            "action": args["action"],
            "operator": args["operator"].strip(),
            "reason": (args.get("reason") or "").strip(),
            "at": workflow.now().isoformat(),
        }
        return {"accepted": True, "action": args["action"]}

    @resolve_gate.validator
    def _v_gate(self, args: dict[str, Any]) -> None:
        self._expect("gate", args)
        self._need_text(args, "operator")
        if args.get("phase") != self._awaiting.get("phase"):
            raise ValueError(
                f"当前等待的是 {self._awaiting.get('phase')!r} 阶段的闸门，"
                f"请求里是 {args.get('phase')!r}"
            )
        if args.get("action") not in _GATE_ACTIONS:
            raise ValueError(f"action 必须是 {_GATE_ACTIONS} 之一")
        if args["action"] in (GATE_OVERRIDE, GATE_ABORT):
            # override 等于「我知道探针没过，我仍然继续」—— 那句话必须有理由。
            self._need_text(args, "reason")

    def _expect(self, kind: str, args: dict[str, Any]) -> None:
        if self._awaiting is None or self._awaiting["type"] != kind:
            now = self._awaiting["type"] if self._awaiting else "无"
            raise ValueError(f"当前不在等 {kind}（正在等：{now}）")
        if self._reply is not None:
            raise ValueError("这个等待点已经收到回复，正在处理")
        if args.get("step_id") != self._awaiting["step_id"]:
            # 拦的是「看着旧页面确认了上一步」—— 越序的确认不能落进 history。
            raise ValueError(
                f"当前步骤是 {self._awaiting['step_id']!r}，请求里是 {args.get('step_id')!r}"
            )

    @staticmethod
    def _need_text(args: dict[str, Any], key: str) -> None:
        v = args.get(key)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{key} 不能为空 —— 每一次人工动作都要有署名与理由")

    # ── 查询 ──────────────────────────────────────────────────────────────

    @workflow.query(name="runbook_state")
    def runbook_state(self) -> dict[str, Any]:
        cur: Step | None = None
        if self._rb is not None and self._cursor is not None and self._cursor < len(self._rb.steps):
            cur = self._rb.steps[self._cursor]
        return {
            "mode": self._args.mode if self._args else None,
            "status": self._status,
            "awaiting": self._awaiting,
            "current_step": (
                {
                    "index": self._cursor,
                    "id": cur.id,
                    "kind": cur.kind,
                    "title": cur.title,
                    "command": cur.command,
                    "reversible": cur.reversible,
                    "rollback": cur.rollback,
                    "irreversible_note": cur.irreversible_note,
                    "options": cur.options,
                }
                if cur
                else None
            ),
            "total_steps": len(self._rb.steps) if self._rb else None,
            "executed": list(self._executed),
            "overrides": list(self._overrides),
            "findings": list(self._findings),
            "last_probes": self._probe_log[-8:],
        }

    # ── 主流程 ────────────────────────────────────────────────────────────

    @workflow.run
    async def run(self, args: RunbookRunInput) -> RunbookRunResult:
        self._args = args
        workflow.upsert_search_attributes(
            [
                _SA_PLAN_REF.value_set(f"{args.plan_id}/v{args.version}"),
                _SA_DRY_RUN.value_set(args.mode == MODE_REHEARSAL),
            ]
        )
        try:
            ok = await self._body(args)
        except asyncio.CancelledError:
            # 被取消也要留下记录。取消请求是一次性的：接住之后仍可以调度 activity。
            self._status = "cancelled"
            self._findings.append("执行被外部取消（Temporal 不一定记录取消请求的发起者）")
            await self._export(None)
            raise
        await self._export(ok)
        return RunbookRunResult(
            status=self._status,
            ok=ok,
            mode=args.mode,
            steps=self._steps,
            findings=self._findings,
            overrides=self._overrides,
            record_key=self._record_key,
        )

    async def _body(self, args: RunbookRunInput) -> bool | None:
        if args.mode not in (MODE_REHEARSAL, MODE_LIVE):
            self._status = "invalid"
            self._findings.append(f"未知模式 {args.mode!r}")
            return False
        try:
            loaded: LoadedPlan = await workflow.execute_activity(
                load_plan_version,
                LoadPlanInput(plan_id=args.plan_id, version=args.version, sha256=args.sha256),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_READ_RETRY,
            )
        except ActivityError as e:
            self._status = "invalid"
            self._findings.append(f"取不到被批准的那一版正文：{e.cause or e}")
            return False
        try:
            rb = extract_runbook(loaded.body)
        except RunbookError as e:
            self._status = "invalid"
            self._findings.extend(e.problems)
            return False
        if rb is None:
            self._status = "invalid"
            self._findings.append("正文里没有 dr-runbook 块")
            return False
        self._rb = rb
        if rb.unresolved():
            self._status = "invalid"
            self._findings.append(f"仍有待人填写的步骤：{rb.unresolved()}")
            return False
        self._status = "running"
        if args.mode == MODE_REHEARSAL:
            return await self._rehearse(rb)
        return await self._live(rb, args)

    # ── 演练 ──────────────────────────────────────────────────────────────

    async def _rehearse(self, rb: Runbook) -> bool | None:
        first_action = next(
            (i for i, s in enumerate(rb.steps) if s.kind != "check"), len(rb.steps)
        )
        unknown = preflight_fail = False
        for i, s in enumerate(rb.steps):
            self._cursor = i
            rec: dict[str, Any] = {"step": s.id, "kind": s.kind, "title": s.title, "probes": []}
            for phase, ref in s.all_probes():
                r = await self._probe(s.id, phase, ref)
                rec["probes"].append(r)
                if r["verdict"] == UNKNOWN:
                    unknown = True
                    self._findings.append(
                        f"{s.id}/{phase}/{ref.probe} 测不到：{r['reason']} —— "
                        "真切换时它同样给不出答案"
                    )
                elif i < first_action and r["verdict"] != PASS:
                    preflight_fail = True
                    self._findings.append(
                        f"切换前置条件不成立：{s.id}/{ref.probe} = {r['verdict']}（{r['reason']}）"
                    )
            self._steps.append(rec)
        self._cursor = None
        self._status = "completed"
        if preflight_fail:
            return False
        if unknown:
            return None
        return True

    # ── 执行 ──────────────────────────────────────────────────────────────

    async def _live(self, rb: Runbook, args: RunbookRunInput) -> bool | None:
        for i, s in enumerate(rb.steps):
            self._cursor = i
            workflow.upsert_search_attributes([_SA_STEP_NAME.value_set(s.id)])
            rec: dict[str, Any] = {"step": s.id, "kind": s.kind, "title": s.title}
            self._steps.append(rec)

            if s.kind == "check":
                if not await self._gate(s, "check", s.probes, rec, args):
                    return False if self._status == "aborted" else None

            elif s.kind == "decision":
                reply = await self._await_human(
                    {"type": "decision", "step_id": s.id, "options": list(s.options)}, args
                )
                if reply is None:
                    return None
                rec["decision"] = reply
                workflow.upsert_search_attributes([_SA_DECISION.value_set(reply["choice"])])
                if reply["choice"] == DECISION_ABORT:
                    # abort 是保留的选项名：选了它就停在这里，不进入后面的任何步骤。
                    self._status = "aborted"
                    self._findings.append(
                        f"{s.id}：{reply['operator']} 裁决 abort —— {reply['reason']}"
                    )
                    return False

            elif s.kind == "manual":
                if s.pre and not await self._gate(s, "pre", s.pre, rec, args):
                    return False if self._status == "aborted" else None
                reply = await self._await_human(
                    {"type": "confirm", "step_id": s.id, "command": s.command}, args
                )
                if reply is None:
                    return None
                rec["confirmed"] = reply
                self._executed.append(s.id)
                workflow.upsert_search_attributes(
                    [_SA_STEPS_EXECUTED.value_set(list(self._executed))]
                )
                if not await self._gate(s, "post", s.post, rec, args):
                    return False if self._status == "aborted" else None

        self._cursor = None
        self._status = "completed"
        # 有人 override 过 = 至少有一个闸门没被核实，结论只能是「无法判断」。
        return None if self._overrides else True

    async def _gate(
        self, s: Step, phase: str, refs: list[ProbeRef], rec: dict[str, Any],
        args: RunbookRunInput,
    ) -> bool:
        """跑一组探针；不全 PASS 就停下等人。返回是否继续。"""
        attempts = rec.setdefault(f"{phase}_attempts", [])
        while True:
            results = [await self._probe(s.id, phase, r) for r in refs]
            attempts.append(results)
            failing = [r for r in results if r["verdict"] != PASS]
            if not failing:
                return True
            reply = await self._await_human(
                {
                    "type": "gate",
                    "step_id": s.id,
                    "phase": phase,
                    "failing": [
                        {"probe": r["probe"], "verdict": r["verdict"], "reason": r["reason"]}
                        for r in failing
                    ],
                },
                args,
            )
            if reply is None:
                return False
            rec.setdefault("gate_resolutions", []).append(reply)
            if reply["action"] == GATE_RETRY:
                continue
            if reply["action"] == GATE_OVERRIDE:
                self._overrides.append({**reply, "failing": [r["probe"] for r in failing]})
                return True
            self._status = "aborted"
            self._findings.append(f"{s.id}/{phase}：{reply['operator']} 中止 —— {reply['reason']}")
            return False

    async def _await_human(
        self, awaiting: dict[str, Any], args: RunbookRunInput
    ) -> dict[str, Any] | None:
        self._awaiting = awaiting
        self._reply = None
        try:
            await workflow.wait_condition(
                lambda: self._reply is not None,
                timeout=timedelta(hours=args.human_wait_hours),
            )
        except asyncio.TimeoutError:
            # 超时**不放行**。停在原地，留下记录，交给人重新发起。
            self._status = "stalled"
            self._findings.append(
                f"在 {awaiting['step_id']} 等 {awaiting['type']} 超过 "
                f"{args.human_wait_hours} 小时 —— 停在原地，没有替人做决定"
            )
            self._awaiting = None
            return None
        reply, self._reply, self._awaiting = self._reply, None, None
        return reply

    async def _probe(self, step_id: str, phase: str, ref: ProbeRef) -> dict[str, Any]:
        assert self._args is not None
        spec = PROBES[ref.probe]
        try:
            r: ProbeResult = await workflow.execute_activity(
                activity_name(ref.probe),
                ProbeInput(params=dict(ref.params), plan_ref=f"{self._args.plan_id}/v{self._args.version}"),
                result_type=ProbeResult,
                start_to_close_timeout=timedelta(seconds=spec.timeout_seconds),
                retry_policy=_PROBE_RETRY,
            )
            d = asdict(r)
        except ActivityError as e:
            # 探针本身没跑成（未注册、worker 崩溃耗尽重试）—— 那是「测不到」，不是 FAIL。
            d = {
                "probe": ref.probe,
                "verdict": UNKNOWN,
                "reason": f"探针本身没跑成：{e.cause or e}",
                "params": dict(ref.params),
            }
        d.update(step=step_id, phase=phase)
        self._probe_log.append(d)
        return d

    async def _export(self, ok: bool | None) -> None:
        args = self._args
        if args is None or not args.export_record:
            return
        info = workflow.info()
        record_id = f"{info.workflow_id}-{info.run_id[:8]}"
        record = {
            "workflow_id": info.workflow_id,
            "run_id": info.run_id,
            "plan_id": args.plan_id,
            "version": args.version,
            "sha256": args.sha256,
            "mode": args.mode,
            "requested_by": args.requested_by,
            "status": self._status,
            "ok": ok,
            "executed_manual_steps": self._executed,
            "overrides": self._overrides,
            "findings": self._findings,
            "steps": self._steps,
            "exported_at": workflow.now().isoformat(),
        }
        try:
            await workflow.execute_activity(
                put_execution_record,
                ExecutionRecordInput(plan_id=args.plan_id, record_id=record_id, record=record),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RECORD_RETRY,
            )
            self._record_key = f"plans/{args.plan_id}/executions/{record_id}.json"
        except ActivityError as e:
            # 导出失败不能掩盖本次结论 —— 记下来，history 里仍有全部内容。
            self._findings.append(f"执行记录导出失败：{e.cause or e}（history 仍保留 30 天）")
            workflow.logger.warning("执行记录导出失败：%s", e)


__all__ = [
    "DrRunbookWorkflow",
    "RunbookRunInput",
    "RunbookRunResult",
    "MODE_REHEARSAL",
    "MODE_LIVE",
]

"""probe_cli.py — 在首尔 worker 主机上手工操作灾备编排。

    venv/bin/python probe_cli.py list
    venv/bin/python probe_cli.py run aurora_writer_region expect_region=ap-northeast-1
    venv/bin/python probe_cli.py run alb_target_health target_group=dr-korea-petsite-tg min_healthy=1
    venv/bin/python probe_cli.py history --plan-ref adhoc
    venv/bin/python probe_cli.py start-plan --plan-id 20261005-tokyo-to-seoul --body-file body.md
    venv/bin/python probe_cli.py state <workflow-id>
    venv/bin/python probe_cli.py update <workflow-id> confirm_step '{"step_id":"scale-nodegroup","operator":"alice"}'

## `run` 走的是 standalone activity

`client.start_activity` 直接在 `dr-plan-queue` 上起一个探针，不经过任何 workflow。
每次调用自带 id 与 search attributes（`DRStepName=probe.<name>`、`DRPlanRef`），
所以 `history` 能按计划把历次临时探测查回来，而它们不会混进计划的执行历史。

2026-10-05 在本机实测过这条路：启动 → 取回结果、non_retryable 错误如实抛回、
按自定义 search attribute 查回（list/count 均为 2）、关闭后按 id 取回 handle。

## 为什么 `update` 只是一个透传

人推进执行的三个动作（confirm_step / decide / resolve_gate）都有 validator，
越序、缺署名、override 不写理由都会在**进入 history 之前**被拒掉。这里不另做
校验 —— 两处校验会分叉，而 workflow 里那一处才是权威。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from temporalio.client import Client  # noqa: E402
from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes  # noqa: E402

from probe_catalog import PROBES, activity_name, check_params  # noqa: E402
from probes import ProbeInput, ProbeResult  # noqa: E402

TARGET = os.environ.get("DR_TEMPORAL_TARGET", "localhost:7233")
NAMESPACE = os.environ.get("DR_TEMPORAL_NAMESPACE", "default")
QUEUE = os.environ.get("DR_TEMPORAL_TASK_QUEUE", "dr-plan-queue")

_SA_STEP = SearchAttributeKey.for_keyword("DRStepName")
_SA_REF = SearchAttributeKey.for_keyword("DRPlanRef")


def _parse_kv(items: list[str], probe: str) -> dict:
    spec = PROBES[probe]
    out: dict = {}
    for it in items:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"参数要写成 k=v：{it!r}")
        p = spec.params.get(k)
        out[k] = int(v) if p is not None and p.type == "int" else v
    return out


async def cmd_run(c: Client, a: argparse.Namespace) -> int:
    if a.probe not in PROBES:
        print(f"未知探针 {a.probe!r}。可用：{', '.join(sorted(PROBES))}", file=sys.stderr)
        return 2
    params = _parse_kv(a.params, a.probe)
    bad = check_params(a.probe, params)
    if bad:
        print("参数不合法：\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aid = f"probe-{a.probe}-{ts}-{uuid.uuid4().hex[:4]}"
    h = await c.start_activity(
        activity_name(a.probe),
        ProbeInput(params=params, plan_ref=a.plan_ref),
        id=aid,
        task_queue=QUEUE,
        result_type=ProbeResult,
        start_to_close_timeout=timedelta(seconds=PROBES[a.probe].timeout_seconds),
        # ⚠️ 必须有排队超时。没有 worker 注册这个探针时（比如主机上还是旧代码），
        #    start_activity 照样成功返回，然后**永远等下去** —— 与 workflow 在
        #    无 worker 队列上「RUNNING 但永不前进」是同一个形状。30 秒没人接单
        #    就响亮失败，而不是让人以为探针还在测。
        schedule_to_start_timeout=timedelta(seconds=30),
        search_attributes=TypedSearchAttributes(
            [SearchAttributePair(_SA_STEP, activity_name(a.probe)),
             SearchAttributePair(_SA_REF, a.plan_ref)]
        ),
        summary=PROBES[a.probe].question[:120],
    )
    r: ProbeResult = await h.result()
    print(json.dumps({"activity_id": aid, **r.__dict__}, ensure_ascii=False, indent=2, default=str))
    # 退出码也是三态：0=PASS 1=FAIL 3=UNKNOWN —— 脚本里调用时分得开。
    return {"PASS": 0, "FAIL": 1}.get(r.verdict, 3)


async def cmd_history(c: Client, a: argparse.Namespace) -> int:
    q = f'DRPlanRef = "{a.plan_ref}"'
    n = 0
    async for x in c.list_activities(q):
        print(getattr(x, "activity_id", ""), getattr(x, "activity_type", ""), getattr(x, "status", ""))
        n += 1
        if n >= a.limit:
            break
    print(f"（{n} 条，查询：{q}）")
    return 0


async def cmd_start_plan(c: Client, a: argparse.Namespace) -> int:
    body = Path(a.body_file).read_text(encoding="utf-8")
    wid = f"plan-{a.plan_id}"
    h = await c.start_workflow(
        "DrPlanWorkflow",
        {"plan_id": a.plan_id, "body": body, "author": a.author, "reason": a.reason},
        id=wid,
        task_queue=QUEUE,
    )
    print(json.dumps({"workflow_id": wid, "run_id": h.result_run_id}, ensure_ascii=False))
    return 0


async def cmd_state(c: Client, a: argparse.Namespace) -> int:
    h = c.get_workflow_handle(a.workflow_id)
    d = await h.describe()
    q = {"DrRunbookWorkflow": "runbook_state", "ArcPlanExecutionWorkflow": "arc_state"}.get(
        d.workflow_type, "plan_state")
    print(json.dumps(await h.query(q), ensure_ascii=False, indent=2, default=str))
    return 0


async def cmd_update(c: Client, a: argparse.Namespace) -> int:
    h = c.get_workflow_handle(a.workflow_id)
    try:
        r = await h.execute_update(a.name, json.loads(a.payload))
    except Exception as e:  # noqa: BLE001 —— validator 的拒绝原因要原样给人看
        # ⚠️ 原因在 e.cause 里，不在 e 本身。2026-10-06 实测：只打印 e 时人看到的是
        #    「被拒绝：Workflow update failed」—— 一句不带任何信息的失败，而 validator
        #    写好的「步骤 switch-front-door 仍标着 needs_human_input」被藏起来了。
        #    validator 的全部价值在于告诉人**为什么**被拒。
        cause = getattr(e, "cause", None)
        print(f"被拒绝：{cause if cause else e}", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0


async def cmd_arc_run(c: Client, a: argparse.Namespace) -> int:
    """起一个 ArcPlanExecutionWorkflow。输入是 JSON 文件（ArcRunInput 的字段）。

    起完就返回 —— ARC 第一步在等人批准，人用 infra/dr-korea/arc-approve.sh
    （要 MFA）去批。进度用 `state <workflow_id>` 看。
    """
    spec = json.loads(Path(a.input_file).read_text(encoding="utf-8"))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = spec["plan_arn"].rsplit("/", 1)[-1].split(":")[0]
    wid = f"arc-{plan}-{spec['target_region']}-{ts}"
    h = await c.start_workflow("ArcPlanExecutionWorkflow", spec, id=wid, task_queue=QUEUE)
    print(json.dumps({"workflow_id": wid, "run_id": h.result_run_id}, ensure_ascii=False))
    return 0


def cmd_list() -> int:
    for name, s in PROBES.items():
        ps = ", ".join(
            f"{k}{'' if p.required else '?'}:{p.type}" for k, p in s.params.items()
        ) or "无参数"
        print(f"{name:26} {s.question}\n{'':26} 参数：{ps}\n{'':26} 手段：{s.means}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("probe")
    r.add_argument("params", nargs="*")
    r.add_argument("--plan-ref", default="adhoc")
    h = sub.add_parser("history")
    h.add_argument("--plan-ref", default="adhoc")
    h.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("start-plan")
    s.add_argument("--plan-id", required=True)
    s.add_argument("--body-file", required=True)
    s.add_argument("--author", default="dr-plan-generator")
    s.add_argument("--reason", default="自动生成的初版")
    st = sub.add_parser("state")
    st.add_argument("workflow_id")
    u = sub.add_parser("update")
    u.add_argument("workflow_id")
    u.add_argument("name")
    u.add_argument("payload")
    ar = sub.add_parser("arc-run")
    ar.add_argument("input_file")
    a = ap.parse_args()
    if a.cmd == "list":
        return cmd_list()

    async def go() -> int:
        c = await Client.connect(TARGET, namespace=NAMESPACE)
        return await {
            "run": cmd_run, "history": cmd_history, "start-plan": cmd_start_plan,
            "state": cmd_state, "update": cmd_update, "arc-run": cmd_arc_run,
        }[a.cmd](c, a)

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())

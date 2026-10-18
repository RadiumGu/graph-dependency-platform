"""ui_text.py — 把计划与执行状态渲染成 Temporal Web UI 里**人能读**的文本。

## 为什么要有这个文件（2026-10-18）

plan-20261006-tokyo-to-seoul 在 Temporal UI 的 Timeline 上只有两根贯穿 12 天的长柱：
workflow 本身，和一个标成「1 (30d)」的计时器。每一步要做什么、现在卡在谁手上，
UI 上一个字都看不到 —— 只能去 Input 里翻 12KB 的 Markdown 原文，或者用 CLI 查询。

Temporal 有三个专门给人看的位置，这里全部用上：

  current details    workflow.set_current_details(md)    详情页「User Metadata」里的 Markdown，
                                                          随状态更新 —— **步骤表放在这里**
  timer summary      wait_condition(timeout_summary=…)   Timeline 上计时器的标签（替代「1 (30d)」）
  activity summary   execute_activity(summary=…)         Timeline 上每个 activity 的标签
                                                          （替代 probe.eks_ready_nodes 这类类型名）

## 约束

- 纯函数、无 I/O：在 workflow 沙箱里调用，必须是确定性的。
- 只改**展示元数据**，不加/减任何 command —— 否则在途执行回放会不确定性失败
  （test_134 用线上真实 history 做回放核对）。
- Markdown 里的命令要原样可复制：表格里放截断版，表下放完整命令块。
"""
from __future__ import annotations

from typing import Any

from runbook import ProbeRef, Runbook, Step

#: summary 上限是 200 **字节**，details 是 20K **字节**（见
#: docs/lessons/temporal-execution-readability-2026-10.md §2）。中文一个字 3 字节，
#: 按字符截会超限 —— 所以 clip 按 UTF-8 字节截。
SUMMARY_MAX = 200
DETAILS_MAX = 19_000
_CELL_MAX = 110

_KIND = {"check": "🔍 核查", "manual": "🛠 人工", "decision": "⚖️ 裁决"}


def clip(s: str, n: int) -> str:
    """压成一行，并截到 n 个**字符**（表格单元用）。"""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def clip_bytes(s: str, n: int) -> str:
    """压成一行，并截到 n 个 **UTF-8 字节**以内，不切断多字节字符。"""
    s = " ".join((s or "").split())
    b = s.encode("utf-8")
    if len(b) <= n:
        return s
    return b[: n - 3].decode("utf-8", errors="ignore") + "…"


def _cell(s: str) -> str:
    """表格单元：去换行、转义竖线。"""
    return clip(s, _CELL_MAX).replace("|", "\\|")


def ref_text(ref: ProbeRef) -> str:
    if not ref.params:
        return ref.probe
    return ref.probe + "(" + ", ".join(f"{k}={v}" for k, v in ref.params.items()) + ")"


def _verify_text(s: Step) -> str:
    parts = []
    if s.probes:
        parts.append(", ".join(ref_text(r) for r in s.probes))
    if s.pre:
        parts.append("前：" + ", ".join(ref_text(r) for r in s.pre))
    if s.post:
        parts.append("后：" + ", ".join(ref_text(r) for r in s.post))
    return " · ".join(parts) or "—"


def _action_text(s: Step) -> str:
    if s.kind == "manual":
        return f"`{_cell(s.command or '（未填）')}`" if s.command else "（未填）"
    if s.kind == "decision":
        return "选：" + " / ".join(s.options)
    return "只读核查"


def _reversible_text(s: Step) -> str:
    if s.kind != "manual":
        return "—"
    # 回滚命令在表下的「完整命令」里 —— 表格里只放结论，免得把表撑得无法阅读
    if s.reversible:
        return "✅ 可逆" + ("（回滚命令见下）" if s.rollback else "（⚠️ 没写回滚）")
    return "⛔ **不可逆**"


def steps_table(rb: Runbook, status_of: dict[int, str] | None = None) -> str:
    """一步一行。status_of 给出时多一列「状态」。"""
    cols = ["#", "类型", "步骤", "要执行的动作", "核实（探针）", "可逆"]
    if status_of is not None:
        cols.insert(1, "状态")
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "---|" * len(cols)
    rows = [head, sep]
    for i, s in enumerate(rb.steps):
        flag = " ⚠️**待人填写**" if s.needs_human_input else ""
        cells = [str(i + 1), _KIND.get(s.kind, s.kind), _cell(s.title) + flag,
                 _action_text(s), _cell(_verify_text(s)), _reversible_text(s)]
        if status_of is not None:
            cells.insert(1, status_of.get(i, "· 未开始"))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def full_commands(rb: Runbook) -> str:
    """人工步骤的完整命令与回滚命令 —— 表格里是截断的，这里可以直接复制。"""
    out = []
    for i, s in enumerate(rb.steps):
        if s.kind != "manual":
            continue
        out.append(f"**{i + 1}. {s.title}**（`{s.id}`）")
        out.append("```bash\n" + (s.command or "# 未填") + "\n```")
        if s.rollback:
            out.append("回滚：\n```bash\n" + s.rollback + "\n```")
    return "\n\n".join(out)


def _cap(md: str) -> str:
    b = md.encode("utf-8")
    if len(b) <= DETAILS_MAX:
        return md
    return (b[: DETAILS_MAX - 200].decode("utf-8", errors="ignore")
            + "\n\n…（超长已截断；全文用 `current_body` 查询）")


# ── 计划（DrPlanWorkflow）────────────────────────────────────────────────


_NEXT = {
    "draft": "等人**批准**这一版（`approve_plan`），或修订（`revise_plan`）",
    "approved": "已批准，等人发起**演练**（`start_drill`）",
    "drilling": "演练进行中 —— 看子执行",
    "drilled": "演练已覆盖被批准的这一版，等人**授权执行**（`authorize_execution`）",
    "executing": "执行进行中 —— 看子执行，每个变更步骤都在等人确认",
    "executed": "已执行完",
    "closed": "已关闭",
}


def plan_details(
    *, plan_id: str, version: int, state: str, sha256: str, rb: Runbook | None,
    parse_error: list[str] | None, approval_blockers: list[str], active_child: str | None,
) -> str:
    lines = [f"## 计划 `{plan_id}` · v{version} · {state}", ""]
    nxt = _NEXT.get(state, state)
    lines.append(f"**下一步**：{nxt}")
    if state == "draft" and approval_blockers:
        lines.append("")
        lines.append("**批准前必须解决：**")
        lines += [f"- {clip(b, 200)}" for b in approval_blockers]
    if active_child:
        lines.append(f"\n**子执行**：`{active_child}`（步骤执行进度在它的详情页）")
    lines.append(f"\n正文 sha256 `{sha256[:16]}…`")
    if rb is None:
        lines.append("\n⚠️ 正文里没有可解析的 `dr-runbook` 块")
        if parse_error:
            lines += [f"- {clip(p, 200)}" for p in parse_error]
        return _cap("\n".join(lines))
    n_manual = sum(1 for s in rb.steps if s.kind == "manual")
    n_irrev = sum(1 for s in rb.steps if s.kind == "manual" and not s.reversible)
    lines.append(f"{rb.source_region} → {rb.target_region} · {len(rb.steps)} 步"
                 f"（人工 {n_manual}，其中不可逆 {n_irrev}）")
    lines += ["", "### 步骤", "", steps_table(rb)]
    cmds = full_commands(rb)
    if cmds:
        lines += ["", "### 人工步骤的完整命令", "", cmds]
    return _cap("\n".join(lines))


def plain(s: str) -> str:
    """summary 在 Timeline 上按纯文本显示，去掉 Markdown 记号。"""
    return s.replace("**", "").replace("`", "")


def plan_wait_summary(state: str, idle_hours: int) -> str:
    return clip_bytes(plain(f"等人推进（{state}）：{_NEXT.get(state, state)} · {idle_hours}h 无动作自动归档"), SUMMARY_MAX)


# ── 执行（DrRunbookWorkflow）─────────────────────────────────────────────


def _awaiting_block(s: Step, awaiting: dict[str, Any], wid: str) -> list[str]:
    t = awaiting.get("type")
    n = f"`{s.id}`"
    if t == "confirm":
        return [
            f"### 🙋 现在等你：执行 {s.title}",
            "在你的终端里执行（这一步由人执行，Temporal 不执行任何变更）：",
            "```bash\n" + (s.command or "# 未填") + "\n```",
            "执行完后在首尔 worker 主机上确认（经 SSM），Temporal 接着跑后置探针核实：",
            f"```bash\n/opt/dr-worker/venv/bin/python /opt/dr-worker/app/probe_cli.py update {wid} confirm_step "
            f"'{{\"step_id\":\"{s.id}\",\"operator\":\"<你的名字>\"}}'\n```",
        ]
    if t == "decision":
        return [
            f"### 🙋 现在等你：裁决 {s.title}",
            "可选：" + " / ".join(f"`{o}`" for o in awaiting.get("options", [])),
            f"```bash\n/opt/dr-worker/venv/bin/python /opt/dr-worker/app/probe_cli.py update {wid} decide "
            f"'{{\"step_id\":\"{s.id}\",\"choice\":\"<选项>\",\"operator\":\"<你的名字>\",\"reason\":\"<理由>\"}}'\n```",
        ]
    if t == "gate":
        fl = awaiting.get("failing", [])
        rows = [f"- `{f.get('probe')}` = **{f.get('verdict')}**：{clip(f.get('reason', ''), 160)}" for f in fl]
        return [
            f"### ⛔ 闸门没过：{s.title}（{awaiting.get('phase')}）{n}",
            *rows,
            "选 `retry`（重测）/ `override`（必须写理由）/ `abort`：",
            f"```bash\n/opt/dr-worker/venv/bin/python /opt/dr-worker/app/probe_cli.py update {wid} resolve_gate "
            f"'{{\"step_id\":\"{s.id}\",\"phase\":\"{awaiting.get('phase')}\",\"action\":\"retry\",\"operator\":\"<你的名字>\"}}'\n```",
        ]
    return []


def step_statuses(rb: Runbook, cursor: int | None, status: str, awaiting: dict[str, Any] | None,
                  done_ids: set[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    finished = status in ("completed",)
    for i, s in enumerate(rb.steps):
        if s.id in done_ids or finished or (cursor is not None and i < cursor):
            out[i] = "✅"
        elif cursor == i:
            if awaiting:
                out[i] = {"confirm": "🙋 等确认", "decision": "🙋 等裁决", "gate": "⛔ 闸门"}.get(
                    awaiting.get("type"), "🙋")
            elif status in ("aborted", "stalled"):
                out[i] = "⛔ " + status
            else:
                out[i] = "⏳ 进行中"
    return out


def runbook_details(
    *, wid: str, mode: str, plan_id: str, version: int, rb: Runbook | None, status: str,
    cursor: int | None, awaiting: dict[str, Any] | None, done_ids: set[str], findings: list[str],
) -> str:
    title = "演练（只跑探针，不做任何变更）" if mode == "rehearsal" else "**真执行**（每个变更由人执行，Temporal 核实）"
    lines = [f"## {plan_id} v{version} · {title}", "", f"**状态**：{status}"]
    if rb is None:
        lines.append("（正文尚未加载）")
        return _cap("\n".join(lines))
    if cursor is not None and 0 <= cursor < len(rb.steps):
        s = rb.steps[cursor]
        lines.append(f"**当前**：第 {cursor + 1}/{len(rb.steps)} 步 · {s.title}")
        if awaiting:
            lines += [""] + _awaiting_block(s, awaiting, wid)
    if findings:
        lines += ["", "### 发现"] + [f"- {clip(f, 220)}" for f in findings[-10:]]
    lines += ["", "### 步骤", "",
              steps_table(rb, step_statuses(rb, cursor, status, awaiting, done_ids))]
    return _cap("\n".join(lines))


def probe_summary(rb: Runbook | None, step_id: str, phase: str, ref: ProbeRef) -> str:
    """Timeline 上一个探针 activity 的标签：「3·拉起首尔节点组·后 eks_ready_nodes(min_ready=2)」。"""
    n, title = "?", step_id
    if rb is not None:
        for i, s in enumerate(rb.steps):
            if s.id == step_id:
                n, title = str(i + 1), s.title
                break
    ph = {"check": "核查", "pre": "前置", "post": "后置"}.get(phase, phase)
    return clip_bytes(plain(f"{n}·{title}·{ph} {ref_text(ref)}"), SUMMARY_MAX)


def human_wait_summary(rb: Runbook | None, awaiting: dict[str, Any], hours: float) -> str:
    sid = awaiting.get("step_id", "")
    s = next((x for x in (rb.steps if rb else []) if x.id == sid), None)
    t = awaiting.get("type")
    what = {"confirm": "等人执行并确认", "decision": "等人裁决", "gate": "闸门没过，等人处理"}.get(t, "等人")
    detail = ""
    if s is not None and t == "confirm" and s.command:
        detail = "：" + clip(s.command, 90)
    elif t == "decision":
        detail = "：" + " / ".join(awaiting.get("options", []))
    label = f"{what}·{s.title if s else sid}{detail}"
    return clip_bytes(plain(f"{label}（{hours:g}h 不回复就停在原地）"), SUMMARY_MAX)


# ── 启动时的静态元数据（列表页一行 + 详情页正文）────────────────────────


def plan_static(plan_id: str, rb: Runbook | None) -> tuple[str, str]:
    """(static_summary, static_details)。从**已解析的块**渲染，不手写第二份。"""
    if rb is None:
        return clip_bytes(f"DR 计划 {plan_id}（正文无 dr-runbook 块）", SUMMARY_MAX), ""
    n_manual = sum(1 for s in rb.steps if s.kind == "manual")
    n_irrev = sum(1 for s in rb.steps if s.kind == "manual" and not s.reversible)
    summary = clip_bytes(
        f"DR 计划 {rb.source_region}→{rb.target_region}：{len(rb.steps)} 步，人工 {n_manual}"
        f"（不可逆 {n_irrev}）· {plan_id}", SUMMARY_MAX)
    details = _cap("\n".join([
        f"## 启动时的 v1（{rb.source_region} → {rb.target_region}）", "",
        "每个变更由人执行；Temporal 展示命令原文、在你确认后用只读探针核实终态。",
        "实时进度与「现在等你做什么」在下面的 **Current Details**。", "",
        steps_table(rb), "", "### 人工步骤的完整命令", "", full_commands(rb),
    ]))
    return summary, details

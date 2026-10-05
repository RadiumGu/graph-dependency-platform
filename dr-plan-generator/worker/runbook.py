"""runbook.py — 灾备切换步骤的结构化格式 `dr-runbook/v1`。**纯 Python。**

## 它在整条链里的位置

    生成器（半自动）──> 计划正文（markdown + 一个 ```dr-runbook 块）
                           │  人审核、修改（revise_plan）
                           ▼
                        approve_plan ──> start_drill（每个探针真跑一次）
                           │
                           ▼  authorize_execution
                        DrRunbookWorkflow：人执行每个变更，Temporal 只负责核实

**Temporal 不执行任何变更。** 拉起节点组、提升数据库、切换前门 —— 这些都是
`manual` 步骤：Temporal 把命令原文展示给人，等人回报「已执行」，然后用探针
从**另一个角度**核实终态。这样做有三个直接后果：

- 不可逆的步骤（Aurora 提升）不再需要「能被演练」—— 它从来不被程序执行；
- 写操作重试是否幂等这个问题消失了 —— 程序不做写；
- worker 的实例角色永远只需要读权限。

## 块是权威，不是渲染

计划正文里那个 ```dr-runbook 块是**唯一的**机器可读来源。人改计划就是改这个
块；validator 在修订进入 history 之前就校验它。正文其余部分只是给人读的说明，
程序不解析。

## 四种步骤

- `check`：只跑探针，没有人工动作。用于切换前置条件、终态确认。
- `manual`：人执行一条命令。**必须**有后置探针 —— 没有后置探针的人工操作等于
  没有核实手段。必须声明可逆性：可逆则写回滚，不可逆则写清楚为什么接受。
- `decision`：人在几个选项里选一个（例如 Aurora 有序切换 vs 允许丢数据）。
  没有默认值，等不到裁决就停住，不替人选。
- 任何步骤都可以 `needs_human_input: true` —— 生成器**不知道**的东西就标出来，
  而不是编一个。带这个标记的计划不能被批准。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from probe_catalog import PROBES, check_params

FORMAT = "dr-runbook/v1"
KINDS = ("check", "manual", "decision")

_BLOCK_RE = re.compile(r"```dr-runbook[ \t]*\n(.*?)\n```", re.DOTALL)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class RunbookError(ValueError):
    """块结构不合法。`problems` 是全部问题，不只是第一个 —— 人改一次就能改完。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("dr-runbook 不合法：\n  - " + "\n  - ".join(problems))


@dataclass
class ProbeRef:
    probe: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    kind: str
    title: str
    #: manual：给人复制粘贴的命令原文。
    command: str | None = None
    #: check：要跑的探针。
    probes: list[ProbeRef] = field(default_factory=list)
    #: manual：做之前必须过的探针 / 做之后必须过的探针。
    pre: list[ProbeRef] = field(default_factory=list)
    post: list[ProbeRef] = field(default_factory=list)
    #: manual：是否可逆。None = 没声明（不合法）。
    reversible: bool | None = None
    rollback: str | None = None
    #: 不可逆时必须写：为什么接受不可逆。
    irreversible_note: str | None = None
    #: decision：可选项。
    options: list[str] = field(default_factory=list)
    #: 生成器不知道、需要人填的地方。
    needs_human_input: bool = False
    note: str = ""

    def all_probes(self) -> list[tuple[str, ProbeRef]]:
        """(阶段, 引用) 列表，阶段为 check / pre / post。"""
        return (
            [("check", p) for p in self.probes]
            + [("pre", p) for p in self.pre]
            + [("post", p) for p in self.post]
        )


@dataclass
class Runbook:
    source_region: str
    target_region: str
    steps: list[Step]
    format: str = FORMAT

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 去掉空字段，让人读的块短一点。
        #
        # ⚠️ 只去 None / 空列表 / 空串，以及值为 False 的 needs_human_input。
        #    第一版写成 `v in (None, [], "", False)`，于是 `reversible: false`
        #    被当成「没填」丢掉了 —— 不可逆的 Aurora 提升序列化之后变成了
        #    「没声明可逆性」，被 validator 拒掉（draft_runbook.py 的自检抓到的）。
        #    「明确的否」和「没说」不是一回事，这正是本项目反复踩的那一族。
        for s in d["steps"]:
            drop = [k for k, v in s.items() if v is None or v == [] or v == ""]
            if s.get("needs_human_input") is False:
                drop.append("needs_human_input")
            for k in drop:
                del s[k]
        return d

    def unresolved(self) -> list[str]:
        return [s.id for s in self.steps if s.needs_human_input]


# ── 解析 ──────────────────────────────────────────────────────────────────


def find_blocks(body: str) -> list[str]:
    return _BLOCK_RE.findall(body or "")


def extract_runbook(body: str) -> Runbook | None:
    """从计划正文里取出 runbook。没有块返回 None；块不合法抛 RunbookError。"""
    blocks = find_blocks(body)
    if not blocks:
        return None
    if len(blocks) > 1:
        # 两个块时「哪个是权威」说不清 —— 拒绝而不是挑一个。
        raise RunbookError([f"正文里有 {len(blocks)} 个 dr-runbook 块，只允许一个"])
    try:
        raw = json.loads(blocks[0])
    except json.JSONDecodeError as e:
        raise RunbookError([f"dr-runbook 块不是合法 JSON：{e}"]) from None
    return parse_runbook(raw)


def _refs(raw: Any, where: str, problems: list[str]) -> list[ProbeRef]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"{where} 必须是数组")
        return []
    out = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict) or not isinstance(r.get("probe"), str):
            problems.append(f"{where}[{i}] 必须是 {{\"probe\": 名字, \"params\": {{...}}}}")
            continue
        unknown = set(r) - {"probe", "params"}
        if unknown:
            problems.append(f"{where}[{i}] 有未知字段 {sorted(unknown)}")
        params = r.get("params") or {}
        for p in check_params(r["probe"], params):
            problems.append(f"{where}[{i}]：{p}")
        out.append(ProbeRef(probe=r["probe"], params=params))
    return out


_STEP_FIELDS = set(Step.__dataclass_fields__)


def parse_runbook(raw: Any) -> Runbook:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise RunbookError(["dr-runbook 块的顶层必须是对象"])
    if raw.get("format") != FORMAT:
        problems.append(f"format 必须是 {FORMAT!r}，实际是 {raw.get('format')!r}")
    for k in ("source_region", "target_region"):
        if not isinstance(raw.get(k), str) or not raw.get(k):
            problems.append(f"缺少 {k}")
    if raw.get("source_region") and raw.get("source_region") == raw.get("target_region"):
        problems.append("source_region 与 target_region 相同")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        problems.append("steps 必须是非空数组")
        steps_raw = []

    steps: list[Step] = []
    seen: set[str] = set()
    for i, s in enumerate(steps_raw):
        where = f"steps[{i}]"
        if not isinstance(s, dict):
            problems.append(f"{where} 必须是对象")
            continue
        sid = s.get("id")
        where = f"步骤 {sid!r}" if isinstance(sid, str) else where
        unknown = set(s) - _STEP_FIELDS
        if unknown:
            # 拼错的字段名（例如 "rolback"）会被静默丢掉 —— 那个回滚说明就没了。
            problems.append(f"{where} 有未知字段 {sorted(unknown)}")
        if not isinstance(sid, str) or not _ID_RE.match(sid):
            problems.append(f"{where} 的 id 必须是小写字母/数字/连字符")
        elif sid in seen:
            problems.append(f"步骤 id {sid!r} 重复")
        else:
            seen.add(sid)

        kind = s.get("kind")
        if kind not in KINDS:
            problems.append(f"{where} 的 kind 必须是 {KINDS} 之一，实际是 {kind!r}")
        if not isinstance(s.get("title"), str) or not s["title"].strip():
            problems.append(f"{where} 缺少 title")

        step = Step(
            id=sid if isinstance(sid, str) else f"#{i}",
            kind=kind if isinstance(kind, str) else "?",
            title=s.get("title") or "",
            command=s.get("command"),
            probes=_refs(s.get("probes"), f"{where}.probes", problems),
            pre=_refs(s.get("pre"), f"{where}.pre", problems),
            post=_refs(s.get("post"), f"{where}.post", problems),
            reversible=s.get("reversible"),
            rollback=s.get("rollback"),
            irreversible_note=s.get("irreversible_note"),
            options=s.get("options") or [],
            needs_human_input=bool(s.get("needs_human_input", False)),
            note=s.get("note") or "",
        )
        problems.extend(_check_step(step, where))
        steps.append(step)

    if problems:
        raise RunbookError(problems)
    return Runbook(
        source_region=raw["source_region"],
        target_region=raw["target_region"],
        steps=steps,
    )


def _check_step(s: Step, where: str) -> list[str]:
    p: list[str] = []
    if s.kind == "check":
        if not s.probes:
            p.append(f"{where}：check 步骤必须至少有一个探针")
        if s.command or s.pre or s.post or s.options:
            p.append(f"{where}：check 步骤只能有 probes，不能有 command/pre/post/options")

    elif s.kind == "manual":
        if not s.needs_human_input and not (isinstance(s.command, str) and s.command.strip()):
            p.append(f"{where}：manual 步骤必须给出命令原文（不知道就标 needs_human_input）")
        if not s.post:
            p.append(
                f"{where}：manual 步骤必须至少有一个后置探针 —— "
                "没有后置探针的人工操作等于没有核实手段"
            )
        if s.probes or s.options:
            p.append(f"{where}：manual 步骤用 pre/post，不能有 probes/options")
        if s.reversible is None or not isinstance(s.reversible, bool):
            p.append(f"{where}：manual 步骤必须声明 reversible（true/false）")
        elif s.reversible:
            if not (isinstance(s.rollback, str) and s.rollback.strip()):
                p.append(f"{where}：声明了可逆，就必须写 rollback")
        else:
            if not (isinstance(s.irreversible_note, str) and s.irreversible_note.strip()):
                p.append(f"{where}：不可逆步骤必须写 irreversible_note（为什么接受不可逆）")
            if s.rollback:
                p.append(f"{where}：不可逆步骤不能写 rollback —— 那会让人以为能撤回")

    elif s.kind == "decision":
        opts = s.options
        if (not isinstance(opts, list) or len(opts) < 2
                or not all(isinstance(o, str) and o for o in opts)
                or len(set(opts)) != len(opts)):
            p.append(f"{where}：decision 步骤需要至少两个互不相同的选项")
        if s.command or s.probes or s.pre or s.post:
            p.append(f"{where}：decision 步骤只能有 options，不能有 command/probes")
    return p


# ── 审批前的额外要求 ──────────────────────────────────────────────────────


def approval_problems(body: str) -> list[str]:
    """批准一份计划之前必须为空的问题列表。"""
    try:
        rb = extract_runbook(body)
    except RunbookError as e:
        return e.problems
    if rb is None:
        return [
            "正文里没有 dr-runbook 块 —— 没有结构化步骤，演练和执行都无从核实。"
        ]
    unresolved = rb.unresolved()
    if unresolved:
        return [
            f"步骤 {', '.join(unresolved)} 仍标着 needs_human_input —— "
            "生成器不知道的东西必须由人填上，才能批准"
        ]
    return []


# ── 渲染 ──────────────────────────────────────────────────────────────────


def render_block(rb: Runbook) -> str:
    return "```dr-runbook\n" + json.dumps(rb.to_dict(), ensure_ascii=False, indent=2) + "\n```"


def summarize(rb: Runbook) -> list[str]:
    """给人看的一行一步。"""
    out = []
    for n, s in enumerate(rb.steps, 1):
        flag = " ⚠️ 待人填写" if s.needs_human_input else ""
        if s.kind == "manual":
            rev = "可逆" if s.reversible else "**不可逆**"
            out.append(f"{n}. [人工·{rev}] {s.title}{flag}")
        elif s.kind == "decision":
            out.append(f"{n}. [裁决] {s.title}：{' / '.join(s.options)}{flag}")
        else:
            names = ", ".join(p.probe for p in s.probes)
            out.append(f"{n}. [核查] {s.title}（{names}）{flag}")
    return out


def known_probe_names() -> frozenset[str]:
    return frozenset(PROBES)

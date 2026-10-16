"""capture_agent_ab.py — 从 AWS DevOps Agent 取回「有/无 graph MCP」A/B 对照的原始记录。

产出 `agent_ab_mcp.json`，供 RCA 页「交给 Agent」第一节使用。

## 实验设计（2026-10-16）

- 两个**新建**的 Agent Space，关联同一个 AWS 账号、同一个 IAM 角色。
  唯一变量：`mcp` 臂额外关联 graph-dependency MCP 并注册 skill
  `dependency-graph-rca`；`base` 臂什么都不加。
  （没有复用 petsite-devops：在那里 disassociate MCP 后新调查仍能调到图谱工具，
  且 Space 的 memory 会把前一次调查的结论带给后一次 —— 两者都会污染对照。）
- 每次用 FIS 注入一个**已知根因**的故障，故障结束后在两个 Space 各开一个调查，
  描述只写用户可见症状。
- 两轮：
  - round 1：agent 角色能看到 FIS 与 CloudTrail，且调查标题 `AB-Fn` 与 FIS tag
    `ab-Fn` 相同 —— 两臂都直接查到了实验，根因准确率封顶，**只能比效率**。
  - round 2：换用 `DevOpsAgentRole-AB-NoFaultVisibility`（显式 Deny `fis:*`、
    `cloudtrail:*`），标题中性、实验不打 tag。准确率才有可比性。

## 什么是抓的、什么是人判的

`minutes` / `tools` / `graph_calls` / `skill_loaded` / `root_cause_headline` 由本脚本
从 API 现取。`verdict`（correct / partial / wrong / none）是**人工**对照真实注入
写的，依据写在 `verdict_basis` —— 不伪装成自动判定。

用法（需要 devops-agent 读权限）：
    python3 demo/fixtures/capture_agent_ab.py
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess

REGION = "ap-northeast-1"
OUT = pathlib.Path(__file__).with_name("agent_ab_mcp.json")
GRAPH_PREFIX = "graph-dependency_"

SPACES = {
    1: {"mcp": "2cefe684-1005-4e9a-b95c-cd93dccc4fec", "base": "1abde79a-8a29-48ae-967e-7796b5163731"},
    2: {"mcp": "eed65012-a3de-4b55-ba1a-15b6b3afec33", "base": "05da450e-2628-4061-86c5-4af3cfc3a50b"},
}

# (round, label, 故障, FIS 模板, FIS 实验, 窗口 UTC, mcp task, base task, mcp 判定, base 判定, 依据)
CASES = [
    (1, "F1", "两个私有子网到 DynamoDB 断连 3 分钟", "EXT3DXsuAXqfsNJ4", "EXPUgogrybgjvKLH3Q", "01:21–01:25",
     "5f5b6971-4525-457a-a138-ea7f34714c58", "5c104a43-9584-40ea-8653-6a86c6429117", "correct", "correct",
     "两臂均从 FIS/CloudTrail 直接查到实验（round 1 可见性未屏蔽）"),
    (1, "F2", "payforadoption 一个 Pod 到 Aurora PG:5432 黑洞 5 分钟", "EXTDcupv9cPwfAKqJ", "EXPGauFb3CLYn6DpSn",
     "01:31–01:37", "597f3338-ce1d-4ae6-ac04-c92e2db6048c", "8a9429cf-6fd0-479a-887f-03df547e02bb",
     "correct", "correct", "同上；base 把 petsearch/petfood 也列进影响面（过宽）"),
    (1, "F3", "Aurora writer 重启", "EXTbxM1D5tMkJbJ", "EXPAbVKua36ixubw8e", "01:45–01:47",
     "dea9ed59-0ce4-42a9-87ee-d71f17cb97a1", "34557d03-37fa-44c2-810d-6093308351a6", "correct", "correct",
     "同上；mcp 在查 FIS 之前先由图谱提出「两功能共用同一 Aurora 集群」"),
    (1, "F4", "payforadoption Pod 内存压力 80%（自身故障，对照）", "EXT2bpJ9UgdcquKn", "EXP8fHvzojrkNu7MH4",
     "01:53–01:59", "49dc274c-dd65-43c2-9b8f-1951cbd6107b", "00fcf2a3-2997-47d0-9b09-9c001e0d768f",
     "correct", "correct", "同上"),
    (1, "F5", "Aurora reader 重启", "EXT3X5eDqkg57hzg", "EXPLAmC1zk1fXfZJTU", "02:05–02:07",
     "2edc0ec6-a1a3-41ba-81d8-5afa13b29447", "b82e587b-f434-4948-a8e3-f150673657f0", "correct", "correct",
     "同上；mcp 点名了被重启的 reader 与读它的 petlistadoptions，base 只写「某个 Aurora 实例」"),
    (2, "G1", "两个私有子网到 DynamoDB 断连", "EXT3DXsuAXqfsNJ4", "EXPnRSAi8PGGN8R45Y", "02:46–02:49",
     "87373cc8-2f35-40fb-a8af-98673091e51f", "d0d1f02f-d5f1-4004-b64c-666c31e87efb", "correct", "correct",
     "两臂都定位到 DynamoDB egress 被阻断；base 把注入方归为 chaos-mesh（机制对、来源错）"),
    (2, "G2", "两个私有子网到 S3 断连", "EXT3LA9HE9NsGj9r", "EXPMk759mG5VEvgYr6", "02:55–03:00",
     "bb8daf42-a26a-4098-b643-0ce40ad2b5e8", "2bf26a73-7b75-4cbf-b83a-bd55634798ce", "partial", "wrong",
     "真实报错是 02:57–03:00 连 S3 桶超时（petsearch 日志）。mcp 首要假设仍是 DynamoDB（错，"
     "那是 G1 窗口残留的报错），但另列了「S3 访问失败」假设、未定论；base 只归因到 DynamoDB"),
    (2, "G3", "Aurora writer 重启", "EXTbxM1D5tMkJbJ", "EXPWFBPWPTCzpXRkn6", "03:06–03:08",
     "20b4ffd0-2309-45e1-88ad-547c6d8236ad", "38df2783-26b4-49e5-af8d-35ecf3f8df2c", "correct", "partial",
     "RDS 事件：writer 03:07:08 shutdown / 03:07:19 restarted。mcp 定为根因并给出实例与时刻；base 停在假设"),
    (2, "G4", "payforadoption Pod 内存压力（自身故障，对照）", "EXT2bpJ9UgdcquKn", "EXPiE9Qzb87JJpoYCi",
     "03:14–03:20", "9a08791f-db32-4e1b-9980-d38526a32949", "9f767ca4-0182-49fe-bbe7-aca4dd1d231e",
     "partial", "wrong", "mcp 只到「namespace 内有 FIS 实验」，没找到内存机制；base 归因到窗口外 03:07 的 writer 重启"),
    (2, "G5", "Aurora reader 重启", "EXT3X5eDqkg57hzg", "EXPL4NopnhbZXVTPZU", "03:26–03:28",
     "0774d71a-4a1d-4fa1-ac2c-124a4de7ed78", "7ec2172e-20c3-4c63-8404-a72cac9affce", "correct", "none",
     "RDS 事件：reader 03:26:40 shutdown / 03:26:56 restarted。mcp 点名该 reader；"
     "base 找不到「过往领养页面」由哪个服务/数据库支撑，No findings"),
]


def _aws(space: str, *args: str) -> dict:
    p = subprocess.run(["aws", "devops-agent", *args, "--region", REGION, "--agent-space-id", space,
                        "--output", "json"], capture_output=True, text=True)
    return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}


def _first_list(d: dict) -> list:
    return next((v for v in d.values() if isinstance(v, list)), [])


def _walk(o, acc: list) -> None:
    """journal 内容是多层嵌套的 JSON 字符串，tool_use 可能出现在任意深度。"""
    if isinstance(o, dict):
        if o.get("type") == "tool_use" and "tool_name" in o:
            acc.append(o)
        for v in o.values():
            _walk(v, acc)
    elif isinstance(o, list):
        for v in o:
            _walk(v, acc)
    elif isinstance(o, str) and o[:1] in "{[":
        try:
            _walk(json.loads(o), acc)
        except ValueError:
            pass


def _text(c) -> str:
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            return c
    if isinstance(c, dict):
        for k in ("markdown", "summary", "content", "text", "body"):
            if isinstance(c.get(k), str):
                return c[k]
    return json.dumps(c, ensure_ascii=False)


def _headline(md: str) -> str:
    """结论里第一条 Root Cause（没有就第一条 Cause / Hypothesis）的标题。"""
    for pat in (r"^### Root Cause:\s*(.+)$", r"^### Cause:\s*(.+)$", r"^### Hypothesis:\s*(.+)$"):
        m = re.search(pat, md, re.M)
        if m:
            return m.group(1).strip()
    return "No findings" if "No findings" in md else ""


def _arm(space: str, task_id: str) -> dict:
    t = _aws(space, "get-backlog-task", "--task-id", task_id).get("task", {})
    row = {"task_id": task_id, "status": t.get("status"), "execution_id": t.get("executionId")}
    exes = _first_list(_aws(space, "list-executions", "--task-id", task_id))
    if exes:
        c = datetime.datetime.fromisoformat(exes[0]["createdAt"])
        u = datetime.datetime.fromisoformat(exes[0]["updatedAt"])
        row["minutes"] = round((u - c).total_seconds() / 60, 1)
    recs = _first_list(_aws(space, "list-journal-records", "--execution-id", t["executionId"]))
    uses, seen = [], set()
    for r in recs:
        if r.get("recordType") == "utilization":   # 这里也有工具名，计入会重复
            continue
        acc: list = []
        _walk(r.get("content"), acc)
        for u_ in acc:
            k = u_.get("id") or json.dumps(u_, sort_keys=True)
            if k not in seen:
                seen.add(k)
                uses.append(u_)
    names = [u_["tool_name"] for u_ in uses]
    row["tools"] = len(names)
    row["graph_calls"] = sum(n.startswith(GRAPH_PREFIX) for n in names)
    row["skill_loaded"] = any("dependency-graph-rca" in json.dumps(u_.get("input", {})) for u_ in uses)
    md = "\n\n".join(_text(r.get("content")) for r in recs
                     if r.get("recordType") == "investigation_summary_md")
    row["root_cause_headline"] = _headline(md)
    return row


def main() -> None:
    cases = []
    for rnd, label, fault, tpl, exp, window, tm, tb, vm, vb, basis in CASES:
        sp = SPACES[rnd]
        mcp, base = _arm(sp["mcp"], tm), _arm(sp["base"], tb)
        mcp["verdict"], base["verdict"] = vm, vb
        cases.append({"round": rnd, "label": label, "fault": fault, "fis_template": tpl,
                      "fis_experiment": exp, "window_utc": f"2026-10-16 {window}",
                      "verdict_basis": basis, "mcp": mcp, "base": base})
        print(f"{label}: mcp {mcp.get('minutes')}min/{mcp['tools']} tools  "
              f"base {base.get('minutes')}min/{base['tools']} tools")
    OUT.write_text(json.dumps({
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "region": REGION,
        "spaces": {str(k): v for k, v in SPACES.items()},
        "rounds": {
            "1": "agent 角色可见 FIS/CloudTrail，且调查标题与 FIS tag 相同 —— 准确率封顶，只比效率",
            "2": "agent 角色显式 Deny fis:*、cloudtrail:*，标题中性、实验无 tag —— 准确率可比",
        },
        "arms": {"mcp": "graph-dependency MCP（24 个只读工具）+ skill dependency-graph-rca",
                 "base": "只关联 AWS 账号"},
        "verdict_scale": {"correct": "定位到真实注入的故障", "partial": "提出了正确方向的假设但未定论",
                          "wrong": "归因到别的东西", "none": "没有结论"},
        "known_leaks_round2": [
            "AWS Health 仍能看到 FIS 事件（G1 的 mcp 臂由此确认）",
            "角色名 NoFaultVisibility 本身是提示，多份结论写了 denied FIS",
            "k8s 里 FIS 留下的 fiscontainer",
        ],
        "cases": cases,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()

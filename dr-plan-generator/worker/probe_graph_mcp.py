#!/usr/bin/env python3
"""probe_graph_mcp.py — 拿真凭证打一次图谱 MCP，看清真实响应形状。

## 为什么需要它

graph_mcp_client.py 里对响应形状的处理是**按 MCP 规范推断的**，
没见过这台 server 真实的响应。照推断写完快照 activity 再去碰，就是在赌 ——
而本项目今天已经因为「看起来完成了而什么都没变」吃过一次亏。

本探针做两件事：
  1. 打印真实响应的**形状**（键名、类型、层级），不打印内容
  2. 顺带检验 graph_mcp_client.py 本身 —— 它 import 的是同一个模块，
     所以探针通过就等于那个模块的解析路径对

## ⚠️ 它证明什么、不证明什么

**在哪台机器上跑，结论不同：**

  · 在操作者机器上跑（IAM 用户）→ 只证明**响应形状**。
    用的是你自己的权限，与 worker 的权限无关。
  · 在韩国 EC2 上跑（实例角色）→ 才证明**worker 那条路通**。

这两件事被混为一谈是很自然的，所以脚本会把当前身份打出来并明说。

## 不打印凭证

secret 值与 access token 全程只在内存里，输出只报长度。
——一个被打印到 CI 日志里的 token 和一个被提交进仓库的 token 一样糟。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graph_mcp_client as g  # noqa: E402


def shape(v, depth: int = 0, max_depth: int = 4) -> str:
    """把一个值描述成它的形状，不泄露内容。

    对 str 只报长度：图谱数据里可能有内部主机名、ARN，
    而这份输出可能被贴进工单或聊天。
    """
    pad = "  " * depth
    if depth > max_depth:
        return "…"
    if isinstance(v, dict):
        if not v:
            return "{}"
        lines = [f"{pad}  {k}: {shape(val, depth + 1, max_depth)}" for k, val in list(v.items())[:12]]
        more = f"\n{pad}  …还有 {len(v) - 12} 个键" if len(v) > 12 else ""
        return "{\n" + "\n".join(lines) + more + f"\n{pad}}}"
    if isinstance(v, list):
        if not v:
            return "[] (空)"
        return f"[{len(v)} 项] 首项 -> {shape(v[0], depth + 1, max_depth)}"
    if isinstance(v, str):
        return f"str({len(v)})"
    if v is None:
        return "null"
    return f"{type(v).__name__}({v})" if isinstance(v, (int, float, bool)) else type(v).__name__


def main() -> int:
    import boto3

    who = boto3.client("sts").get_caller_identity()["Arn"]
    print(f"当前身份：{who}")
    if ":role/" in who:
        print("  → 这是角色。若它就是 worker 的实例角色，本次结果可证明 worker 那条路通。")
    else:
        print("  ⚠️ 这是 IAM 用户，不是 worker 的实例角色 ——")
        print("     本次成功只证明**响应形状**，不证明 worker 有权限走通。")
    print()

    for k in ("DR_GRAPH_MCP_RUNTIME_ARN", "DR_GRAPH_MCP_COGNITO_DOMAIN",
              "DR_GRAPH_MCP_SECRET_ID"):
        v = os.environ.get(k, "")
        print(f"  {k:32} {'✓ ' + v[:60] if v else '✗ 未设'}")
    print()

    # ── 1. 取 token ──────────────────────────────────────────────────────
    print("── 1. 换 token ──")
    try:
        tok = g.fetch_token()
    except g.GraphMcpError as e:
        print(f"  ✗ {e}")
        return 1
    print(f"  ✓ 拿到 access token（{len(tok)} 字符，值不打印）")

    # ── 2. tools/list ───────────────────────────────────────────────────
    print("\n── 2. tools/list ──")
    try:
        tools = g.list_tools(token=tok)
    except g.GraphMcpError as e:
        print(f"  ✗ {e}")
        return 1
    print(f"  ✓ {len(tools)} 个工具")
    print(f"  首个工具的形状：{shape(tools[0])}")
    names = [t.get("name", "?") for t in tools]
    print(f"\n  快照要用的那 5 条是否都在：")
    missing = []
    for q in g.SNAPSHOT_QUERIES:
        hit = q in names
        print(f"    {'✓' if hit else '✗'} {q}")
        if not hit:
            missing.append(q)
    if missing:
        print(f"\n  ⚠️ 缺 {len(missing)} 条。**不要在 worker 里写临时查询补上** ——")
        print("     那会抹掉「不直连 Neptune」这个决定的意义。")
        print("     正确做法是往 QUERY_CATALOG 里加（一次对版本化契约的受审改动）。")
        print(f"\n  目录里实际有的（前 20 个）：")
        for n in names[:20]:
            print(f"      {n}")

    # ── 3. 真调一条，看 provenance 与契约版本 ───────────────────────────
    probe = next((q for q in g.SNAPSHOT_QUERIES if q in names), None)
    if not probe:
        print("\n  跳过第 3 步：没有一条期望的查询存在")
        return 1
    print(f"\n── 3. tools/call {probe} ──")
    # 先不带参数调，看它要什么 —— 参数校验失败会以 isError 返回可读文本，
    # 那正好告诉我们必填参数是什么，比猜快。
    try:
        doc = g.query(probe, token=tok)
    except g.ContractVersionMismatch as e:
        print(f"  ⚠️ 契约版本不符（这是刻意的硬失败）：\n     {e}")
        print("     确认新契约兼容后改 DR_GRAPH_CONTRACT_VERSION。")
        return 2
    except g.GraphMcpError as e:
        print(f"  ✗ {e}")
        print("     若上面写的是「参数校验失败」，那正好列出了必填参数 —— 照它传。")
        return 1
    print("  ✓ 调用成功")
    print(f"  响应形状：{shape(doc)}")
    # 键名是 **_provenance**（前导下划线），实测得知。
    # 这里原本写的是 "provenance"，于是打印出「实际 None → 一致」——
    # 一个自相矛盾却报通过的输出，正是本项目一直在清理的那类假判据。
    prov = doc.get("_provenance", {})
    print(f"\n  _provenance 关键字段：")
    for k in ("source", "graph_cluster", "region", "graph_contract_version",
              "query", "queried_at", "determinism", "caveat"):
        if k in prov:
            print(f"    {k:24} {prov[k]}")
    got = prov.get("graph_contract_version")
    ok = str(got) == str(g.EXPECTED_CONTRACT_VERSION)
    print(f"\n  契约版本 期望 {g.EXPECTED_CONTRACT_VERSION} / 实际 {got!r} "
          f"→ {'一致' if ok else '不一致'}")
    print(f"  读取路径：{doc.get('_read_via')}")
    rc = doc.get("row_count")
    print(f"  行数：{rc}")
    if rc == 0:
        print(f"    server 自己的指引：{doc.get('empty_result_guidance','')[:120]}")
        print("    零行不等于错误 —— 但对灾备快照，全部查询都为零才是判据。")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

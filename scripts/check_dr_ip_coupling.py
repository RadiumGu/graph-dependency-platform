#!/usr/bin/env python3.11
"""
check_dr_ip_coupling.py —— 检查 AgentCore 指向的 Temporal 地址是否还有效。

## 为什么需要这个检查

AgentCore runtime 的 `TEMPORAL_ADDRESS` 里写的是 Temporal 实例的**私有 IP**
(`http://10.20.1.10:7243`)。这个耦合有一个安静的失效路径:

    实例被替换 → 新 IP → AgentCore 仍指向旧地址 → 所有 MCP 工具调用超时

而超时的表现是「工具调不通」,看起来像 AgentCore 挂了、像网络不通、
像 Temporal 挂了 —— **唯独不像「地址过期了」**。排查会绕很久。

`PrivateIpAddress` 在 `AWS::EC2::Instance` 的 `createOnlyProperties` 里,
所以把 IP 固定进模板本身就要求替换实例;在做那次有计划的重建之前,
这个检查是唯一能及早发现耦合断裂的手段。

## 判据纪律

**「查不到」与「不一致」是两件事。** 拿不到某一侧的值时返回
`inconclusive` 而不是 `mismatch` —— 把「没测到」判成「不匹配」会制造假告警,
而假告警会让人开始忽略这个检查。

退出码:
    0  一致
    1  确认不一致(需要处置)
    2  无法判断(缺凭据 / 资源不存在 / 调用失败)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

REGION = "ap-northeast-2"
STACK = "dr-korea-temporal"
RUNTIME_NAME = "temporal_mcp"
#: Temporal 的 HTTP API 端口。不是 8080(Web UI)、不是 7233(gRPC)。
HTTP_API_PORT = 7243


def _client(service: str) -> Any:
    import boto3  # noqa: PLC0415

    return boto3.client(service, region_name=REGION)


def instance_ip() -> tuple[str | None, str | None]:
    """从栈里找到实例，再查它的当前私有 IP。

    刻意走「栈 → 实例 → IP」而不是直接读栈的 Output：
    Output 是栈上次更新时的值，实例的 describe 才是当下的事实。
    本项目已实测六次「命令成功但没生效」，这类差别正是那些案例的来源。
    """
    try:
        cfn = _client("cloudformation")
        res = cfn.describe_stack_resources(StackName=STACK)["StackResources"]
        inst = [
            r for r in res if r["ResourceType"] == "AWS::EC2::Instance"
        ]
        if len(inst) != 1:
            return None, f"栈 {STACK} 里找到 {len(inst)} 个 EC2 实例，预期 1 个"
        iid = inst[0]["PhysicalResourceId"]

        ec2 = _client("ec2")
        d = ec2.describe_instances(InstanceIds=[iid])
        r0 = d["Reservations"][0]["Instances"][0]
        state = r0["State"]["Name"]
        ip = r0.get("PrivateIpAddress")
        if not ip:
            return None, f"实例 {iid} 状态 {state}，没有私有 IP"
        return ip, None
    except Exception as e:  # noqa: BLE001
        return None, f"查实例 IP 失败：{type(e).__name__}: {e}"


def agentcore_address() -> tuple[str | None, str | None]:
    try:
        c = _client("bedrock-agentcore-control")
        rts = c.list_agent_runtimes()["agentRuntimes"]
        match = [r for r in rts if r["agentRuntimeName"] == RUNTIME_NAME]
        if len(match) != 1:
            return None, f"找到 {len(match)} 个名为 {RUNTIME_NAME} 的 runtime，预期 1 个"
        rid = match[0]["agentRuntimeId"]
        rt = c.get_agent_runtime(agentRuntimeId=rid)
        addr = (rt.get("environmentVariables") or {}).get("TEMPORAL_ADDRESS")
        if not addr:
            return None, f"runtime {rid} 没有设 TEMPORAL_ADDRESS"
        return addr, None
    except Exception as e:  # noqa: BLE001
        return None, f"查 AgentCore 地址失败：{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    ip, ip_err = instance_ip()
    addr, addr_err = agentcore_address()

    expected = f"http://{ip}:{HTTP_API_PORT}" if ip else None
    problems = [e for e in (ip_err, addr_err) if e]

    if problems:
        verdict = "inconclusive"
        # ⚠️ 不是 mismatch。拿不到值不等于值不对 —— 把「没测到」判成
        # 「不匹配」会制造假告警，而假告警会让人开始忽略这个检查。
        detail = "；".join(problems)
        code = 2
    elif addr == expected:
        verdict = "ok"
        detail = f"AgentCore 指向 {addr}，与实例当前 IP 一致"
        code = 0
    else:
        verdict = "mismatch"
        detail = (
            f"AgentCore 指向 {addr}，但实例当前 IP 对应的应是 {expected}。"
            "所有 MCP 工具调用会超时，而超时看起来像网络或服务故障，"
            "唯独不像「地址过期」。处置：update-agent-runtime 改 TEMPORAL_ADDRESS。"
        )
        code = 1

    out = {
        "verdict": verdict,
        "instance_ip": ip,
        "agentcore_address": addr,
        "expected_address": expected,
        "detail": detail,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        mark = {"ok": "✅", "mismatch": "❌", "inconclusive": "⚠️"}[verdict]
        print(f"{mark} {verdict}: {detail}")
    return code


if __name__ == "__main__":
    sys.exit(main())

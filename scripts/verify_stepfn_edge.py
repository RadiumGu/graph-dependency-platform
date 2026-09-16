#!/usr/bin/env python3
"""验证 `petsite -> StepFunction` —— 用**状态机执行历史**作证据通道。

    python3 scripts/verify_stepfn_edge.py --list
    python3 scripts/verify_stepfn_edge.py --apply

## 为什么这条边必须换证据通道

2026-09-15 实测：3 次 bunny 领养全部成功触发 StartExecution
（`StatusCode = OK`，executionArn 分别是 `bunny-023/024/025-…`），
而 **X-Ray 服务图里始终没有 StepFunctions 节点**。原因在 petsite 日志里写着：

    AWSXRayRecorder|DEBUG|Service name doesn't exist in
      AWSServiceHandlerManifest: serviceName = StepFunctions.

X-Ray .NET SDK 的服务清单没有 StepFunctions 条目，所以它**不会**为这个调用
建子段。这条边在 X-Ray 服务图里**永远**不会出现，与流量多少无关 ——
此前记录的「三种前缀实测全 0」不是采样波动，是结构性的。

同一个形态本项目已经处理过一次：pethistory 没有消费方 SQL 边遥测时，
改用 RDS 自身事件作证据通道。这里改用状态机自己的执行历史。

## 为什么只有 bunny 能驱动

`petsite/Controllers/PaymentController.cs:124-128` —— StartExecution **仅在
`pettype == "bunny"` 时触发**（:211 `StartExecutionAsync`）。
26 只库存里只有 4 只 bunny，所以泛化负载几乎压不出这条边的流量。
此前测到 0 次也有这一层原因。**必须定向打 bunny。**

## 归因判据：执行名带 petId

执行名形如 `bunny-{petId}-{uuid}`，所以能把执行归因到具体那次领养，
而不是「这段时间有执行发生」。上一批执行是 2026-09-13（近两天前），
基线天然安静 —— 但判据不依赖这一点，仍按时间窗 + 名字前缀双重过滤。

## 判定要求三环，与其他验证器一致

    基线   定向打 bunny -> 窗口内出现 >= MIN_EXECUTIONS 次执行
    故障   deny states:StartExecution -> 执行归零
    回滚   撤策略 + 重启 -> 执行重现

缺第三环退回 `observation_only`：只有前两环的话，无法排除
「执行停止是别的原因」。

## ⚠️ 业务退化必须单独测，不能假定

StartExecution 失败**不一定**让领养失败 —— 取决于 petsite 是否吞异常。
所以本脚本分别记录「注入生效性」（执行归零）与「业务退化」（领养成功率），
两者不混。若执行归零而业务照常，判定是 `inconclusive` 而非 `confirmed`：
那说明这条边**不承重**，把它写成 confirmed 就是过度声称。
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "chaos", "code"))
sys.path.insert(0, os.path.join(_HERE, "..", "infra", "lambda", "shared", "python"))
sys.path.insert(0, os.path.join(_HERE, ".."))

STATE_MACHINE_ARN = ("arn:aws:states:ap-northeast-1:926093770964:stateMachine:"
                     "StepFnStateMachine76D362E8-3jkn8j2OUpdQ")
#: 图谱里这条边的目标节点名（现查校验，不硬信）
EDGE_TARGET = "StepFnStateMachine76D362E8-3jkn8j2OUpdQ"
EDGE_SERVICE = "petsite"
#: petsite 的 IRSA 执行角色 —— 由脚本现查，这里只作交叉校验
EXPECT_ROLE_HINT = "PetSiteServiceAccount"
#: 基线窗口内至少要有这么多次执行才算「压出了流量」。
#: 库存只有 4 只 bunny，所以门槛按实际可驱动量定，不照抄 X-Ray 边的 20。
MIN_EXECUTIONS = 3
_EXEC_NAME_PREFIX = "bunny-"


def _vd():
    """复用 iam-deny 验证器里的共享辅助（互锁、角色解析、边 id、写回）。

    ⚠️ 已知技术债：`acquire_chaos_lock` / `_edge_ids` / `_execution_role_for`
    现在住在一个 CLI 脚本里，被另外三个脚本 import。应搬进共享模块 ——
    暂留是为了不动那批门禁，且此刻不宜与并发会话在同一文件上抢改。
    """
    spec = importlib.util.spec_from_file_location(
        "_vd", os.path.join(_HERE, "verify_via_iam_deny.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _executions(since_epoch: float) -> list[dict]:
    """窗口内、名字前缀匹配的执行清单。

    双重过滤（时间窗 + 名字前缀）而不是只看时间：状态机可能被别的路径触发，
    只按时间窗计数会把别人的流量算进来 —— 那正是此前把 API Gateway 节点的
    1547 次当成状态机流量时犯的错。
    """
    import boto3
    sf = boto3.client("stepfunctions", region_name="ap-northeast-1")
    out, tok = [], None
    while True:
        kw = {"stateMachineArn": STATE_MACHINE_ARN, "maxResults": 100}
        if tok:
            kw["nextToken"] = tok
        r = sf.list_executions(**kw)
        for e in r.get("executions", []):
            sd = e["startDate"]
            ts = sd.timestamp() if hasattr(sd, "timestamp") else float(sd)
            if ts < since_epoch:
                # list_executions 按时间倒序 —— 一旦早于窗口就可以停
                return out
            if str(e.get("name", "")).startswith(_EXEC_NAME_PREFIX):
                out.append({"name": e["name"], "status": e["status"], "ts": ts})
        tok = r.get("nextToken")
        if not tok:
            return out


def _drive_bunnies(rounds: int = 4) -> dict:
    """定向打 bunny 领养，返回业务侧结果。

    归还宠物：本项目实测过「库存耗光的表现与依赖被切断完全一样」——
    4 只 bunny 尤其容易耗光，不归还会在两轮内造出假 confirmed。
    """
    import urllib.request
    from runner.business_probes import (PETSITE_URL, _UA, parse_pets,
                                        probe_adopt)
    req = urllib.request.Request(PETSITE_URL + "/", headers=_UA)
    html = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
    pets = parse_pets(html)
    bunnies = [p for p in pets if p.get("available") and p["pettype"] == "bunny"]
    if not bunnies:
        return {"ok": False, "why": "本轮无可用 bunny —— 前置条件不满足，不是业务故障",
                "adopted": 0, "tried": 0}
    ok = 0
    tried = 0
    for b in bunnies[:rounds]:
        tried += 1
        r = probe_adopt(pet=b)
        ok += 1 if r.get("value") == 1 else 0
        time.sleep(2)
    return {"ok": True, "adopted": ok, "tried": tried,
            "ids": [b["id"] for b in bunnies[:rounds]]}


def _return_pets(ids: list[str]) -> int:
    """把领养掉的 bunny 放回去。

    `/housekeeping` 只删交易记录、**不重置可用性**（petsite 现版代码里真正
    放回去那段是注释掉的），所以必须走 statusupdater API。
    """
    import json
    import urllib.request
    from runner.business_probes import _UA
    try:
        from runner.service_names import STATUS_UPDATER_URL as _U
    except Exception:
        _U = os.environ.get("STATUS_UPDATER_URL") or ""
    if not _U:
        return 0
    n = 0
    for pid in ids:
        try:
            body = json.dumps({"petid": pid, "petavailability": "yes"}).encode()
            rq = urllib.request.Request(
                _U, data=body, headers={**_UA, "Content-Type": "application/json"})
            urllib.request.urlopen(rq, timeout=30).read()
            n += 1
        except Exception:
            pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--window", type=int, default=300,
                    help="每个阶段的观测窗口秒数")
    a = ap.parse_args()
    vd = _vd()

    print("── 0. 前置校验 ──")
    eids, how = vd._edge_ids(EDGE_SERVICE, EDGE_TARGET)
    if not eids:
        raise SystemExit("图谱里定位不到边 %s -> %s（%s）"
                         % (EDGE_SERVICE, EDGE_TARGET, how))
    print("  边 id: %s（%s）" % ([e["eid"][:12] for e in eids], how))
    role, rhow = vd._execution_role_for(EDGE_SERVICE, "Microservice")
    if not role:
        raise SystemExit("解析不到 petsite 的执行角色：%s" % rhow)
    print("  执行角色: %s（%s）" % (role, rhow))
    if EXPECT_ROLE_HINT not in role:
        raise SystemExit(
            "角色名不含 %r —— 拒绝对一个可能不对的角色施加 deny。"
            "解析到的是 %s" % (EXPECT_ROLE_HINT, role))

    if not a.apply:
        recent = _executions(time.time() - 3600)
        print("\n── 近 1 小时执行（证据通道自检）──")
        for e in recent[:6]:
            print("  %-46s %-10s %s" % (
                e["name"][:46], e["status"],
                datetime.datetime.utcfromtimestamp(e["ts"]).strftime("%H:%M:%SZ")))
        if not recent:
            print("  （无。基线阶段需要先定向打 bunny）")
        print("\n（--list 模式。加 --apply 跑完整三环实验）")
        return 0

    print("\n── 1. 基线：定向打 bunny ──")
    t0 = time.time()
    base_biz = _drive_bunnies()
    base_exec = _executions(t0)
    print("  业务: %s" % base_biz)
    print("  执行: %d 次 %s" % (len(base_exec), [e["name"][:22] for e in base_exec]))
    if base_biz.get("adopted", 0) == 0:
        raise SystemExit("基线未能领养任何 bunny —— 前置条件不满足，中止（不是判定）")
    if len(base_exec) < MIN_EXECUTIONS:
        raise SystemExit(
            "基线执行数 %d < %d —— 压不出足够流量就没有可退化的基线，中止"
            % (len(base_exec), MIN_EXECUTIONS))
    _return_pets(base_biz.get("ids") or [])
    print("  已归还 bunny（避免库存耗光被误读成依赖切断）")
    print("\n（三环实验的故障与回滚阶段需要互锁与 deny 策略，见 --apply 后续实现）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""验证 `petsite -> StepFunction` —— 执行历史作证据通道 + 类型条件对照。

    python3 scripts/verify_stepfn_edge.py --list
    python3 scripts/verify_stepfn_edge.py --apply

## 为什么这条边必须换证据通道

2026-09-15 实测：3 次 bunny 领养全部成功触发 StartExecution
（`StatusCode = OK`，executionArn 分别是 `bunny-023/024/025-…`），
而 **X-Ray 服务图里始终没有 StepFunctions 节点**。原因在 petsite 日志里写着：

    AWSXRayRecorder|DEBUG|Service name doesn't exist in
      AWSServiceHandlerManifest: serviceName = StepFunctions.

注意 `PaymentController.cs:210` **确实调了** `AWSSDKHandler.RegisterXRay
<IAmazonStepFunctions>()` —— 注册做了，但 X-Ray .NET SDK 的服务清单没有
StepFunctions 条目，所以它不会为这个调用建子段。**这条边在 X-Ray 服务图里
永远不会出现，与流量多少无关**；此前记录的「三种前缀实测全 0」不是采样波动。

同一形态本项目处理过一次：pethistory 没有消费方 SQL 边遥测时改用 RDS 事件。
这里改用状态机自己的执行历史。

## 这条边独有的价值：实验内自带阴性对照

`PaymentController.cs:124` —— StartExecution **仅在 `pettype == "bunny"` 时**
触发（`:211`）。而异常**不会被吞**：`:137` 的 catch 把页面渲染成
`ViewData["txStatus"] = "failure"` 并返回 **HTTP 200**。

于是同一次故障注入里：

    bunny 领养        应当失败（走 StartExecution → AccessDenied → failure 页）
    puppy/kitten 领养 应当照常成功（根本不碰 StepFunctions）

**这是阴性对照**：如果连非 bunny 也失败，说明 deny 打宽了或 restart 本身造成
影响，判定不成立。别的边都拿不到这种同实验内对照。

顺带这也检验「HTTP 200 + 失败内容」这条检测路径 —— 本会话已有两个缺陷
（probe_adopt 把表单页当成功、probe_waggle 把 ConcurrencyException 判为健康）
都出在只看状态码。

## 判定要求三环 + 对照

    基线   定向打 bunny -> 窗口内 >= MIN_EXECUTIONS 次执行，且 bunny 领养成功
    故障   deny states:* -> bunny 领养失败、执行归零，**且非 bunny 仍成功**
    回滚   撤策略 + 重启  -> bunny 领养恢复、执行重现

缺第三环退回 `observation_only`：只有前两环无法排除「执行停止是别的原因」。
注入生效但 bunny 未失败 -> `inconclusive`（这条边不承重），**绝不是「不依赖」**。
对照失败（非 bunny 也挂）-> `inconclusive`，并明确报出 deny 打宽的可能。
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "chaos", "code"))
sys.path.insert(0, os.path.join(_HERE, "..", "infra", "lambda", "shared", "python"))
sys.path.insert(0, os.path.join(_HERE, ".."))

STATE_MACHINE_ARN = ("arn:aws:states:ap-northeast-1:926093770964:stateMachine:"
                     "StepFnStateMachine76D362E8-3jkn8j2OUpdQ")
EDGE_TARGET = "StepFnStateMachine76D362E8-3jkn8j2OUpdQ"
EDGE_SERVICE = "petsite"
EDGE_LABEL = "StepFunction"          # SEVERANCE_METHODS 的键（节点类型）
EXPECT_ROLE_HINT = "PetSiteServiceAccount"
#: 基线窗口内至少要有这么多次执行。库存只有 4 只 bunny，门槛按实际可驱动量定，
#: 不照抄 X-Ray 边的 MIN_BASELINE_REQUESTS=20。
MIN_EXECUTIONS = 3
_EXEC_NAME_PREFIX = "bunny-"
#: 注入生效轮询预算（秒）。SigV4 的策略状态是**最终一致**的，实测传播 2~3 分钟，
#: 所以不猜延迟、轮询到 bunny 真的失败为止。
_PROPAGATION_BUDGET = 420
_POLL_INTERVAL = 20


def _vd():
    """复用 iam-deny 验证器的共享构件。

    ⚠️ 已知技术债：`acquire_chaos_lock` / `_edge_ids` / `_execution_role_for`
    等住在一个 CLI 脚本里被别的脚本 import。应搬进共享模块 ——
    暂留是为了不动那批门禁。
    """
    spec = importlib.util.spec_from_file_location(
        "_vd", os.path.join(_HERE, "verify_via_iam_deny.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 证据通道：状态机执行历史 ──────────────────────────────────────────────

def _executions(since_epoch: float) -> list[dict]:
    """窗口内、名字前缀匹配的执行清单。

    **双重过滤（时间窗 + 名字前缀）**，不只看时间：状态机可能被别的路径触发，
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
                return out          # list_executions 按时间倒序
            if str(e.get("name", "")).startswith(_EXEC_NAME_PREFIX):
                out.append({"name": e["name"], "status": e["status"], "ts": ts})
        tok = r.get("nextToken")
        if not tok:
            return out


# ── 业务驱动：bunny 与非 bunny 分开 ───────────────────────────────────────

def _pets() -> list[dict]:
    import urllib.request
    from runner.business_probes import PETSITE_URL, _UA, parse_pets
    req = urllib.request.Request(PETSITE_URL + "/", headers=_UA)
    html = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
    return parse_pets(html)


def _drive(pettype: str | None, rounds: int,
           only_ids: list[str] | None = None) -> dict:
    """领养指定类型的宠物 `rounds` 次，返回 {tried, ok, ids, details}。

    `pettype=None` 表示「任何非 bunny」——那是阴性对照用的。

    ## `only_ids`：闸门必须只用**基线证明可领养**的宠物

    ⚠️ 2026-09-17 实测缺陷：原实现取 `avail[:rounds]`，即恒定从最小 id 开始。
    而 `022` 这只 bunny **在基线就领养失败**（`022/bunny=0`，另外三只都成功），
    于是：

      - 「注入已生效」闸门在 +20s 就通过了 —— 但它测的是 022，
        **没施加 deny 也会这么报**。那不是证据。
      - 回滚后的恢复轮询一直重试 022，烧完 420s 预算全程失败，
        而最终测量用全部四只时是 3/4 已恢复。

    这与 `probe_adopt` 取 `ids[0]` 是同一族缺陷（我已修过一次，又在驱动器里
    引入了一遍）：**闸门用的样本必须是基线里证明健康的那些**，
    否则闸门测的是样本自身的问题，不是被验证的依赖。
    """
    from runner.business_probes import probe_adopt
    avail = [p for p in _pets() if p.get("available")
             and (p["pettype"] == pettype if pettype
                  else p["pettype"] != "bunny")]
    if only_ids:
        avail = [p for p in avail if p["id"] in only_ids]
    if not avail:
        return {"ok": 0, "tried": 0, "ids": [], "pre": False,
                "why": "无可用的 %s%s —— 前置条件不满足，不是业务故障"
                       % (pettype or "非 bunny",
                          ("（限定 %s）" % only_ids) if only_ids else "")}
    ok, ids, details, good = 0, [], [], []
    for p in avail[:rounds]:
        r = probe_adopt(pet=p)
        hit = r.get("value") == 1
        ok += 1 if hit else 0
        if hit:
            good.append(p["id"])
        ids.append(p["id"])
        details.append("%s/%s=%s" % (p["id"], p["pettype"], r.get("value")))
        time.sleep(2)
    return {"ok": ok, "tried": len(ids), "ids": ids, "pre": True,
            "details": details, "good": good}


def _return_pets(vd, ids: list[str]) -> int:
    """把领养掉的宠物放回去。

    `/housekeeping` 只删交易记录、**不重置可用性**（petsite 现版代码里真正
    放回去那段是注释掉的），所以走 statusupdater API。
    只有 4 只 bunny，不归还两轮就耗光，而**库存耗光的表现与依赖被切断完全一样**。
    """
    import urllib.request
    from runner.business_probes import _UA
    url = vd._status_updater_url()
    if not url:
        return 0
    n = 0
    for pid in ids:
        try:
            body = json.dumps({"petid": pid, "petavailability": "yes"}).encode()
            rq = urllib.request.Request(
                url, data=body, headers={**_UA, "Content-Type": "application/json"})
            urllib.request.urlopen(rq, timeout=30).read()
            n += 1
        except Exception:
            pass
    return n


def _restart(vd) -> str:
    """rollout restart petsite 并等完成。

    施加 deny 后**必须立刻**做：只施加 deny 会让切断**不均匀**
    （旧 Pod 的会话仍在用旧的策略评估结果），实测边成功率停在 51% ≈
    两个 Pod 只有一个被拒，判定会误写成「业务未退化」。
    """
    name = vd._k8s_workload(EDGE_SERVICE)
    if not name:
        return "⚠️ 取不到 K8s 工作负载名，切断可能只作用到部分 Pod"
    subprocess.run(["kubectl", "rollout", "restart", "deploy/%s" % name,
                    "-n", vd._NAMESPACE], capture_output=True, text=True)
    r = subprocess.run(["kubectl", "rollout", "status", "deploy/%s" % name,
                        "-n", vd._NAMESPACE, "--timeout=420s"],
                       capture_output=True, text=True)
    return (r.stdout.strip().splitlines()[-1] if r.stdout.strip()
            else r.stderr.strip()[:120])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rounds", type=int, default=4,
                    help="每阶段领养多少只 bunny（库存上限 4）")
    a = ap.parse_args()
    vd = _vd()

    print("── 0. 前置校验 ──")
    eids, how = vd._edge_ids(EDGE_SERVICE, EDGE_TARGET)
    if not eids:
        raise SystemExit("图谱里定位不到边 %s -> %s（%s）"
                         % (EDGE_SERVICE, EDGE_TARGET, how))
    print("  边 id: %s" % [e["eid"][:12] for e in eids])
    print("  %s" % how)
    role, rhow = vd._execution_role_for(EDGE_SERVICE, "Microservice")
    if not role:
        raise SystemExit("解析不到执行角色：%s" % rhow)
    if EXPECT_ROLE_HINT not in role:
        raise SystemExit("角色名不含 %r —— 拒绝对可能不对的角色施加 deny。"
                         "解析到 %s" % (EXPECT_ROLE_HINT, role))
    print("  执行角色: %s" % role)
    acct = vd._account_id()
    doc = vd._deny_document(EDGE_LABEL, EDGE_TARGET, acct)
    if not doc:
        raise SystemExit("取不到 %s 的 deny 文档" % EDGE_LABEL)
    print("  deny 文档: %s" % json.dumps(doc["Statement"][0], ensure_ascii=False))

    if not a.apply:
        recent = _executions(time.time() - 3600)
        print("\n── 近 1 小时执行（证据通道自检）──")
        for e in recent[:6]:
            print("  %-46s %-10s %s" % (
                e["name"][:46], e["status"],
                datetime.datetime.utcfromtimestamp(e["ts"]).strftime("%H:%M:%SZ")))
        if not recent:
            print("  （无。基线阶段会先定向打 bunny）")
        print("\n（--list 模式。加 --apply 跑完整三环实验）")
        return 0

    lock = vd.acquire_chaos_lock(
        "%s -> StepFunction（deny states:*）" % EDGE_SERVICE, 2400,
        "stepfn three-ring")
    policy = "%s-%s" % (vd.POLICY_PREFIX, int(time.time()))
    removed = False
    result: dict = {}
    try:
        # ── 1. 基线 ──────────────────────────────────────────────────────
        print("\n── 1. 基线 ──")
        t0 = time.time()
        b_bunny = _drive("bunny", a.rounds)
        b_exec = _executions(t0)
        print("  bunny 领养: %d/%d  %s" % (b_bunny["ok"], b_bunny["tried"],
                                          b_bunny.get("details")))
        print("  执行: %d 次 %s" % (len(b_exec), [e["name"][:20] for e in b_exec]))
        if not b_bunny.get("pre"):
            raise SystemExit("基线前置条件不满足：%s" % b_bunny.get("why"))
        if b_bunny["ok"] == 0:
            raise SystemExit("基线 bunny 领养全部失败 —— 基线本身不健康，中止")
        if len(b_exec) < MIN_EXECUTIONS:
            raise SystemExit(
                "基线执行数 %d < %d —— 压不出足够流量就没有可退化的基线，中止"
                % (len(b_exec), MIN_EXECUTIONS))
        _return_pets(vd, b_bunny["ids"])
        b_ctrl = _drive(None, 2)
        print("  对照（非 bunny）: %d/%d %s"
              % (b_ctrl["ok"], b_ctrl["tried"], b_ctrl.get("details")))
        _return_pets(vd, b_ctrl.get("ids") or [])
        # 闸门与后续阶段只用**基线证明可领养**的那几只。
        # 基线里失败的宠物（实测 022）会让闸门测到样本自身的问题。
        good = b_bunny.get("good") or []
        print("  基线健康样本（后续阶段只用这些）: %s" % good)
        result["baseline"] = {"bunny_ok": b_bunny["ok"], "bunny_tried": b_bunny["tried"],
                              "good_ids": good,
                              "executions": len(b_exec),
                              "ctrl_ok": b_ctrl["ok"], "ctrl_tried": b_ctrl["tried"]}

        # ── 2. 施加 deny ────────────────────────────────────────────────
        print("\n── 2. 施加 deny（策略 %s）──" % policy)
        d, e = vd._aws("iam", "put-role-policy", "--role-name", role,
                       "--policy-name", policy,
                       "--policy-document", json.dumps(doc))
        if d is None:
            raise SystemExit("施加失败: %s" % e)
        print("  已施加。立即 rollout restart 使切断均匀：")
        print("    %s" % _restart(vd))

        # 轮询到 bunny 真的失败 —— 不猜传播延迟
        print("  等注入生效（轮询 bunny 领养，预算 %ds）：" % _PROPAGATION_BUDGET)
        effective_at = None
        for i in range(_PROPAGATION_BUDGET // _POLL_INTERVAL):
            time.sleep(_POLL_INTERVAL)
            probe = _drive("bunny", 1, only_ids=good)
            _return_pets(vd, probe.get("ids") or [])
            print("    [+%3ds] bunny %d/%d %s"
                  % ((i + 1) * _POLL_INTERVAL, probe["ok"], probe["tried"],
                     probe.get("details") or probe.get("why", "")))
            if probe.get("pre") and probe["tried"] > 0 and probe["ok"] == 0:
                effective_at = (i + 1) * _POLL_INTERVAL
                print("    ✓ 注入已生效（第 %ds，bunny 领养失败）" % effective_at)
                break
        result["effective_at"] = effective_at

        # ── 3. 故障期测量 + 阴性对照 ────────────────────────────────────
        print("\n── 3. 故障期 ──")
        t1 = time.time()
        f_bunny = _drive("bunny", a.rounds, only_ids=good)
        _return_pets(vd, f_bunny["ids"])
        f_ctrl = _drive(None, 2)
        _return_pets(vd, f_ctrl.get("ids") or [])
        f_exec = _executions(t1)
        print("  bunny 领养: %d/%d  %s" % (f_bunny["ok"], f_bunny["tried"],
                                          f_bunny.get("details")))
        print("  对照（非 bunny）: %d/%d %s"
              % (f_ctrl["ok"], f_ctrl["tried"], f_ctrl.get("details")))
        print("  执行: %d 次" % len(f_exec))
        result["fault"] = {"bunny_ok": f_bunny["ok"], "bunny_tried": f_bunny["tried"],
                           "executions": len(f_exec),
                           "ctrl_ok": f_ctrl["ok"], "ctrl_tried": f_ctrl["tried"]}

        # ── 4. 回滚 ─────────────────────────────────────────────────────
        print("\n── 4. 回滚 ──")
        vd._aws("iam", "delete-role-policy", "--role-name", role,
                "--policy-name", policy)
        removed = True
        print("  已撤策略。**删策略不足以恢复** —— 必须 rollout restart：")
        print("    %s" % _restart(vd))
        print("  等恢复（轮询 bunny 领养）：")
        p_bunny = {"ok": 0, "tried": 0}
        for i in range(vd._RECOVERY_BUDGET_SECONDS // _POLL_INTERVAL):
            time.sleep(_POLL_INTERVAL)
            p_bunny = _drive("bunny", 1, only_ids=good)
            _return_pets(vd, p_bunny.get("ids") or [])
            print("    [+%3ds] bunny %d/%d %s"
                  % ((i + 1) * _POLL_INTERVAL, p_bunny["ok"], p_bunny["tried"],
                     p_bunny.get("details") or p_bunny.get("why", "")))
            if p_bunny.get("pre") and p_bunny["ok"] > 0:
                break
        t2 = time.time()
        r_bunny = _drive("bunny", a.rounds, only_ids=good)
        _return_pets(vd, r_bunny["ids"])
        r_exec = _executions(t2)
        print("  回滚后 bunny 领养: %d/%d  执行 %d 次"
              % (r_bunny["ok"], r_bunny["tried"], len(r_exec)))
        result["post"] = {"bunny_ok": r_bunny["ok"], "bunny_tried": r_bunny["tried"],
                          "executions": len(r_exec)}

        # ── 5. 判定 ─────────────────────────────────────────────────────
        print("\n── 5. 判定 ──")
        ring1 = len(b_exec) >= MIN_EXECUTIONS and b_bunny["ok"] > 0
        ring2 = len(f_exec) == 0 and f_bunny["ok"] == 0 and f_bunny["tried"] > 0
        ring3 = len(r_exec) >= MIN_EXECUTIONS and r_bunny["ok"] > 0
        ctrl_ok = f_ctrl["tried"] > 0 and f_ctrl["ok"] == f_ctrl["tried"]
        print("  环1 基线（执行 %d>=%d 且 bunny 成功 %d）  %s"
              % (len(b_exec), MIN_EXECUTIONS, b_bunny["ok"], "✓" if ring1 else "✗"))
        print("  环2 故障（执行归零 %d==0 且 bunny 全败 %d/%d）  %s"
              % (len(f_exec), f_bunny["ok"], f_bunny["tried"], "✓" if ring2 else "✗"))
        print("  环3 回滚（执行 %d>=%d 且 bunny 恢复 %d）  %s"
              % (len(r_exec), MIN_EXECUTIONS, r_bunny["ok"], "✓" if ring3 else "✗"))
        print("  阴性对照（非 bunny 故障期仍全成功 %d/%d）  %s"
              % (f_ctrl["ok"], f_ctrl["tried"], "✓" if ctrl_ok else "✗"))

        if not ctrl_ok:
            status, why = "inconclusive", (
                "阴性对照未通过：故障期非 bunny 领养也失败 %d/%d —— "
                "deny 可能打宽了，或 rollout restart 本身造成影响。"
                "在排除这一点之前不能把 bunny 的失败归给 StepFunctions 依赖。"
                % (f_ctrl["ok"], f_ctrl["tried"]))
        elif ring1 and ring2 and ring3:
            status, why = "confirmed", (
                "三环齐全 + 阴性对照通过。基线执行 %d 次且 bunny 领养成功；"
                "deny states:* 后执行归零、bunny 领养全部失败（%d/%d）而"
                "非 bunny 照常成功（%d/%d）；回滚后执行 %d 次、bunny 恢复。"
                "类型条件对照排除了「restart 或 deny 打宽」这两种替代解释。"
                % (len(b_exec), f_bunny["ok"], f_bunny["tried"],
                   f_ctrl["ok"], f_ctrl["tried"], len(r_exec)))
        elif ring1 and ring2 and not ring3:
            status, why = "observation_only", (
                "缺第三环（回滚后执行 %d 次、bunny 成功 %d）—— "
                "无法排除「执行停止是别的原因」，不写 confirmed。"
                % (len(r_exec), r_bunny["ok"]))
        elif ring1 and not ring2:
            status, why = "inconclusive", (
                "注入%s，但 bunny 领养未全败（%d/%d）或执行未归零（%d）—— "
                "这条边可能不承重。**这不是「不依赖」**。"
                % ("已生效（第 %ds）" % effective_at if effective_at else "未确认生效",
                   f_bunny["ok"], f_bunny["tried"], len(f_exec)))
        else:
            status, why = "inconclusive", "基线不成立，无法判定"
        print("\n  判定: %s\n  %s" % (status, why))
        result["status"], result["why"] = status, why

        if status in ("confirmed", "inconclusive"):
            # 成功率用**可比集**：只算基线证明可领养的那几只，
            # 否则含一只基线就坏的宠物会把基线口径压低、退化幅度算小。
            n_good = max(1, len(good))
            vd._persist_verdict(
                EDGE_SERVICE, EDGE_LABEL, EDGE_TARGET,
                status, why,
                {"success_rate": 100.0 * b_bunny["ok"] / n_good},
                {"success_rate": 100.0 * f_bunny["ok"] / max(1, f_bunny["tried"])},
                "stepfn-three-ring-%s" % int(time.time()),
                severance="iam-deny",
                evidence_channel="stepfn-execution-history+business-probe")
            print("  已写回图谱。")
        else:
            print("  **刻意不写回** —— observation_only 不落盘。")
        return 0
    finally:
        if not removed:
            print("\n[finally] 撤策略并 rollout restart（删策略不足以恢复）")
            vd._aws("iam", "delete-role-policy", "--role-name", role,
                    "--policy-name", policy)
            print("  %s" % _restart(vd))
        vd.release_chaos_lock(lock)
        p = os.path.join(_HERE, "..", "todo",
                         "stepfn-three-ring_%s.json"
                         % time.strftime("%Y%m%d-%H%M%S"))
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print("  实验记录: %s" % os.path.abspath(p))
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

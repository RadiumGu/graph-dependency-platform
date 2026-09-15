#!/usr/bin/env python3.11
"""用 K8s Service 选择器黑洞验证服务间依赖边。

## 为什么不是 NetworkPolicy

2026-09-14 实测：本集群的 NetworkPolicy **不被强制执行**。
`aws-eks-nodeagent` 的参数是 `--enable-network-policy=false`；把它和
`ENABLE_NETWORK_POLICY` 都打开之后，用一次性 Pod 做阳性对照 ——
施加 deny-all egress 后目标**仍然可达**，且没有生成任何 `PolicyEndpoint`。
根因是翻译 NetworkPolicy 的控制器属于 **EKS 控制面**，要通过 VPC CNI
**托管插件**启用，而本集群的 VPC CNI 不是托管插件。

那次变更已回滚。**若当时没做阳性对照，就会拿一个静默空操作去跑实验，
得到「切断已施加但业务正常」的假 inconclusive。**

## 手段：把 Service 的选择器改成不匹配任何 Pod

Service 的 Endpoints 由选择器算出。选择器指向一个不存在的标签，
Endpoints 立即变空，调用方连接直接失败 —— 这是**服务发现/路由层**的真实切断，
且不动 Pod、不改镜像、不重启工作负载，恢复就是把选择器改回去（秒级）。

与 IAM deny 的对称性一致：注入期间调用**全程**失败，不是瞬时中断。
所以判定沿用对称逻辑，`severance=k8s-service-blackhole`。

## ⚠️ 探针与被测依赖的耦合必须先解开

`probe_adopt` 默认从**首页**取宠物，而首页本身调 petsearch。
验证 `payforadoption -> petsearch` 时若不处理，petsearch 一断探针就卡在
「取不到 petId」这个前置条件上返回 `ok=False` —— 于是**测不到支付本身**，
而那正是要验证的东西。

所以本实验器在注入**之前**预取一只可用宠物，故障期把它传给
`probe_adopt(pet=...)`。这不是放宽判据，而是把两条路径分开测：
发现路径由 `probe_home` 负责，支付路径由 `probe_adopt` 负责。
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "chaos" / "code"))
sys.path.insert(0, str(_ROOT / "infra" / "lambda" / "shared" / "python"))

_NAMESPACE = "petadoptions"
MIN_BASELINE_REQUESTS = 20
MIN_BASELINE_SUCCESS_RATE = 95.0
_POLL_SECONDS = 5
_POLL_BUDGET = 180
_RECOVERY_STREAK = 8

#: 黑洞用的标签值。刻意取一个不可能存在的值，并带上用途说明 ——
#: 万一实验中断留下残留，运维一眼能看出这是谁、为什么。
_BLACKHOLE = "chaos-severed-by-dep-verify"


def _iam_deny_module():
    """复用 IAM deny 实验器的负载驱动与判据函数（同一技术债，见那边说明）。"""
    p = _ROOT / "scripts" / "verify_via_iam_deny.py"
    spec = importlib.util.spec_from_file_location("_iam_deny", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _kubectl(*args) -> tuple[bool, str]:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[:300]


def _selector(svc: str) -> dict | None:
    ok, out = _kubectl("get", "svc", svc, "-n", _NAMESPACE,
                       "-o", "jsonpath={.spec.selector}")
    if not ok or not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _endpoint_count(svc: str) -> int:
    ok, out = _kubectl("get", "endpoints", svc, "-n", _NAMESPACE,
                       "-o", "jsonpath={.subsets[*].addresses[*].ip}")
    if not ok or not out.strip():
        return 0
    return len(out.split())


def _set_selector(svc: str, sel: dict) -> tuple[bool, str]:
    patch = json.dumps({"spec": {"selector": sel}})
    return _kubectl("patch", "svc", svc, "-n", _NAMESPACE,
                    "--type=merge", "-p", patch)


def _measure(client: str, server: str, window: int) -> dict:
    """服务间边按**名字**匹配 —— 目标是服务节点，不是 AWS 资源类型。"""
    from runner.xray_metrics import XRayEdgeMetrics
    snap = XRayEdgeMetrics().collect_edge_flow(
        client, server, window_seconds=window)
    return {"ok": bool(getattr(snap, "ok", False)),
            "success_rate": getattr(snap, "success_rate", None),
            "total_requests": getattr(snap, "total_requests", None)}


def run(service: str, target: str, svc_name: str, window: int,
        warmup: int, hold: int, apply: bool) -> int:
    dm = _iam_deny_module()
    from runner.business_probes import probes_for, parse_pets, _get

    print("目标边: %s -> Microservice(%s)" % (service, target))
    print("切断对象: Service %s/%s（选择器黑洞）" % (_NAMESPACE, svc_name))
    orig = _selector(svc_name)
    if not orig:
        print("✗ 取不到 Service %s 的选择器 —— 中止（不猜）" % svc_name)
        return 2
    if _BLACKHOLE in json.dumps(orig):
        print("✗ 选择器里已有黑洞标记 —— 上次实验未回滚，拒绝叠加。")
        print("  处置：kubectl patch svc %s -n %s 恢复原选择器"
              % (svc_name, _NAMESPACE))
        return 2
    print("原选择器: %s（端点 %d 个）" % (orig, _endpoint_count(svc_name)))

    if not probes_for(service):
        print("✗ 源服务 %s 未登记业务探针 —— 拒绝开跑" % service)
        return 2
    print("业务探针: %s" % [n for n, _ in probes_for(service)])
    print()

    load = dm._LoadDriver()
    if apply and warmup > 0:
        print("── 0. 背景流量 ──")
        load.start()
        print("   lead-in %ds…" % warmup)
        time.sleep(warmup)
        print("   %s" % load.stats)
        print()

    print("── 1. 基线 ──")
    base = _measure(service, target, window)
    print("   被测边: %s" % base)
    biz_base = dm._probe_business(service)
    print("   业务探针: %s" % biz_base["detail"])
    if not base["ok"]:
        print("✗ 被测边基线采集失败（ok=False）—— 中止")
        load.stop()
        return 3
    if (base["total_requests"] or 0) < MIN_BASELINE_REQUESTS:
        print("✗ 基线请求数 %s < %d —— 拒绝出判定"
              % (base["total_requests"], MIN_BASELINE_REQUESTS))
        load.stop()
        return 3
    if (base["success_rate"] or 0) < MIN_BASELINE_SUCCESS_RATE:
        print("✗ 基线成功率 %.2f%% < %.0f%% —— 拒绝开跑"
              % (base["success_rate"] or 0, MIN_BASELINE_SUCCESS_RATE))
        load.stop()
        return 3
    ok, why = dm._biz_baseline_ok(biz_base)
    if not ok:
        print("✗ 业务探针基线不可用：%s" % why)
        load.stop()
        return 3

    # ── 故障前预取宠物：解开探针与被测依赖的耦合，见模块 docstring ──
    pre_pet = None
    try:
        _st, _html = _get("/?userId=preseed-blackhole")
        avail = [p for p in parse_pets(_html) if p["available"]]
        pre_pet = avail[0] if avail else None
    except Exception:
        pass
    pk = {"adopt": {"pet": pre_pet}} if pre_pet else None
    if pre_pet:
        print("   已预取宠物 %s/%s 供故障期使用（首页调 petsearch，"
              "不预取就会卡在前置条件上）" % (pre_pet["id"], pre_pet["pettype"]))
    else:
        print("   ⚠️ 预取宠物失败 —— 故障期 adopt 探针可能卡在前置条件上")

    if not apply:
        print()
        print("[dry-run] 基线合格，具备执行条件。加 --apply 真跑。")
        load.stop()
        return 0

    biz_during = None
    try:
        print()
        print("── 2. 注入（选择器 → 黑洞）──")
        ok2, msg = _set_selector(svc_name, {"app": _BLACKHOLE})
        if not ok2:
            print("✗ 施加失败: %s" % msg)
            load.stop()
            return 4
        print("   已施加。等端点清空…")
        for _ in range(12):
            time.sleep(5)
            n = _endpoint_count(svc_name)
            if n == 0:
                print("   ✓ 端点已清空（0 个）—— 切断生效")
                break
            print("      端点仍有 %d 个…" % n)
        else:
            print("   ⚠️ 端点未清空 —— 切断可能未生效")

        print("   轮询业务探针（预算 %ds）" % _POLL_BUDGET)
        worst, waited = None, 0
        while waited < _POLL_BUDGET:
            time.sleep(_POLL_SECONDS)
            waited += _POLL_SECONDS
            cur = dm._probe_business(service, n=1, probe_kwargs=pk)
            deg, brk, note = dm._biz_degraded(biz_base, cur)
            print("      [+%3ds] %s" % (waited, note))
            if deg:
                worst = cur
                print("      ✓ 捕捉到业务退化（第 %ds）" % waited)
                break
        biz_during = worst or dm._probe_business(service, n=2, probe_kwargs=pk)

        print("   保持 %ds 让 X-Ray 聚合…" % hold)
        time.sleep(hold)
        during = _measure(service, target, window)
        print("   被测边（故障期）: %s" % during)
    finally:
        print()
        print("── 3. 回滚（finally，任何路径都执行）──")
        ok3, msg3 = _set_selector(svc_name, orig)
        print("   恢复选择器: %s" % ("成功" if ok3 else "✗ 失败 %s" % msg3))
        if not ok3:
            print("   ⚠️⚠️ 手工恢复（不做的话该服务一直无后端）：")
            print("   kubectl patch svc %s -n %s --type=merge -p '%s'"
                  % (svc_name, _NAMESPACE,
                     json.dumps({"spec": {"selector": orig}})))
        for _ in range(12):
            time.sleep(5)
            if _endpoint_count(svc_name) > 0:
                print("   端点已恢复（%d 个）" % _endpoint_count(svc_name))
                break
        print("   背景流量: %s" % load.stats)

    print()
    print("── 4. 恢复确认（连续 %d 次正常）──" % _RECOVERY_STREAK)
    biz_post, recovered = dm._wait_recovery(service, biz_base)
    print("   %s" % biz_post.get("detail"))
    post = _measure(service, target, window)

    verdict, why2 = dm._verdict(base, during, post, biz_base, biz_during,
                               biz_post)
    print()
    print("── 判定 ──")
    print("   %s：%s" % (verdict, why2))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_p = _ROOT / "todo" / ("svc-blackhole-probe_%s.json" % stamp)
    out_p.parent.mkdir(exist_ok=True)
    out_p.write_text(json.dumps({
        "taken_at": stamp, "service": service, "target_label": "Microservice",
        "target": target, "severed_service": svc_name,
        "severance": "k8s-service-blackhole",
        "original_selector": orig,
        "channel_edge": {"baseline": base, "during": during, "post": post},
        "channel_business": {"baseline": biz_base, "during": biz_during,
                             "post": biz_post},
        "verdict": verdict, "why": why2,
        "caveat": ("Service 选择器黑洞使调用方连接立即失败，覆盖「依赖不可用」；"
                   "不覆盖延迟升高与部分失败。"
                   "故障期 adopt 探针使用故障前预取的宠物，"
                   "以隔离发现路径与支付路径"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("   记录: %s" % out_p.relative_to(_ROOT))

    persisted = dm._persist_verdict(
        service, "Microservice", target, verdict, why2, base, during or {},
        out_p.stem, severance="k8s-service-blackhole",
        evidence_channel="xray-edge+business-probe")
    print("   写回: %s" % persisted)
    load.stop()
    print("   背景流量已停: %s" % load.stats)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--service", required=True, help="源服务（图谱名）")
    ap.add_argument("--target", required=True, help="目标服务（图谱名）")
    ap.add_argument("--k8s-service", required=True,
                    help="要黑洞的 K8s Service 名（**现查，不要凭记忆写**）")
    ap.add_argument("--window", type=int, default=180)
    ap.add_argument("--warmup", type=int, default=210)
    ap.add_argument("--hold", type=int, default=200)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.warmup and a.warmup < a.window:
        print("✗ --warmup < --window：基线窗口跑不满，拒绝开跑")
        return 2
    return run(a.service, a.target, a.k8s_service, a.window,
               a.warmup, a.hold, a.apply)


if __name__ == "__main__":
    raise SystemExit(main())

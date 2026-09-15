#!/usr/bin/env python3.11
"""用 AWS FIS 的 Aurora 实例重启验证 RDS 依赖边。

## 为什么不是 Aurora 的故障注入查询

用户给的第一个参考是 Aurora 的 `ALTER SYSTEM CRASH` / `SIMULATE DISK FAILURE`
那一套。**它是 Aurora MySQL 专有的** —— 文档原文写的是「A crash of the
MySQL-compatible database」。本集群是 `aurora-postgresql 16.11`，
这些语句在 PostgreSQL 上没有实现，跑了只会拿到语法错误。

文档自己指了路：要测故障转移用 `failover-db-cluster`。第二个参考
（fis-template-library 的 aurora-postgres-cluster-loadtest-failover）
正是走 FIS 的 `aws:rds:failover-db-cluster`。这里用同族的
`aws:rds:reboot-db-instances`，因为它更贴近要验证的命题：
**让写实例真的短暂不可用**，而 failover 只是把写角色挪到另一个实例。

## 与参考模板的两处刻意偏离

1. **不用它的 EC2 + SSM 合成负载。** 那段会在库里建
   `load_test_users` / `load_test_transactions` 两张表并**持久留下**
   （README 自己写明）。本项目已有驱动真实业务的负载器，压的正是要测量的
   业务功能 —— 那比合成 SQL 负载更贴近证据，也不污染数据库。
2. **补上停止条件。** 参考模板的 `stopConditions` 是 `[{"source":"none"}]`，
   README 也承认默认没有。对一个动实时数据库的实验来说这是真缺口。
   这里挂上集群已有的两个 CloudWatch 告警。

## 目标必须现取 —— 现成实验踩过这个坑

集群里已有的 EXP-001「Aurora PG Writer Instance Reboot」把目标 ARN 硬编码成
`...databasewriter2462cc03...`，而**那个实例现在是 reader**
（`IsClusterWriter=false`）；真正的 writer 是名字里写着 reader 的那个。
角色随某次故障转移换了，实验目标没跟着换 —— 于是一个叫「重启 writer」的实验
实际重启 reader，测的是另一种故障，却会以「已验证数据层恢复」的名义出报告。

所以这里每次运行都**现查当前 writer**，并与模板目标核对；不一致就拒绝开跑。

## 判定必须是不对称的 —— 瞬时故障不等于切断

重启是**瞬时中断**（约 30~60 秒），不是「移除这个依赖」。所以：

    业务退化   → confirmed。依赖在关键路径上，且应用扛不住写实例短暂丢失。
                 但 `severance=rds-reboot` 必须落在边上，报告据此披露
                 证据范围是「瞬时写实例丢失」，**不覆盖「数据库彻底不可用」**。
    业务未退化 → **inconclusive，不是「不依赖」**。只能说明应用能吸收瞬时中断，
                 完全推不出「没有这个依赖也行」。

IAM deny 那条路的判定是对称的（deny 期间调用一直失败），这里不是。
把这个不对称写死在代码里，否则下一个人会照抄对称逻辑并得出反向结论。

## 证据覆盖哪条边

重启一个实例，证据覆盖的是**目标名等于该实例标识符**的那条图谱边，
不是该服务的两条 RDS 边都覆盖。⚠️ 注意图谱节点名里的 reader/writer 字样
**不可信**（见上文角色漂移），要按实例标识符对齐，别按名字里的词。
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

REGION = "ap-northeast-1"
CLUSTER = "serviceseks2-databaseb269d8bb-efjeyzicx2ak"

#: X-Ray 里 SQL 边的节点形态。**两种都要**：
#: payforadoption(Go) 记成 `postgres`(Type=Database::SQL)，
#: petlistadoptions(.NET) 记成 `PGSQL Query`(Type=remote)。
#: `remote` 太泛（`HTTP GET` 也是 remote），所以后者只能按名字认。
SQL_NODE_NAMES = ("postgres", "PGSQL Query")
SQL_NODE_TYPES = ("Database::SQL",)

MIN_BASELINE_REQUESTS = 20
MIN_BASELINE_SUCCESS_RATE = 95.0

#: 故障期业务轮询：重启约 30~60s，轮询要密到能抓住它。
_FAULT_POLL_SECONDS = 10
_FAULT_POLL_BUDGET = 300

_RECOVERY_STREAK = 8
_RECOVERY_BUDGET_SECONDS = 420


def _iam_deny_module():
    """按文件路径加载 IAM deny 实验器，复用它的负载驱动与判据函数。

    那些部件（`_LoadDriver` / `_measure` / `_biz_baseline_ok` / `_biz_degraded`）
    本该在共享模块里，而不是从一个 CLI 脚本里 import。**这是明知的技术债**：
    那个脚本有 13 条门禁盯着，为了不动它而在这里 import，
    比现在做一次大搬迁更不容易出错。搬迁应当单独做并连门禁一起改。
    """
    p = _ROOT / "scripts" / "verify_via_iam_deny.py"
    spec = importlib.util.spec_from_file_location("_iam_deny", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _aws(*args) -> tuple[dict | None, str]:
    r = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:300]
    try:
        return (json.loads(r.stdout) if r.stdout.strip() else {}), ""
    except Exception as e:
        return None, "解析失败 %r" % e


def cluster_roles() -> dict:
    """现查两个实例的角色，返回 `{实例标识符: "writer"|"reader"}`。

    硬编码角色就是 EXP-001 那个漂移缺陷：它的目标 ARN 写死成
    `...databasewriter2462cc03...`，而那个实例现在是 **reader**。
    """
    out, err = _aws("rds", "describe-db-clusters",
                    "--db-cluster-identifier", CLUSTER)
    if not out:
        print("   ✗ 查集群失败: %s" % err)
        return {}
    roles = {}
    for m in (out.get("DBClusters") or [{}])[0].get("DBClusterMembers") or []:
        roles[m.get("DBInstanceIdentifier")] = (
            "writer" if m.get("IsClusterWriter") else "reader")
    return roles


def template_target(tpl_id: str) -> str | None:
    """取模板的目标实例标识符。"""
    out, err = _aws("fis", "get-experiment-template", "--id", tpl_id)
    if not out:
        print("   ✗ 取模板失败: %s" % err)
        return None
    tgts = (out.get("experimentTemplate") or {}).get("targets") or {}
    for t in tgts.values():
        for arn in t.get("resourceArns") or []:
            return arn.rsplit(":", 1)[-1]
    return None


def _measure_sql(svc: str, target: str, window: int, m=None) -> dict:
    """量 `svc -> SQL` 这条边。目标名只用于日志 —— 匹配靠节点形态。

    ⚠️ 这里有一个必须写明的**过度声称**：X-Ray 只有一个 SQL 节点，
    reader 与 writer 分不开。所以这个数字是「该服务对集群的 SQL 调用」，
    不是「对某一个实例的调用」。判定文本里要照实说。
    """
    from runner.xray_metrics import XRayEdgeMetrics
    snap = (m or XRayEdgeMetrics()).collect_edge_flow(
        svc, target, window_seconds=window,
        dst_type_prefixes=SQL_NODE_TYPES, dst_names=SQL_NODE_NAMES)
    return {"ok": bool(getattr(snap, "ok", False)),
            "success_rate": getattr(snap, "success_rate", None),
            "total_requests": getattr(snap, "total_requests", None)}


def _verdict(base: dict, during: dict, post: dict,
             biz_note: str, degraded: bool, broke: bool,
             recovered: bool, instance: str, role: str) -> tuple[str, str]:
    """瞬时故障的**不对称**判定。见模块 docstring。

    ⚠️ `role` 必须传入并写进证据范围文案，**不能写死「写实例」**。
    2026-09-15 踩到：文案里硬编码了「瞬时写实例丢失」，而 reader 重启那轮
    把这句话落到了边上 —— 读报告的人会以为测的是写路径。
    这与把 `severance` 硬编码成 `iam-deny` 是同一类错误：
    **落在合规产物上的范围声明写错，就是虚假陈述。**
    """
    role_cn = "写实例" if role == "writer" else "读实例"
    scope = ("；证据范围＝瞬时%s丢失（重启 %s，当时角色 %s），"
             "**不覆盖「数据库彻底不可用」**" % (role_cn, instance, role))
    rec = "；故障后恢复" if recovered else "；⚠️ 未在预算内恢复"

    if not during.get("ok"):
        return "observation_only", (
            "故障期 SQL 边采集失败（ok=False）—— 无法判定注入是否落在被测路径上")

    b_sr = base.get("success_rate")
    d_sr = during.get("success_rate")
    if b_sr is None or d_sr is None:
        return "observation_only", "成功率缺值，无法判定"
    drop = b_sr - d_sr

    if broke or degraded:
        return "confirmed", (
            "重启期间业务退化（%s），SQL 边成功率 %.1f%% → %.1f%%（降 %.1fpp）%s%s"
            % (biz_note, b_sr, d_sr, drop, rec, scope))

    if drop >= 5:
        return "inconclusive", (
            "SQL 边出现失败（降 %.1fpp）但业务未退化（%s）—— "
            "应用吸收了瞬时%s丢失。**这不等于不依赖**："
            "只证明它扛得住短暂中断，推不出「没有这个依赖也行」%s"
            % (drop, biz_note, role_cn, scope))

    return "observation_only", (
        "SQL 边未见明显失败（降 %.1fpp）且业务未退化（%s）—— "
        "重启可能未落在测量窗口内，或连接池把它整个吸收了。"
        "信号不足以判定，拒绝出结论" % (drop, biz_note))


def run(service: str, target: str, tpl_id: str, expect_role: str,
        window: int, warmup: int, apply: bool) -> int:
    dm = _iam_deny_module()

    print("目标边: %s -> RDSInstance(%s)" % (service, target))
    roles = cluster_roles()
    tgt = template_target(tpl_id)
    print("集群实例角色（现取）: %s" % roles)
    print("模板 %s 的目标: %s（当前角色 %s）"
          % (tpl_id, tgt, roles.get(tgt, "?")))
    if not roles or not tgt:
        print("✗ 取不到角色或模板目标 —— 中止（不猜）")
        return 2

    # ── 三重守卫，防的是 EXP-001 那个漂移缺陷 ──
    #
    # 那个实验把目标 ARN 写死成 `...databasewriter2462cc03...`，而角色随某次
    # 故障转移换了，如今那个实例是 **reader**。于是一个叫「重启 writer」的实验
    # 实际重启 reader，测的是另一种故障，却会以「已验证数据层恢复」出报告。
    #
    # 所以每次运行都核对三件事：模板目标的**当前角色** == 声明的角色；
    # 被验证边的目标 == 模板目标；两者都取自现查而不是任何写死的名字。
    # ⚠️ 实例**名字**里的 reader/writer 字样不可信 —— 名字没跟着角色换。
    if roles.get(tgt) != expect_role:
        print("✗ 模板目标当前角色是 %r，而声明要打的是 %r —— 拒绝开跑。"
              % (roles.get(tgt), expect_role))
        print("  这正是 EXP-001 踩过的坑：角色换了、目标没跟着换。")
        print("  处置：重建模板指向当前的 %s 实例。" % expect_role)
        return 2
    if target != tgt:
        print("✗ 被验证的边指向 %s，而模板重启的是 %s —— 拒绝开跑。"
              % (target, tgt))
        print("  证据只覆盖被重启实例对应的那条边，不能张冠李戴。")
        return 2
    writer = tgt          # 下文沿用：本次实际被重启的实例

    from runner.business_probes import probes_for
    if not probes_for(service):
        print("✗ 源服务 %s 未登记业务探针 —— 拒绝开跑" % service)
        print("  没有业务证据的 confirmed 是过度声称。")
        return 2
    pnames = [n for n, _ in probes_for(service)]
    print("业务探针: %s" % pnames)
    print()

    load = dm._LoadDriver()
    if apply and warmup > 0:
        print("── 0. 背景流量 ──")
        load.start()
        print("   lead-in %ds 让基线窗口跑满…" % warmup)
        time.sleep(warmup)
        print("   %s" % load.stats)
        print()

    print("── 1. 基线 ──")
    base = _measure_sql(service, target, window)
    print("   SQL 边: %s" % base)
    biz_base = dm._probe_business(service)
    print("   业务探针: %s" % biz_base["detail"])
    if not base["ok"]:
        print("✗ SQL 边基线采集失败（ok=False）—— 中止")
        load.stop()
        return 3
    if (base["total_requests"] or 0) < MIN_BASELINE_REQUESTS:
        print("✗ 基线请求数 %s < %d —— 拒绝出判定"
              % (base["total_requests"], MIN_BASELINE_REQUESTS))
        load.stop()
        return 3
    if (base["success_rate"] or 0) < MIN_BASELINE_SUCCESS_RATE:
        print("✗ 基线成功率 %.2f%% < %.0f%% —— 拒绝开跑（基线本身是坏的）"
              % (base["success_rate"] or 0, MIN_BASELINE_SUCCESS_RATE))
        load.stop()
        return 3
    ok, why = dm._biz_baseline_ok(biz_base)
    if not ok:
        print("✗ 业务探针基线不可用：%s" % why)
        load.stop()
        return 3

    if not apply:
        print()
        print("[dry-run] 两个通道基线都合格，具备执行条件。加 --apply 真跑。")
        load.stop()
        return 0

    lock = None
    try:
        from importlib import import_module
        lock = import_module("chaos_lock")
    except Exception:
        pass

    exp_id = None
    biz_during = None
    try:
        print()
        print("── 2. 注入（FIS 重启当前 %s 实例 %s）──" % (expect_role, writer))
        out, err = _aws("fis", "start-experiment",
                        "--experiment-template-id", tpl_id)
        if not out:
            print("✗ 启动实验失败: %s" % err)
            load.stop()
            return 4
        exp_id = (out.get("experiment") or {}).get("id")
        print("   实验 %s 已启动" % exp_id)

        print("   轮询业务探针直到退化（不猜延迟；预算 %ds）" % _FAULT_POLL_BUDGET)
        worst = None
        waited = 0
        while waited < _FAULT_POLL_BUDGET:
            time.sleep(_FAULT_POLL_SECONDS)
            waited += _FAULT_POLL_SECONDS
            cur = dm._probe_business(service, n=1)
            deg, brk, note = dm._biz_degraded(biz_base, cur)
            print("      [+%3ds] %s" % (waited, note))
            if deg and worst is None:
                worst = cur
                print("      ✓ 捕捉到业务退化（第 %ds）" % waited)
                break
        biz_during = worst or dm._probe_business(service, n=3)

        print("   等 %ds 让 X-Ray 聚合故障期样本…" % window)
        time.sleep(window)
        during = _measure_sql(service, target, window)
        print("   SQL 边（故障期）: %s" % during)
    finally:
        print()
        print("── 3. 收尾（finally）──")
        if exp_id:
            out, _ = _aws("fis", "get-experiment", "--id", exp_id)
            st = ((out or {}).get("experiment") or {}).get("state") or {}
            print("   实验状态: %s（%s）"
                  % (st.get("status"), str(st.get("reason"))[:80]))
        print("   背景流量: %s" % load.stats)
        r2 = cluster_roles()
        print("   重启后角色: %s%s"
              % (r2, "（未变）" if r2 == roles else "（⚠️ 角色已切换）"))

    print()
    print("── 4. 恢复确认（连续 %d 次正常）──" % _RECOVERY_STREAK)
    biz_post, recovered = dm._wait_recovery(service, biz_base)
    print("   %s" % biz_post.get("detail"))
    if not recovered:
        print("   ⚠️⚠️ 未在预算内恢复 —— 线上仍退化，需人工介入")
    post = _measure_sql(service, target, window)

    degraded, broke, biz_note = dm._biz_degraded(biz_base, biz_during)
    verdict, why = _verdict(base, during, post, biz_note, degraded, broke,
                            recovered, writer, expect_role)
    print()
    print("── 判定 ──")
    print("   %s：%s" % (verdict, why))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_p = _ROOT / "todo" / ("rds-fault-probe_%s.json" % stamp)
    out_p.parent.mkdir(exist_ok=True)
    out_p.write_text(json.dumps({
        "taken_at": stamp, "service": service, "target_label": "RDSInstance",
        "target": target, "fis_template": tpl_id, "fis_experiment": exp_id,
        "rebooted_instance": writer, "rebooted_role": expect_role,
        "severance": "rds-reboot",
        "channel_edge": {"baseline": base, "during": during, "post": post},
        "channel_business": {"baseline": biz_base, "during": biz_during,
                             "post": biz_post},
        "verdict": verdict, "why": why,
        "caveat": ("瞬时实例重启，不覆盖「数据库彻底不可用」；"
                   "X-Ray 只有一个 SQL 节点，reader/writer 分不开，"
                   "边计数是该服务对集群的 SQL 调用"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("   记录: %s" % out_p.relative_to(_ROOT))

    # severance 必须传 —— 默认值是 iam-deny，不传就会在边上错标证据来源。
    persisted = dm._persist_verdict(service, "RDSInstance", target, verdict,
                                    why, base, during, out_p.stem,
                                    severance="rds-reboot")
    print("   写回: %s" % persisted)
    if lock:
        try:
            lock.end()
        except Exception:
            pass
    load.stop()
    print("   背景流量已停: %s" % load.stats)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--service", required=True, help="源服务（图谱名）")
    ap.add_argument("--target", required=True, help="RDS 实例标识符（图谱目标名）")
    ap.add_argument("--template", required=True, help="FIS 实验模板 id")
    ap.add_argument("--expect-role", required=True, choices=("writer", "reader"),
                    help="模板目标**当前**应当扮演的角色。必须显式声明 —— "
                         "实例名字里的 reader/writer 字样不可信（名字不随角色换）")
    ap.add_argument("--window", type=int, default=180, help="观测窗口秒数")
    ap.add_argument("--warmup", type=int, default=210,
                    help="背景流量 lead-in，必须 ≥ --window")
    ap.add_argument("--apply", action="store_true", help="真跑（默认 dry-run）")
    a = ap.parse_args()
    if a.warmup and a.warmup < a.window:
        print("✗ --warmup(%d) < --window(%d)：基线窗口跑不满，拒绝开跑"
              % (a.warmup, a.window))
        return 2
    return run(a.service, a.target, a.template, a.expect_role,
               a.window, a.warmup, a.apply)


if __name__ == "__main__":
    raise SystemExit(main())

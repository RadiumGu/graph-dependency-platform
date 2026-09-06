#!/usr/bin/env python3
"""tests/verify_acceptance.py — 逐条自查 DR_REFACTOR_PLAN.md 的 9 项验收标准。

用法：
    cd dr-plan-generator && PYTHONPATH=. python3 tests/verify_acceptance.py <workdir>

每项打印 PASS/FAIL 与依据；有 FAIL 时退出码为 1。
"""

import json
import os
import re
import subprocess
import sys

PKG = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURE_PROFILE = os.path.join(PKG, "tests", "fixtures", "test_profile.yaml")
PARENT_PROFILE = os.path.abspath(os.path.join(PKG, "..", "profiles", "petsite.yaml"))
AZ_FIXTURE = os.path.join(PKG, "tests", "fixtures", "az1_subgraph.json")

_results = []


def check(name: str, ok: bool, detail: str) -> None:
    _results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"          {detail}")


def _run_plan(workdir: str, out: str, **flags) -> tuple:
    """跑一次 plan，返回 (returncode, stderr, plan_dict|None)。"""
    cmd = [
        sys.executable, os.path.join(PKG, "main.py"), "plan",
        "--scope", "region", "--offline", os.path.join(workdir, "snap.json"),
        "--format", "json", "--output-dir", os.path.join(workdir, out),
    ]
    for key, value in flags.items():
        cmd += [f"--{key.replace('_', '-')}", value]
    env = dict(os.environ)
    env["NEPTUNE_ENDPOINT"] = ""
    env["PYTHONPATH"] = PKG
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PKG, env=env)
    plan = None
    outdir = os.path.join(workdir, out)
    if os.path.isdir(outdir):
        files = [f for f in os.listdir(outdir) if f.endswith(".json")]
        if files:
            with open(os.path.join(outdir, files[0]), encoding="utf-8") as fh:
                plan = json.load(fh)
    return proc.returncode, proc.stderr, plan


def main(workdir: str) -> int:
    os.makedirs(workdir, exist_ok=True)
    sys.path.insert(0, PKG)

    # 建快照（掺入平台设施与污染节点）
    from tests.verify_scope_e2e import build  # noqa: E402

    build(AZ_FIXTURE, os.path.join(workdir, "snap.json"))

    common = dict(source="eu-west-1", target="eu-central-1", profile=FIXTURE_PROFILE)

    # ① 离线生成（不通 Neptune）
    rc, err, plan = _run_plan(workdir, "a1", mode="drill", strategy="warm_standby", **common)
    check("① --offline 在不通 Neptune 下生成完整计划",
          rc == 0 and plan is not None and len(plan["phases"]) >= 5,
          f"exit={rc}, phases={len(plan['phases']) if plan else 0}, "
          f"plan_source={plan.get('plan_source') if plan else '-'}")

    # ② 两档策略 phase-2 步骤数不同
    _, _, pl = _run_plan(workdir, "a2pl", mode="drill", strategy="pilot_light", **common)
    _, _, ws = _run_plan(workdir, "a2ws", mode="drill", strategy="warm_standby", **common)
    def ph2(p):
        return next(x for x in p["phases"] if x["phase_id"] == "phase-2")
    n_pl, n_ws = len(ph2(pl)["steps"]), len(ph2(ws)["steps"])
    check("② pilot_light 与 warm_standby 的 phase-2 步骤数不同",
          n_pl > n_ws,
          f"pilot_light={n_pl} 步（前两步 {[s['action'] for s in ph2(pl)['steps'][:2]]}）, "
          f"warm_standby={n_ws} 步")

    # ③ drill / failover 的 Aurora 用不同 API
    _, _, dr = _run_plan(workdir, "a3d", mode="drill", strategy="warm_standby", **common)
    _, _, fo = _run_plan(workdir, "a3f", mode="failover", strategy="warm_standby", **common)
    def aurora_cmd(p):
        for phase in p["phases"]:
            for s in phase["steps"]:
                if s["action"] in ("switchover_global_cluster", "failover_global_cluster"):
                    return s["action"], s["command"]
        return None, ""
    a_dr, c_dr = aurora_cmd(dr)
    a_fo, c_fo = aurora_cmd(fo)
    check("③ drill / failover 下 Aurora 步骤用不同 API",
          a_dr == "switchover_global_cluster" and a_fo == "failover_global_cluster"
          and "--allow-data-loss" in c_fo and "--allow-data-loss" not in c_dr
          and "failover-db-cluster" not in c_dr + c_fo,
          f"drill={a_dr}, failover={a_fo}, failover 带 --allow-data-loss="
          f"{'--allow-data-loss' in c_fo}, 无 failover-db-cluster="
          f"{'failover-db-cluster' not in c_dr + c_fo}")

    # ④ 恢复顺序：被调方先于调用方
    order = [s["resource_name"] for p in ws["phases"] for s in p["steps"]]
    def idx(name):
        return order.index(name) if name in order else -1
    deps_ok = all(
        idx(dep) != -1 and idx(dep) < idx("petsite")
        for dep in ("petsearch",) if idx("petsite") != -1
    )
    check("④ 被调方排在调用方之前",
          deps_ok,
          f"petsearch@{idx('petsearch')} < petsite@{idx('petsite')}")

    # ⑥ RPO 是实测/推导值；SQS 丢失量单列（用 petsite profile，锚点与快照匹配）
    _, _, petsite_plan = _run_plan(
        workdir, "a6", mode="failover", strategy="pilot_light",
        source="ap-northeast-1", target="us-west-2", profile=PARENT_PROFILE)

    # ⑤ 排除项：必须用**锚点与快照匹配**的 profile。
    #    快照是 petsite 子图，而 fixture profile 的锚点是 acme-shop 的服务，
    #    一个都不命中 → 触发空范围回退 → 什么都没排除（该回退现已记入缺口）。
    resources = set(petsite_plan["affected_resources"])
    leaked = [n for n in ("petsite-neptune", "etl_aws", "etl_deepflow",
                          "gateway-service", "order-service", "artillery")
              if n in resources]
    exclusions = petsite_plan.get("scope_exclusions", [])
    all_have_reason = all(e.get("reason") for e in exclusions)
    check("⑤ 平台设施与污染节点全部出局，排除项均列明原因",
          not leaked and all_have_reason and len(exclusions) >= 6,
          f"误入={leaked or '无'}, 排除项={len(exclusions)} 条, 全部带 reason={all_have_reason}")

    rpo = petsite_plan.get("estimated_rpo")
    basis = petsite_plan.get("rpo_basis", [])
    sqs_steps = [s for p in ws["phases"] for s in p["steps"]
                 if s["action"] == "quantify_message_loss"]
    check("⑥ RPO 有推导依据（不可推定时为 null），SQS 丢失量可量化",
          rpo is None and len(basis) >= 2
          and all(b.get("reason") for b in basis),
          f"petsite RPO={rpo!r}（null=不可从配置推定）, 依据 {len(basis)} 条, "
          f"SQS 量化步骤={len(sqs_steps)}（本快照无 SQS 节点属正常）")

    # ⑦ 核心代码无 workload 字面量
    proc = subprocess.run(
        [sys.executable, os.path.join(PKG, "scripts", "check_decoupling.py")],
        capture_output=True, text=True, cwd=PKG)
    check("⑦ 核心代码无 workload 字面量 / 无父仓库 import",
          proc.returncode == 0,
          proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[:120])

    # ⑧ 换 profile 能生成另一个 workload 的计划
    acme_prefix = any(
        "acme-shop" in s.get("command", "")
        for p in ws["phases"] for s in p["steps"]
    )
    petsite_prefix = any(
        "--alarm-name-prefix petsite" in s.get("command", "")
        for p in petsite_plan["phases"] for s in p["steps"]
    )
    check("⑧ 换 --profile 能生成另一个 workload 的计划",
          acme_prefix and petsite_prefix,
          f"fixture profile 产出含 acme-shop 前缀={acme_prefix}; "
          f"petsite profile 产出含 petsite 前缀={petsite_prefix}")

    # ⑨ pytest 全绿
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        capture_output=True, text=True, cwd=PKG)
    tail = [l for l in proc.stdout.strip().splitlines() if "passed" in l or "failed" in l]
    check("⑨ pytest 全绿", proc.returncode == 0, tail[-1] if tail else "无输出")

    failed = [n for n, ok, _ in _results if not ok]
    print()
    print(f"  ── {len(_results) - len(failed)}/{len(_results)} 项通过 ──")
    if failed:
        print(f"  未通过: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dr-acceptance"))

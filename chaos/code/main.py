"""
main.py - chaos-automation CLI 入口

用法：
  python main.py setup                                         # 建表
  python main.py run --file experiments/tier0/petsite-pod-kill.yaml
  python main.py run --file ... --dry-run                     # 不实际注入
  python main.py history --service petsite --limit 10
"""
from __future__ import annotations
import argparse
import logging
import signal
import sys
import os

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROJECT_ROOT))
# 让 engines 包可 import（它在 rca/engines/）
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")
if os.path.abspath(_RCA_ROOT) not in sys.path:
    sys.path.insert(0, os.path.abspath(_RCA_ROOT))

from shared import get_region

# 确保 code/ 在 path 上
sys.path.insert(0, os.path.dirname(__file__))
# 忽略 SIGPIPE：父进程（Slack agent/shell）关闭管道时，进程继续运行完成实验并写 DynamoDB
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chaos-runner")


def cmd_setup(args):
    """建表"""
    from infra.dynamodb_setup import create_table
    create_table()


def parse_tags(tags_str: str) -> dict:
    """解析 key=value,key2=value2 格式的 tag 字符串"""
    if not tags_str:
        return {}
    tags = {}
    for pair in tags_str.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            tags[k.strip()] = v.strip()
    return tags


def cmd_run(args):
    """执行实验"""
    from runner import load_experiment, ExperimentRunner

    if not os.path.exists(args.file):
        print(f"❌ 文件不存在: {args.file}")
        sys.exit(1)

    duration_override = getattr(args, "duration", None)
    exp    = load_experiment(args.file, duration_override=duration_override)
    tags   = parse_tags(getattr(args, "tags", None))
    runner = ExperimentRunner(dry_run=args.dry_run, tags=tags)

    logger.info(f"实验: {exp.name}")
    logger.info(f"目标: {exp.target_service} ({exp.target_tier})")

    # 组合实验显示 action 列表
    from runner.experiment import CompositeExperiment
    if isinstance(exp, CompositeExperiment):
        logger.info(f"类型: 组合实验 ({len(exp.actions)} actions)")
        for a in exp.actions:
            logger.info(f"  → {a.id}: {a.type} ({a.backend}) start={a.start}")
    else:
        logger.info(f"故障: {exp.fault.type} {exp.fault.mode}={exp.fault.value} 持续 {exp.fault.duration}")

    if args.dry_run:
        logger.info("⚡ [dry-run 模式] 不执行实际注入")

    result = runner.run(exp)

    # 退出码：PASSED=0, 其他=1
    sys.exit(0 if result.status == "PASSED" else 1)


def cmd_history(args):
    """查询历史实验记录"""
    import boto3, json
    from datetime import datetime, timezone, timedelta

    ddb = boto3.client("dynamodb", region_name=get_region())
    try:
        resp = ddb.query(
            TableName="chaos-experiments",
            IndexName="target_service-start_time-index",
            KeyConditionExpression="target_service = :svc",
            ExpressionAttributeValues={":svc": {"S": args.service}},
            ScanIndexForward=False,   # 降序（最新在前）
            Limit=args.limit,
        )
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        sys.exit(1)

    items = resp.get("Items", [])
    if not items:
        print(f"暂无 {args.service} 的实验记录")
        return

    print(f"\n{'='*80}")
    print(f"服务 {args.service} 最近 {len(items)} 次实验记录")
    print(f"{'='*80}")
    for item in items:
        exp_id  = item.get("experiment_id", {}).get("S", "")
        st      = item.get("start_time", {}).get("S", "")[:19]
        status  = item.get("status", {}).get("S", "")
        ft      = item.get("fault_type", {}).get("S", "")
        dur     = item.get("duration_seconds", {}).get("N", "0")
        min_sr  = item.get("impact_min_success_rate", {}).get("N", "-")
        rec_sec = item.get("recovery_seconds", {}).get("N", "-")
        rca_m   = item.get("rca_match", {}).get("BOOL")
        icon    = {"PASSED": "✅", "FAILED": "❌", "ABORTED": "🛑", "ERROR": "💥"}.get(status, "❓")
        rca_icon = ("✅" if rca_m else "❌") if rca_m is not None else "-"
        print(f"{icon} {st}  {fault_type_pad(ft)}  min_sr={min_sr}%  recovery={rec_sec}s  rca={rca_icon}  [{exp_id}]")

    print()


def fault_type_pad(s: str, width: int = 20) -> str:
    return s.ljust(width)[:width]


def cmd_suite(args):
    """批量编排实验"""
    from orchestrator import WorkflowOrchestrator, collect_yamls

    paths = collect_yamls(args.dir)
    if not paths:
        print(f"❌ 目录下无 YAML 文件: {args.dir}")
        sys.exit(1)

    tags = parse_tags(getattr(args, "tags", None))
    orch = WorkflowOrchestrator(dry_run=args.dry_run, tags=tags)
    result = orch.run_suite(
        experiment_paths=paths,
        strategy=args.strategy,
        max_parallel=args.max_parallel,
        stop_on_failure=args.stop_on_failure,
        cooldown=args.cooldown,
        top=args.top,
        dry_run=args.dry_run,
    )
    print(result.summary())
    sys.exit(0 if result.failed + result.aborted + result.errors == 0 else 1)


def cmd_auto(args):
    """端到端自动化：假设生成 → YAML → 执行"""
    from orchestrator import WorkflowOrchestrator

    tags = parse_tags(getattr(args, "tags", None))
    prepare_only = getattr(args, "prepare_only", False)
    orch = WorkflowOrchestrator(dry_run=args.dry_run or prepare_only, tags=tags)
    result = orch.generate_and_run(
        max_hypotheses=args.max_hypotheses,
        top_n=args.top,
        max_parallel=args.max_parallel,
        stop_on_failure=args.stop_on_failure,
        cooldown=args.cooldown,
        dry_run=args.dry_run,
        prepare_only=prepare_only,
    )
    if prepare_only:
        print("\n实验已准备完毕，执行请运行:")
        print("  python3 main.py suite --dir experiments/generated/\n")
    print(result.summary())
    sys.exit(0 if result.failed + result.aborted + result.errors == 0 else 1)


def cmd_learn(args):
    """闭环学习分析"""
    from agents import LearningAgent

    agent = LearningAgent()
    service = None if args.all else args.service
    if not service and not args.all:
        print("❌ 请指定 --service 或 --all")
        sys.exit(1)

    report = agent.run(
        service=service,
        limit=args.limit,
        output=args.output,
        update_neptune=not args.no_graph_update,
    )

    print(f"\n{'='*60}")
    print(f"📊 学习分析完成")
    print(f"{'='*60}")
    print(f"  实验总数: {report.total_experiments}")
    print(f"  通过率:   {report.pass_rate}%")
    print(f"  平均恢复: {report.avg_recovery_seconds}s")
    if report.repeated_failures:
        print(f"  ⚠️  重复失败: {len(report.repeated_failures)} 个模式")
    if report.coverage_gaps:
        print(f"  🔍 覆盖空白: {len(report.coverage_gaps)} 个服务")
    if report.new_hypotheses:
        print(f"  🆕 新假设:   {len(report.new_hypotheses)} 个")
    print(f"  📄 报告:     {args.output}")
    print()


def cmd_hypothesis(args):
    """假设生成 / 列表 / 导出"""
    from agents import HypothesisAgent  # 需 .load() / .to_experiment_yamls() 等附属方法
    from engines.factory import make_hypothesis_engine

    agent = make_hypothesis_engine()
    action = args.hypothesis_action

    if action == "generate":
        hypotheses = agent.generate(
            max_hypotheses=args.max,
            service_filter=args.service,
        )
        hypotheses = agent.prioritize(hypotheses)
        agent.save(hypotheses)
        print(f"\n✅ 已生成 {len(hypotheses)} 个假设 → hypotheses.json")
        for h in hypotheses[:10]:
            print(f"  [{h.id}] P{h.priority} {h.title} ({h.failure_domain}/{h.backend})")
        if len(hypotheses) > 10:
            print(f"  ... 共 {len(hypotheses)} 个，详见 hypotheses.json")

    elif action == "list":
        hypotheses = HypothesisAgent.load()
        if not hypotheses:
            print("暂无假设，请先运行: python main.py hypothesis generate")
            return
        print(f"\n{'='*80}")
        print(f"假设库: {len(hypotheses)} 个假设")
        print(f"{'='*80}")
        for h in hypotheses:
            scores = h.priority_scores
            score_str = " ".join(f"{k}={v}" for k, v in scores.items()) if scores else ""
            icon = {"draft": "📝", "tested": "🧪", "validated": "✅", "invalidated": "❌"}.get(h.status, "❓")
            print(f"  {icon} P{h.priority:>3} [{h.id}] {h.title}")
            print(f"       域={h.failure_domain} 后端={h.backend} 服务={h.target_services} {score_str}")

    elif action == "export":
        hypotheses = HypothesisAgent.load()
        if not hypotheses:
            print("暂无假设，请先运行: python main.py hypothesis generate")
            return
        top = hypotheses[:args.top]
        paths = agent.to_experiment_yamls(top, output_dir=args.output_dir)
        print(f"\n✅ 已导出 {len(paths)} 个实验 YAML → {args.output_dir}/")
        for p in paths:
            print(f"  📄 {p}")

    else:
        print("用法: python main.py hypothesis {generate|list|export}")


def cmd_compose(args):
    """CLI 快速组合实验生成（--fault 参数模式）"""
    import yaml
    from datetime import datetime

    target = args.target or "petsite"
    namespace = args.namespace or "petadoptions"
    duration_override = args.duration

    # 解析 --fault 参数
    faults = []
    for i, fault_str in enumerate(args.fault or []):
        fa = _parse_fault_arg(fault_str, i, duration_override)
        faults.append(fa)

    if not faults:
        print("❌ 至少需要一个 --fault 参数")
        sys.exit(1)

    # 构建 YAML dict
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    exp_name = f"compose-{target}-{timestamp}"

    yaml_dict = {
        "name": exp_name,
        "description": f"CLI compose: {len(faults)} actions on {target}",
        "backend": "composite",
        "target": {
            "service": target,
            "namespace": namespace,
            "tier": "Tier0",
        },
        "faults": faults,
        "steady_state": {
            "before": [{"metric": "success_rate", "threshold": ">= 95%", "window": "1m"}],
            "after": [
                {"metric": "success_rate", "threshold": ">= 90%", "window": "5m"},
                {"metric": "latency_p99", "threshold": "< 5000ms", "window": "5m"},
            ],
        },
        "stop_conditions": [
            {"metric": "success_rate", "threshold": "< 30%", "window": "60s", "action": "abort"},
        ],
        "rca": {"enabled": True, "trigger_after": "30s"},
        "options": {"max_duration": args.max_duration or "15m"},
    }

    # 调度模式
    if args.schedule == "sequential":
        # 串行：每个 action 依赖前一个
        for i in range(1, len(faults)):
            faults[i]["start"] = f"after:{faults[i-1]['id']}"

    yaml_output = yaml.dump(yaml_dict, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 保存或执行
    if args.save:
        save_path = args.save
    else:
        save_dir = os.path.join(os.path.dirname(__file__), "experiments", "composite")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{exp_name}.yaml")

    with open(save_path, "w") as f:
        f.write(f"# 自动生成 by compose CLI — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(yaml_output)
    print(f"✅ 已生成: {save_path}")

    if args.run:
        print(f"\n▶️ 执行实验...")
        from runner import load_experiment, ExperimentRunner
        exp = load_experiment(save_path)
        runner = ExperimentRunner(dry_run=args.dry_run, tags={})
        result = runner.run(exp)
        sys.exit(0 if result.status == "PASSED" else 1)
    elif args.dry_run:
        print(f"\n▶️ Dry-run 验证...")
        from runner import load_experiment, ExperimentRunner
        exp = load_experiment(save_path)
        runner = ExperimentRunner(dry_run=True, tags={})
        result = runner.run(exp)
        sys.exit(0 if result.status == "PASSED" else 1)
    else:
        print(f"\n生成的 YAML:\n")
        print(yaml_output)
        print(f"\n执行命令: python3 main.py run --file {save_path}")


def _parse_fault_arg(fault_str: str, index: int, duration_override: str = None) -> dict:
    """
    解析 --fault 参数字符串。

    格式: type:key=val,key=val
    短名简写: delay:3m:200ms → network_delay:duration=3m,latency=200ms
    """
    SHORT_NAMES = {
        "delay": ("network_delay", {"latency": None}),
        "loss": ("network_loss", {"loss": None}),
        "cpu": ("pod_cpu_stress", {"load": None}),
        "mem": ("pod_memory_stress", {"size": None}),
        "kill": ("pod_kill", {}),
    }

    parts = fault_str.split(":", 1)
    type_name = parts[0]

    # 短名展开
    if type_name in SHORT_NAMES:
        real_type, extra_map = SHORT_NAMES[type_name]
        type_name = real_type
        # 简写参数（如 delay:3m:200ms）
        if len(parts) > 1 and "=" not in parts[1]:
            simple_parts = parts[1].split(":")
            params = {"duration": simple_parts[0]} if simple_parts else {}
            if len(simple_parts) > 1 and extra_map:
                first_key = list(extra_map.keys())[0]
                params[first_key] = simple_parts[1]
            if duration_override:
                params["duration"] = duration_override
            return {
                "id": f"action-{index}",
                "type": type_name,
                "backend": "chaosmesh",
                "params": params,
                "start": "immediate",
            }

    # 标准格式: type:key=val,key=val
    params = {}
    if len(parts) > 1:
        for kv in parts[1].split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()

    if duration_override:
        params["duration"] = duration_override
    if "duration" not in params:
        params["duration"] = "2m"

    # 自动推断 backend
    backend = "fis" if type_name.startswith("fis_") else "chaosmesh"

    return {
        "id": f"action-{index}",
        "type": type_name,
        "backend": backend,
        "params": params,
        "start": "immediate",
    }


def cmd_dr_verify(args):
    """DR Plan 逐步验证"""
    import json as _json

    # Add project root so dr-plan-generator modules are importable
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from dr_step_runner import DRStepRunner

    # Load plan — support both dr-plan-generator and raw JSON
    dr_gen_root = os.path.join(project_root, "dr-plan-generator")
    if dr_gen_root not in sys.path:
        sys.path.insert(0, dr_gen_root)
    from models import DRPlan

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(_json.load(fh))

    runner = DRStepRunner(
        dry_run=getattr(args, "dry_run", False),
        cooldown_seconds=args.cooldown,
    )

    if args.level == "dry-run":
        from validation.plan_verifier import DRPlanVerifier
        verifier = DRPlanVerifier(plan=plan)
        report = verifier.dry_run()
        print(_json.dumps(
            {"ready": report.ready, "pass": report.pass_count,
             "fail": report.fail_count, "warn": report.warn_count,
             "blockers": report.blockers},
            indent=2, ensure_ascii=False,
        ))
    else:
        from dr_orchestrator import DROrchestrator
        orch = DROrchestrator(
            step_runner=runner,
            plan=plan,
            stop_on_failure=args.stop_on_failure,
        )
        # Run phase by phase
        for phase in plan.phases:
            print(f"\n{'='*60}")
            print(f"Phase: {phase.phase_id} — {phase.name}")
            print(f"{'='*60}")
            result = orch.run_phase(phase)
            status = "✅ PASS" if result.all_steps_passed else "❌ FAIL"
            print(f"  Result: {status} ({len(result.steps)} steps, "
                  f"{result.total_duration_seconds:.1f}s)")
            if result.failed_steps:
                print(f"  Failed: {result.failed_steps}")
                if phase.phase_id == "phase-readiness":
                    print("  ⛔ HARD BLOCK: Readiness gate failed — stopping before traffic cutover")
                    break
                if args.stop_on_failure:
                    break


def cmd_dr_rehearsal(args):
    """DR Plan 全量演练"""
    import json as _json

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    dr_gen_root = os.path.join(project_root, "dr-plan-generator")
    if dr_gen_root not in sys.path:
        sys.path.insert(0, dr_gen_root)

    from models import DRPlan
    from runner.dr_step_runner import DRStepRunner
    from dr_orchestrator import DROrchestrator

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(_json.load(fh))

    runner = DRStepRunner(cooldown_seconds=args.cooldown)
    orch = DROrchestrator(
        step_runner=runner,
        plan=plan,
        require_approval=args.require_approval,
        stop_on_failure=args.stop_on_failure,
        environment=args.environment,
    )

    print(f"\n{'='*60}")
    print(f"DR Plan Full Rehearsal: {plan.plan_id}")
    print(f"Scope: {plan.scope} | Environment: {args.environment}")
    print(f"{'='*60}\n")

    report = orch.run_rehearsal()

    # Summary
    print(f"\n{'='*60}")
    print(f"Rehearsal {'PASSED ✅' if report.success else 'FAILED ❌'}")
    print(f"  Duration: {report.total_duration_seconds:.0f}s "
          f"(actual RTO: {report.actual_rto_minutes:.1f} min, "
          f"estimated: {report.estimated_rto_minutes} min)")
    if report.failed_steps:
        print(f"  Failed steps: {report.failed_steps}")
    if report.warnings:
        for w in report.warnings:
            print(f"  ⚠️  {w}")
    print(f"{'='*60}")

    # Save report
    report_path = orch.save_report(report)
    print(f"\nReport saved to: {report_path}")


def main():
    parser = argparse.ArgumentParser(description="chaos-automation runner")
    sub = parser.add_subparsers(dest="cmd")

    # setup
    sub.add_parser("setup", help="初始化 DynamoDB 表")

    # run
    run_p = sub.add_parser("run", help="执行实验")
    run_p.add_argument("--file", required=True, help="实验 YAML 路径")
    run_p.add_argument("--dry-run", action="store_true", help="不实际注入，走完流程框架")
    run_p.add_argument("--duration", default=None, help="覆盖 YAML 中所有 action 的 duration（如 30s, 2m）")
    run_p.add_argument("--tags", default=None, help="资源 tag 过滤 (格式: key=value,key2=value2)")

    # history
    hist_p = sub.add_parser("history", help="查询历史记录")
    hist_p.add_argument("--service", required=True, help="目标服务名")
    hist_p.add_argument("--limit", type=int, default=10)

    # suite
    suite_p = sub.add_parser("suite", help="批量编排实验")
    suite_p.add_argument("--dir", required=True, help="实验 YAML 目录")
    suite_p.add_argument("--strategy", default="by_tier",
                         choices=["by_tier", "by_priority", "by_domain", "full_suite"])
    suite_p.add_argument("--top", type=int, default=None, help="只执行前 N 个")
    suite_p.add_argument("--max-parallel", type=int, default=1, help="并行数")
    suite_p.add_argument("--stop-on-failure", action="store_true", default=True)
    suite_p.add_argument("--no-stop-on-failure", dest="stop_on_failure", action="store_false")
    suite_p.add_argument("--cooldown", type=int, default=60, help="实验间冷却秒数")
    suite_p.add_argument("--dry-run", action="store_true")
    suite_p.add_argument("--tags", default=None, help="资源 tag 过滤 (格式: key=value,key2=value2)")

    # auto
    auto_p = sub.add_parser("auto", help="端到端自动化（假设生成 → 执行）")
    auto_p.add_argument("--max-hypotheses", type=int, default=20)
    auto_p.add_argument("--top", type=int, default=5)
    auto_p.add_argument("--max-parallel", type=int, default=1)
    auto_p.add_argument("--stop-on-failure", action="store_true", default=True)
    auto_p.add_argument("--no-stop-on-failure", dest="stop_on_failure", action="store_false")
    auto_p.add_argument("--cooldown", type=int, default=60)
    auto_p.add_argument("--dry-run", action="store_true")
    auto_p.add_argument("--prepare-only", action="store_true",
                        help="只生成假设和实验 YAML + dry-run 验证，不实际执行")
    auto_p.add_argument("--tags", default=None, help="资源 tag 过滤 (格式: key=value,key2=value2)")

    # hypothesis
    hyp_p = sub.add_parser("hypothesis", help="AI 假设生成与管理")

    # learn
    learn_p = sub.add_parser("learn", help="闭环学习分析")
    learn_p.add_argument("--service", default=None, help="目标服务名")
    learn_p.add_argument("--all", action="store_true", help="分析所有服务")
    learn_p.add_argument("--limit", type=int, default=50, help="每服务最大记录数")
    learn_p.add_argument("--output", default="learning_report.md", help="报告输出路径")
    learn_p.add_argument("--no-graph-update", action="store_true", help="不更新 Neptune 图谱")
    hyp_sub = hyp_p.add_subparsers(dest="hypothesis_action")

    gen_p = hyp_sub.add_parser("generate", help="生成假设")
    gen_p.add_argument("--max", type=int, default=50, help="生成数量上限")
    gen_p.add_argument("--service", default=None, help="限定目标服务")

    hyp_sub.add_parser("list", help="列出现有假设")

    exp_p = hyp_sub.add_parser("export", help="导出为实验 YAML")
    exp_p.add_argument("--top", type=int, default=5, help="导出优先级最高的 N 个")
    exp_p.add_argument("--output-dir", default="experiments/generated/", help="输出目录")

    # dr-verify
    drv_p = sub.add_parser("dr-verify", help="DR Plan 逐步验证")

    # compose
    compose_p = sub.add_parser("compose", help="CLI 快速组合实验")
    compose_p.add_argument("--target", default=None, help="目标服务名")
    compose_p.add_argument("--namespace", default=None, help="K8s namespace")
    compose_p.add_argument("--fault", action="append", help="故障定义 (格式: type:key=val,key=val 或短名)")
    compose_p.add_argument("--duration", default=None, help="全局 duration 覆盖")
    compose_p.add_argument("--max-duration", default=None, help="max_duration 安全上限")
    compose_p.add_argument("--schedule", default="parallel", choices=["parallel", "sequential"],
                           help="编排模式: parallel（并行）/ sequential（串行）")
    compose_p.add_argument("--save", default=None, help="保存 YAML 到指定路径")
    compose_p.add_argument("--run", action="store_true", help="生成后立即执行")
    compose_p.add_argument("--dry-run", action="store_true", help="生成后 dry-run 验证")
    drv_p.add_argument("--plan", required=True, help="DR Plan JSON 路径")
    drv_p.add_argument("--level", default="step", choices=["dry-run", "step"],
                       help="验证级别 (dry-run | step)")
    drv_p.add_argument("--strategy", default="checkpoint",
                       choices=["isolated", "cumulative", "checkpoint"])
    drv_p.add_argument("--dry-run", action="store_true", help="DRStepRunner dry-run 模式")
    drv_p.add_argument("--cooldown", type=int, default=10, help="步骤间冷却秒数")
    drv_p.add_argument("--stop-on-failure", action="store_true", default=True)

    # dr-rehearsal
    drr_p = sub.add_parser("dr-rehearsal", help="DR Plan 全量演练")
    drr_p.add_argument("--plan", required=True, help="DR Plan JSON 路径")
    drr_p.add_argument("--cooldown", type=int, default=30, help="步骤间冷却秒数")
    drr_p.add_argument("--stop-on-failure", action="store_true", default=True)
    drr_p.add_argument("--no-stop-on-failure", dest="stop_on_failure", action="store_false")
    drr_p.add_argument("--environment", default="staging", choices=["staging", "production"])
    drr_p.add_argument("--require-approval", action="store_true", default=False)

    args = parser.parse_args()
    if args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "history":
        cmd_history(args)
    elif args.cmd == "suite":
        cmd_suite(args)
    elif args.cmd == "auto":
        cmd_auto(args)
    elif args.cmd == "learn":
        cmd_learn(args)
    elif args.cmd == "hypothesis":
        cmd_hypothesis(args)
    elif args.cmd == "dr-verify":
        cmd_dr_verify(args)
    elif args.cmd == "dr-rehearsal":
        cmd_dr_rehearsal(args)
    elif args.cmd == "compose":
        cmd_compose(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

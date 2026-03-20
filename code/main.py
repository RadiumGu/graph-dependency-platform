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
import sys
import os

# 确保 code/ 在 path 上
sys.path.insert(0, os.path.dirname(__file__))

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


def cmd_run(args):
    """执行实验"""
    from runner import load_experiment, ExperimentRunner

    if not os.path.exists(args.file):
        print(f"❌ 文件不存在: {args.file}")
        sys.exit(1)

    exp    = load_experiment(args.file)
    runner = ExperimentRunner(dry_run=args.dry_run)

    logger.info(f"实验: {exp.name}")
    logger.info(f"目标: {exp.target_service} ({exp.target_tier})")
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

    ddb = boto3.client("dynamodb", region_name="ap-northeast-1")
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


def main():
    parser = argparse.ArgumentParser(description="chaos-automation runner")
    sub = parser.add_subparsers(dest="cmd")

    # setup
    sub.add_parser("setup", help="初始化 DynamoDB 表")

    # run
    run_p = sub.add_parser("run", help="执行实验")
    run_p.add_argument("--file", required=True, help="实验 YAML 路径")
    run_p.add_argument("--dry-run", action="store_true", help="不实际注入，走完流程框架")

    # history
    hist_p = sub.add_parser("history", help="查询历史记录")
    hist_p.add_argument("--service", required=True, help="目标服务名")
    hist_p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "history":
        cmd_history(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

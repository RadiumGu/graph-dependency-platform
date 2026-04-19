#!/usr/bin/env python3
"""
main.py — DR Plan Generator CLI entry point

Commands:
  plan         Generate a DR switchover plan
  assess       Run impact assessment
  validate     Validate an existing plan JSON
  rollback     Generate rollback plan from existing plan JSON
  export-chaos Export plan assumptions as chaos experiment YAMLs
"""

import argparse
import dataclasses
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> None:
    """Generate a DR switchover plan and write it to the output directory.

    Args:
        args: Parsed CLI arguments.
    """
    from graph.graph_analyzer import GraphAnalyzer
    from output.json_renderer import JSONRenderer
    from output.markdown_renderer import MarkdownRenderer
    from planner.plan_generator import PlanGenerator
    from planner.rollback_generator import RollbackGenerator
    from planner.step_builder import StepBuilder
    from registry.registry_loader import get_registry
    from validation.plan_validator import PlanValidator

    registry = get_registry(custom_path=getattr(args, "custom_registry", None))
    analyzer = GraphAnalyzer(registry=registry)
    builder = StepBuilder()
    generator = PlanGenerator(analyzer, builder)

    plan = generator.generate_plan(
        scope=args.scope,
        source=args.source,
        target=args.target,
        exclude=args.exclude.split(",") if args.exclude else None,
    )

    # Attach rollback phases automatically
    plan.rollback_phases = RollbackGenerator().generate_rollback(plan)

    # Static validation
    report = PlanValidator().validate(plan)
    if report.issues:
        print(
            f"[WARN] Validation found {len(report.issues)} issue(s):",
            file=sys.stderr,
        )
        for issue in report.issues:
            print(f"  [{issue.severity}] {issue.message}", file=sys.stderr)

    _output(plan, args)

    total_steps = sum(len(p.steps) for p in plan.phases)
    print(f"\nPlan generated: {plan.plan_id}", file=sys.stderr)
    print(f"  Affected services: {len(plan.affected_services)}", file=sys.stderr)
    print(f"  Switchover steps:  {total_steps}", file=sys.stderr)
    print(f"  Estimated RTO:     {plan.estimated_rto} min", file=sys.stderr)


def cmd_assess(args: argparse.Namespace) -> None:
    """Run impact assessment for a failure scenario.

    Args:
        args: Parsed CLI arguments.
    """
    from assessment.impact_analyzer import ImpactAnalyzer
    from graph.graph_analyzer import GraphAnalyzer
    from output.markdown_renderer import MarkdownRenderer
    from registry.registry_loader import get_registry

    registry = get_registry(custom_path=getattr(args, "custom_registry", None))
    analyzer = GraphAnalyzer(registry=registry)
    subgraph = analyzer.extract_affected_subgraph(args.scope, args.failure)
    impact = ImpactAnalyzer().assess_impact(subgraph, args.scope, args.failure)

    if args.format == "json":
        print(json.dumps(dataclasses.asdict(impact), indent=2, ensure_ascii=False))
    else:
        print(MarkdownRenderer().render_impact(impact))


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate an existing DR plan JSON file.

    Args:
        args: Parsed CLI arguments.
    """
    from models import DRPlan
    from validation.plan_validator import PlanValidator

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(json.load(fh))

    report = PlanValidator().validate(plan)
    status = "PASS" if report.valid else "FAIL"
    print(f"Validation result: {status}")
    for issue in report.issues:
        print(f"  [{issue.severity}] {issue.message}")

    sys.exit(0 if report.valid else 1)


def cmd_rollback(args: argparse.Namespace) -> None:
    """Generate and output a rollback plan from an existing plan file.

    Args:
        args: Parsed CLI arguments.
    """
    from models import DRPlan
    from planner.rollback_generator import RollbackGenerator

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(json.load(fh))

    plan.rollback_phases = RollbackGenerator().generate_rollback(plan)
    _output(plan, args, rollback_only=True)


def cmd_export_chaos(args: argparse.Namespace) -> None:
    """Export DR plan key assumptions as chaos experiment YAML files.

    Args:
        args: Parsed CLI arguments.
    """
    from models import DRPlan
    from validation.chaos_exporter import ChaosExporter

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(json.load(fh))

    os.makedirs(args.output, exist_ok=True)
    experiments = ChaosExporter().export(plan, args.output)
    print(f"Exported {len(experiments)} chaos experiment(s) to {args.output}")


def cmd_execute(args: argparse.Namespace) -> None:
    """Execute a DR plan using the configured engine."""
    import json as _json
    from models import DRPlan
    from executor_factory import make_dr_executor

    with open(args.plan) as f:
        plan_data = _json.load(f)
    plan = DRPlan.from_dict(plan_data)

    dry_run = not getattr(args, 'no_dry_run', False)
    executor = make_dr_executor(dry_run=dry_run)

    print(f"Executing DR plan {plan.plan_id} (dry_run={executor.dry_run}, engine={executor.ENGINE_NAME})")
    report = executor.execute(plan)

    print(f"\nResult: environment={report.environment}")
    print(f"Duration: {report.total_duration_seconds:.1f}s")
    print(f"RTO: {report.actual_rto_minutes}min (estimated {report.estimated_rto_minutes}min)")
    if report.warnings:
        print(f"Warnings: {report.warnings}")
    if report.failed_steps:
        print(f"Failed steps: {report.failed_steps}")
    """Export DR plan key assumptions as chaos experiment YAML files.

    Args:
        args: Parsed CLI arguments.
    """
    from models import DRPlan
    from validation.chaos_exporter import ChaosExporter

    with open(args.plan, encoding="utf-8") as fh:
        plan = DRPlan.from_dict(json.load(fh))

    os.makedirs(args.output, exist_ok=True)
    experiments = ChaosExporter().export(plan, args.output)
    print(f"Exported {len(experiments)} chaos experiment(s) to {args.output}")


# ---------------------------------------------------------------------------
# Shared output helper
# ---------------------------------------------------------------------------

def _output(
    plan: "DRPlan",  # type: ignore[name-defined]  # noqa: F821
    args: argparse.Namespace,
    rollback_only: bool = False,
) -> None:
    """Write plan output to a file and print to stdout.

    Args:
        plan: The DRPlan to write.
        args: CLI args (used for format and output_dir).
        rollback_only: If True, output only the rollback phases as JSON.
    """
    from output.json_renderer import JSONRenderer
    from output.markdown_renderer import MarkdownRenderer

    fmt = getattr(args, "format", "markdown")
    out_dir = getattr(args, "output_dir", "plans")
    os.makedirs(out_dir, exist_ok=True)

    if fmt == "json":
        content = JSONRenderer().render(plan)
        path = os.path.join(out_dir, f"{plan.plan_id}.json")
    else:
        content = MarkdownRenderer().render(plan)
        path = os.path.join(out_dir, f"{plan.plan_id}.md")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)

    print(content)
    print(f"\n[Output saved to {path}]", file=sys.stderr)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="DR Plan Generator — graph-driven disaster recovery plan generation"
    )
    sub = parser.add_subparsers(dest="command")

    # plan
    p = sub.add_parser("plan", help="Generate DR switchover plan")
    p.add_argument(
        "--scope", required=True, choices=["region", "az", "service"],
        help="Failure scope",
    )
    p.add_argument("--source", required=True, help="Failure source (region/az/service name)")
    p.add_argument("--target", required=True, help="DR target (region/az)")
    p.add_argument("--exclude", help="Comma-separated services to exclude")
    p.add_argument("--format", default="markdown", choices=["markdown", "json"])
    p.add_argument("--output-dir", default="plans", dest="output_dir")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument(
        "--custom-registry",
        default=None,
        dest="custom_registry",
        metavar="PATH",
        help="Path to custom_types.yaml (overrides bundled service_types.yaml entries)",
    )

    # assess
    a = sub.add_parser("assess", help="Impact assessment for a failure scenario")
    a.add_argument(
        "--scope", required=True, choices=["region", "az", "service"],
        help="Failure scope",
    )
    a.add_argument("--failure", required=True, help="Failure source identifier")
    a.add_argument("--format", default="markdown", choices=["markdown", "json"])
    a.add_argument(
        "--custom-registry",
        default=None,
        dest="custom_registry",
        metavar="PATH",
        help="Path to custom_types.yaml (overrides bundled service_types.yaml entries)",
    )

    # validate
    v = sub.add_parser("validate", help="Validate an existing plan JSON")
    v.add_argument("--plan", required=True, help="Path to plan JSON file")

    # rollback
    r = sub.add_parser("rollback", help="Generate rollback plan from existing plan JSON")
    r.add_argument("--plan", required=True, help="Path to plan JSON file")
    r.add_argument("--format", default="markdown", choices=["markdown", "json"])
    r.add_argument("--output-dir", default="plans", dest="output_dir")

    # export-chaos
    e = sub.add_parser("export-chaos", help="Export plan as chaos validation experiments")
    e.add_argument("--plan", required=True, help="Path to plan JSON file")
    e.add_argument("--output", required=True, help="Output directory for experiment YAMLs")

    x = sub.add_parser("execute", help="Execute a DR plan (dry-run by default)")
    x.add_argument("--plan", required=True, help="Path to plan JSON file")
    x.add_argument("--dry-run", action="store_true", default=True, help="Dry-run mode (default)")
    x.add_argument("--no-dry-run", action="store_true", help="Disable dry-run (real execution)")

    return parser


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command handler."""
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "plan": cmd_plan,
        "assess": cmd_assess,
        "validate": cmd_validate,
        "rollback": cmd_rollback,
        "export-chaos": cmd_export_chaos,
        "execute": cmd_execute,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()

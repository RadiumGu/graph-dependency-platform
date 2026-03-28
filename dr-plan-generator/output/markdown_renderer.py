"""
output/markdown_renderer.py — Render DR plans as human-readable Markdown

Produces a structured Markdown document suitable for review, approval,
and execution during a DR event.
"""

import logging
from typing import List

from models import DRPhase, DRPlan, ImpactReport

logger = logging.getLogger(__name__)


class MarkdownRenderer:
    """Render DRPlan and ImpactReport objects as Markdown strings."""

    def render(self, plan: DRPlan) -> str:
        """Render a complete DR plan as Markdown.

        Args:
            plan: The DRPlan to render.

        Returns:
            Markdown string.
        """
        lines: List[str] = []

        # Header
        lines += [
            f"# DR Switchover Plan — {plan.scope.upper()} Level",
            "",
            f"> Generated: {plan.created_at}",
            f"> Failure scope: {plan.source} → DR target: {plan.target}",
            f"> Estimated RTO: {plan.estimated_rto} minutes",
            f"> Estimated RPO: {plan.estimated_rpo} minutes",
            f"> Graph snapshot: {plan.graph_snapshot_time}",
            f"> Plan ID: `{plan.plan_id}`",
            "",
        ]

        # Impact summary table
        lines += self._render_impact_summary(plan)

        # SPOF warnings
        if plan.impact_assessment and plan.impact_assessment.single_points_of_failure:
            lines += self._render_spof_warnings(plan.impact_assessment)

        # Phases
        for phase in plan.phases:
            lines += self._render_phase(phase)

        # Rollback phases (if populated)
        if plan.rollback_phases:
            lines += [
                "---",
                "",
                "# Rollback Plan",
                "",
            ]
            for phase in plan.rollback_phases:
                lines += self._render_phase(phase)

        return "\n".join(lines)

    def render_impact(self, report: ImpactReport) -> str:
        """Render an ImpactReport as Markdown.

        Args:
            report: The ImpactReport to render.

        Returns:
            Markdown string.
        """
        lines: List[str] = [
            f"# Impact Assessment — {report.scope.upper()} Failure: {report.source}",
            "",
            "## Summary",
            "",
            f"| Dimension | Value |",
            f"|-----------|-------|",
            f"| Total affected resources | {report.total_affected} |",
            f"| Tier0 services | {len(report.by_tier.get('Tier0', []))} |",
            f"| Tier1 services | {len(report.by_tier.get('Tier1', []))} |",
            f"| Tier2 services | {len(report.by_tier.get('Tier2', []))} |",
            f"| Estimated RTO | {report.estimated_rto_minutes} min |",
            f"| Estimated RPO | {report.estimated_rpo_minutes} min |",
            "",
        ]

        if report.single_points_of_failure:
            lines += self._render_spof_warnings(report)

        if report.risk_matrix:
            lines += [
                "## Risk Matrix",
                "",
                f"**Severity**: {report.risk_matrix.get('severity', 'UNKNOWN')}",
                "",
                f"- Tier0 services affected: "
                f"{report.risk_matrix.get('tier0_services_affected', 0)}",
                f"- Single points of failure: "
                f"{report.risk_matrix.get('single_points_of_failure', 0)}",
            ]
            key_risks = report.risk_matrix.get("key_risks", [])
            if key_risks:
                lines.append(f"- Key risks: {', '.join(key_risks)}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _render_impact_summary(self, plan: DRPlan) -> List[str]:
        """Render the impact summary section.

        Shows a concise but informative overview: scope, scale, tier
        breakdown, service lists, risk level, and key timing estimates.

        Args:
            plan: DRPlan.

        Returns:
            List of Markdown lines.
        """
        impact = plan.impact_assessment
        tier0 = impact.by_tier.get("Tier0", []) if impact else []
        tier1 = impact.by_tier.get("Tier1", []) if impact else []
        tier2 = impact.by_tier.get("Tier2", []) if impact else []
        risk = impact.risk_matrix if impact else {}
        severity = risk.get("severity", "UNKNOWN")

        # Severity emoji
        sev_map = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
        sev_icon = sev_map.get(severity, "⚪")

        total_steps = sum(len(p.steps) for p in plan.phases)
        rollback_steps = sum(len(p.steps) for p in plan.rollback_phases)

        lines = [
            "## Impact Summary",
            "",
            f"**Risk level**: {sev_icon} {severity}  ",
            f"**Scope**: {plan.scope.upper()} — `{plan.source}` → `{plan.target}`  ",
            f"**Estimated RTO**: {plan.estimated_rto} min | **RPO**: {plan.estimated_rpo} min",
            "",
            "| Dimension | Value |",
            "|-----------|-------|",
            f"| Affected services | {len(plan.affected_services)} |",
            f"| Affected resources | {len(plan.affected_resources)} |",
            f"| Tier0 (critical) | {len(tier0)} |",
            f"| Tier1 (important) | {len(tier1)} |",
            f"| Tier2 (standard) | {len(tier2)} |",
            f"| Switchover steps | {total_steps} |",
            f"| Rollback steps | {rollback_steps} |",
            "",
        ]

        # Tier0 services (always list — these are critical)
        if tier0:
            names = sorted(set(n.get("name", "?") for n in tier0))
            lines += [
                "### Tier0 Critical Services",
                "",
                ", ".join(f"`{n}`" for n in names),
                "",
            ]

        # Tier1 services (list if any)
        if tier1:
            names = sorted(set(n.get("name", "?") for n in tier1))
            lines += [
                "### Tier1 Important Services",
                "",
                ", ".join(f"`{n}`" for n in names),
                "",
            ]

        # Affected by layer (compact view)
        layer_counts: dict = {}
        if impact:
            for node in (
                impact.by_tier.get("Tier0", [])
                + impact.by_tier.get("Tier1", [])
                + impact.by_tier.get("Tier2", [])
                + impact.by_tier.get("Unknown", [])
            ):
                rtype = node.get("type", "Unknown")
                layer_counts[rtype] = layer_counts.get(rtype, 0) + 1

        if layer_counts:
            lines += [
                "### Affected Resource Types",
                "",
                "| Type | Count | Fault Domain |",
                "|------|-------|-------------|",
            ]
            from registry import registry_loader
            reg = registry_loader.get_registry()
            for rtype, count in sorted(layer_counts.items(), key=lambda x: -x[1]):
                fd = reg.get_fault_domain(rtype)
                fd_icon = {"zonal": "⚡ zonal", "regional": "🌐 regional", "global": "🌍 global"}.get(fd, fd)
                lines.append(f"| {rtype} | {count} | {fd_icon} |")
            lines.append("")

        # Skipped regional services note (AZ scope only)
        if plan.scope == "az" and layer_counts:
            regional_types = [
                rtype for rtype, _ in layer_counts.items()
                if registry_loader.get_registry().get_fault_domain(rtype) in ("regional", "global")
            ]
            if regional_types:
                lines += [
                    f"> ℹ️ **{len(regional_types)} regional/global resource type(s)** in the affected subgraph "
                    f"are unaffected by AZ failure and have no switchover steps: "
                    f"{', '.join(f'`{t}`' for t in sorted(regional_types))}",
                    "",
                ]

        return lines

    def _render_spof_warnings(self, report: ImpactReport) -> List[str]:
        """Render SPOF warnings section.

        Args:
            report: ImpactReport.

        Returns:
            List of Markdown lines.
        """
        lines = ["### Single Point of Failure Risks", ""]
        for spof in report.single_points_of_failure:
            impact_count = len(spof.get("impact", []))
            lines.append(
                f"- **{spof['resource']}** ({spof['type']}) — "
                f"Only in `{spof.get('az', 'unknown')}`, "
                f"affects {impact_count} service(s)"
            )
        lines.append("")
        return lines

    def _render_phase(self, phase: DRPhase) -> List[str]:
        """Render a single DRPhase as Markdown.

        Args:
            phase: DRPhase to render.

        Returns:
            List of Markdown lines.
        """
        lines = [
            f"## {phase.phase_id}: {phase.name}",
            "",
            f"**Estimated duration**: {phase.estimated_duration} min",
            f"**Gate condition**: {phase.gate_condition}",
            "",
        ]

        for i, step in enumerate(phase.steps, 1):
            approval_tag = " (requires approval)" if step.requires_approval else ""
            parallel_tag = (
                f" [parallel group: {step.parallel_group}]"
                if step.parallel_group
                else ""
            )
            tier_tag = f" [{step.tier}]" if step.tier else ""

            lines += [
                f"### Step {phase.phase_id}.{i}: "
                f"`{step.action}` — {step.resource_name}"
                f"{approval_tag}{parallel_tag}{tier_tag}",
                "",
                f"**Resource type**: {step.resource_type}",
                f"**Estimated time**: {step.estimated_time}s",
            ]
            if step.dependencies:
                lines.append(f"**Depends on**: {', '.join(step.dependencies)}")

            lines += [
                "",
                "**Command**:",
                "```bash",
                step.command,
                "```",
                "",
                "**Validation**:",
                "```bash",
                step.validation,
                "```",
                f"Expected result: `{step.expected_result}`",
                "",
                "**Rollback**:",
                "```bash",
                step.rollback_command,
                "```",
                "",
            ]

        return lines

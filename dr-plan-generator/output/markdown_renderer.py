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
            f"> Strategy: {plan.strategy or '(unset)'} | Mode: {plan.mode}",
            f"> Estimated RTO: {plan.estimated_rto} minutes"
            + (
                f" (confidence: {plan.rto_basis.get('confidence')})"
                if plan.rto_basis.get("confidence") else ""
            ),
            f"> Estimated RPO: {self._format_rpo(plan)}",
            f"> Graph snapshot: {plan.graph_snapshot_time}"
            + (
                f" (captured from {plan.plan_source}, "
                f"age {plan.graph_snapshot_age_seconds / 3600.0:.1f}h"
                f"{', STALE' if plan.graph_snapshot_stale else ''})"
                if plan.graph_snapshot_age_seconds is not None
                else f" (source: {plan.plan_source})"
            ),
            f"> Plan ID: `{plan.plan_id}`",
            "",
        ]

        # Data-layer feasibility comes FIRST, above the impact summary: if the
        # declared strategy's preconditions are not met, every RTO/RPO figure
        # below is optimistic and the reader must know that before reading them.
        if plan.data_layer_gaps or plan.compute_layer_gaps:
            lines += self._render_data_layer_gaps(plan)

        # Impact summary table
        lines += self._render_impact_summary(plan)

        # RPO 推导依据 —— 审计问「数字怎么来的」时的答案。
        if plan.rpo_basis:
            lines += self._render_rpo_basis(plan)

        # What was deliberately left out of scope.
        if plan.scope_exclusions:
            lines += self._render_scope_exclusions(plan)

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

    def _render_data_layer_gaps(self, plan: DRPlan) -> List[str]:
        """渲染「所声明策略未满足的前提」（数据层 + 计算层）。

        刻意放在 RTO/RPO 摘要**之前**：前提不成立时，下方所有指标都是乐观值，
        读者必须先知道这一点。审计场景下这一段就是「已知缺口」的书面留痕。

        Args:
            plan: 含 ``data_layer_gaps`` / ``compute_layer_gaps`` 的 DRPlan。

        Returns:
            Markdown 行列表。
        """
        lines: List[str] = []

        if plan.data_layer_gaps:
            lines += [
                "## ⚠️ 数据层前提未满足",
                "",
                f"所声明的策略 **{plan.strategy or '(unset)'}** 要求数据已复制到恢复区"
                "（AWS Well-Architected REL13-BP02）。以下组件不满足该前提，"
                "**下方 RTO/RPO 均为乐观估计**：",
                "",
                "| 组件 | 当前状态 | 该策略要求 | 影响 |",
                "|------|----------|-----------|------|",
            ]
            for gap in plan.data_layer_gaps:
                lines.append(
                    f"| `{gap.get('component', '')}` | {gap.get('actual', '')} "
                    f"| {gap.get('requirement', '')} | {gap.get('implication', '')} |"
                )
            lines += [
                "",
                "> 在补齐跨区复制之前，本计划的**实际**恢复能力接近 "
                "backup & restore（RPO 数小时），而非所声明的档位。",
                "",
            ]

        if plan.compute_layer_gaps:
            lines += [
                "## ⚠️ 计算层前提未满足",
                "",
                "**本计划在执行前必须先补齐以下配置**，否则会出现「命令成功但服务未起」：",
                "",
                "| 组件 | 当前状态 | 需要 | 影响 |",
                "|------|----------|------|------|",
            ]
            for gap in plan.compute_layer_gaps:
                lines.append(
                    f"| `{gap.get('component', '')}` | {gap.get('actual', '')} "
                    f"| {gap.get('requirement', '')} | {gap.get('implication', '')} |"
                )
            lines.append("")

        return lines

    def _render_scope_exclusions(self, plan: DRPlan) -> List[str]:
        """渲染排除清单。

        审计要能看出某个资源是**有意排除**而不是漏了，所以每条都带 reason 与
        命中的规则。可达性排除数量通常很多，只详列显式规则命中的，其余汇总。

        Args:
            plan: 含 ``scope_exclusions`` 的 DRPlan。

        Returns:
            Markdown 行列表。
        """
        explicit = [e for e in plan.scope_exclusions if e.get("rule") != "reachability"]
        implicit = [e for e in plan.scope_exclusions if e.get("rule") == "reachability"]

        lines = ["## 切换范围排除项", ""]
        if explicit:
            lines += [
                "**按显式规则排除**（有意排除，非遗漏）：",
                "",
                "| 资源 | 类型 | 命中规则 | 原因 |",
                "|------|------|---------|------|",
            ]
            for e in explicit:
                lines.append(
                    f"| `{e.get('name','')}` | {e.get('type','')} "
                    f"| `{e.get('rule','')}` | {e.get('reason','')} |"
                )
            lines.append("")
        if implicit:
            names = ", ".join(f"`{e.get('name','')}`" for e in implicit[:20])
            more = f" 等 {len(implicit)} 项" if len(implicit) > 20 else ""
            lines += [
                f"**从本 workload 锚点不可达**（{len(implicit)} 项）：{names}{more}",
                "",
            ]
        return lines

    @staticmethod
    def _format_rpo(plan: DRPlan) -> str:
        """格式化 RPO。

        ``None`` 表示无法从配置推定，此时**必须显示这一点**而不是印一个 0
        ——0 会被读成「零数据丢失」，与「说不清」是完全相反的结论。
        """
        if plan.estimated_rpo is None:
            unmeasurable = "、".join(plan.rpo_unmeasurable) or "部分组件"
            return f"⚠️ 不可从配置推定（{unmeasurable}），需实测或按备份间隔论证"
        return f"{plan.estimated_rpo} minutes"

    def _render_rpo_basis(self, plan: DRPlan) -> List[str]:
        """渲染 RPO 的逐组件推导依据。

        这一段就是审计问「这个数字怎么来的」时的答案。原实现给不出答案——
        它是一张硬编码表（RDS=5 / DynamoDB=0 / 其他=15）。

        Args:
            plan: 含 ``rpo_basis`` 的 DRPlan。

        Returns:
            Markdown 行列表。
        """
        lines = [
            "## RPO 推导依据",
            "",
            "| 组件 | 复制拓扑 | RPO | 依据 |",
            "|------|---------|-----|------|",
        ]
        for item in plan.rpo_basis:
            lines.append(
                f"| `{item.get('component', '')}` | {item.get('topology', '')} "
                f"| {item.get('rpo', '')} | {item.get('reason', '')} |"
            )
        lines.append("")

        if plan.rpo_measurement_commands:
            lines += [
                "**取得可举证数值所需的实测命令**（演练时执行并回填报告）：",
                "",
            ]
            for cmd in plan.rpo_measurement_commands:
                lines += [
                    f"- `{cmd.get('component', '')}` — {cmd.get('purpose', '')}",
                    "",
                    "  ```bash",
                    f"  {cmd.get('command', '')}",
                    "  ```",
                    "",
                ]

        if plan.rto_basis.get("confidence") == "design_values_only":
            lines += [
                "> ⚠️ **RTO 全部来自设计值查表，尚无演练实测数据。** 监管场景要的是"
                "实测 RTO；跑过演练并回写 `plans/measurements.json` 后，"
                "估算会自动改用实测滚动均值。",
                "",
            ]
        return lines

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

"""
output/summary_generator.py — LLM-powered DR plan executive summary

Calls AWS Bedrock (Claude) to generate a concise management-readable
summary of the DR plan. Gracefully falls back to a static template
if Bedrock is unavailable.
"""

import json
import logging
from typing import Optional

from models import DRPlan

logger = logging.getLogger(__name__)


class SummaryGenerator:
    """Generate executive summaries for DR plans using Bedrock Claude.

    Falls back gracefully to a static summary if:
    - ``BEDROCK_MODEL`` is not configured.
    - Bedrock API call fails for any reason.
    """

    def generate(self, plan: DRPlan) -> str:
        """Generate an executive summary for a DR plan.

        Args:
            plan: The DRPlan to summarise.

        Returns:
            Summary string (LLM-generated or static fallback).
        """
        try:
            return self._call_bedrock(plan)
        except Exception as exc:
            logger.warning(
                "Bedrock summary generation failed (%s), using static fallback.", exc
            )
            return self._static_summary(plan)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_bedrock(self, plan: DRPlan) -> str:
        """Invoke Bedrock Claude to generate the summary.

        Args:
            plan: DRPlan for context.

        Returns:
            LLM-generated summary string.

        Raises:
            Exception: If Bedrock call fails.
        """
        import boto3

        from config import BEDROCK_MODEL, REGION

        tier0_count = 0
        spof_json = "[]"
        if plan.impact_assessment:
            tier0_count = len(plan.impact_assessment.by_tier.get("Tier0", []))
            spof_json = json.dumps(
                plan.impact_assessment.single_points_of_failure, ensure_ascii=False
            )

        prompt = (
            f"You are an AWS SRE expert. Based on the following DR switchover plan, "
            f"generate a concise executive summary.\n\n"
            f"Switchover scope: {plan.scope} level, from {plan.source} to {plan.target}\n"
            f"Affected services: {len(plan.affected_services)} "
            f"(Tier0: {tier0_count})\n"
            f"Switchover steps: {sum(len(p.steps) for p in plan.phases)}\n"
            f"Estimated RTO: {plan.estimated_rto} minutes\n"
            f"Estimated RPO: {plan.estimated_rpo} minutes\n\n"
            f"Single points of failure:\n{spof_json}\n\n"
            f"Please generate:\n"
            f"1. A one-paragraph summary (for management)\n"
            f"2. Key risk points (max 3)\n"
            f"3. Recommended approval checkpoints\n"
        )

        bedrock = boto3.client("bedrock-runtime", region_name=REGION)
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000,
                }
            ),
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    def _static_summary(self, plan: DRPlan) -> str:
        """Generate a static template summary as fallback.

        Args:
            plan: DRPlan.

        Returns:
            Static summary string.
        """
        tier0_count = 0
        spof_count = 0
        if plan.impact_assessment:
            tier0_count = len(plan.impact_assessment.by_tier.get("Tier0", []))
            spof_count = len(plan.impact_assessment.single_points_of_failure)

        total_steps = sum(len(p.steps) for p in plan.phases)

        summary_lines = [
            f"## DR Plan Executive Summary",
            "",
            f"This {plan.scope}-level DR switchover plan transitions workloads "
            f"from **{plan.source}** to **{plan.target}**.",
            "",
            f"- **Affected services**: {len(plan.affected_services)} "
            f"(Tier0: {tier0_count})",
            f"- **Total switchover steps**: {total_steps} across {len(plan.phases)} phases",
            f"- **Estimated RTO**: {plan.estimated_rto} minutes",
            f"- **Estimated RPO**: {plan.estimated_rpo} minutes",
            "",
        ]

        if spof_count > 0:
            summary_lines += [
                f"**Risk**: {spof_count} single point(s) of failure detected. "
                "Review SPOF section before executing.",
                "",
            ]

        summary_lines += [
            "**Approval checkpoints**: Phase 0 pre-flight completion, "
            "Phase 1 data layer verification, Phase 3 DNS cutover.",
        ]

        return "\n".join(summary_lines)

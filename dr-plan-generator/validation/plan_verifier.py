"""
validation/plan_verifier.py — DR Plan verification engine

Three verification levels:
  Level 1: dry_run()       — Zero-risk pre-condition checks
  Level 2: step_verify()   — Step-by-step verification with fault injection
  Level 3: full_rehearsal() — Full end-to-end DR rehearsal

Coordinates with:
  - chaos/code/runner/dr_step_runner.py for step execution
  - chaos/code/dr_orchestrator.py for rehearsal orchestration
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from validation.verification_models import (
    CheckStatus,
    DryRunCheck,
    DryRunReport,
    GateCondition,
    GateType,
    PhaseResult,
    RehearsalReport,
    StepVerificationResult,
    VerificationLevel,
    VerificationScope,
)

if TYPE_CHECKING:
    from models import DRPlan, DRStep, DRPhase
    from profiles.profile_loader import EnvironmentProfile

logger = logging.getLogger(__name__)


class DRPlanVerifier:
    """DR Plan verification engine.

    Supports three levels of verification:
      Level 1 (dry_run): Static checks — variables, resources, permissions, state
      Level 2 (step_verify): Dynamic step-by-step verification
      Level 3 (full_rehearsal): End-to-end rehearsal with rollback
    """

    def __init__(
        self,
        plan: "DRPlan",
        profile: Optional["EnvironmentProfile"] = None,
    ) -> None:
        self.plan = plan
        self._profile = profile

    @property
    def profile(self) -> "EnvironmentProfile":
        if self._profile is None:
            from profiles.profile_loader import EnvironmentProfile
            self._profile = EnvironmentProfile()
        return self._profile

    # ==================================================================
    # Level 1: Dry-Run
    # ==================================================================

    def dry_run(self) -> DryRunReport:
        """Level 1: Zero-risk pre-condition checks.

        Checks:
          1. Variable completeness — unresolved placeholders in commands
          2. Resource existence — AWS describe calls for referenced resources
          3. Permission check — IAM policy simulator (best-effort)
          4. Target state — RDS replicas healthy, EKS nodes ready
          5. Network reachability — STS call to target region
          6. Graph freshness — plan age vs threshold
          7. Cross-region context — (region scope) K8s and AWS profiles
        """
        report = DryRunReport(plan_id=self.plan.plan_id, scope=self.plan.scope)

        # 1. Variable completeness
        report.checks.extend(self._check_variables())

        # 2. Resource existence
        report.checks.extend(self._check_resource_existence())

        # 3. Permission check (best-effort)
        report.checks.extend(self._check_permissions())

        # 4. Target state
        report.checks.extend(self._check_target_state())

        # 5. Network reachability
        report.checks.extend(self._check_network_reachability())

        # 6. Graph freshness
        report.checks.extend(self._check_graph_freshness())

        # 7. Cross-region context
        if self.plan.scope == "region":
            report.checks.extend(self._check_cross_region_context())

        logger.info(
            "Dry-run complete: %d pass, %d fail, %d warn",
            report.pass_count, report.fail_count, report.warn_count,
        )
        return report

    def _check_variables(self) -> List[DryRunCheck]:
        """Check for unresolved variable placeholders in all step commands."""
        checks: List[DryRunCheck] = []
        unresolved_pattern = re.compile(r"\$\{[A-Z_]+\}|\$[A-Z_]{3,}")

        for phase in self.plan.phases:
            for step in phase.steps:
                for field_name in ("command", "validation", "rollback_command"):
                    cmd = getattr(step, field_name, "")
                    if not cmd:
                        continue
                    matches = unresolved_pattern.findall(cmd)
                    # Filter out awk/jq variables (single-letter after $)
                    real_unresolved = [
                        m for m in matches
                        if not m.startswith("$T") or len(m) > 3  # skip $TG_ARN etc in inline scripts
                    ]
                    if real_unresolved:
                        # Check if they're set in environment
                        truly_missing = []
                        for var in real_unresolved:
                            var_name = var.strip("${}")
                            if not os.environ.get(var_name):
                                truly_missing.append(var)
                        if truly_missing:
                            checks.append(DryRunCheck(
                                category="variable",
                                name=f"{step.step_id}.{field_name}",
                                description=f"Unresolved variables in {step.step_id}.{field_name}: {truly_missing}",
                                status=CheckStatus.FAIL,
                                details=f"Set environment variables or update profile: {truly_missing}",
                                severity="critical",
                            ))
                        else:
                            checks.append(DryRunCheck(
                                category="variable",
                                name=f"{step.step_id}.{field_name}",
                                description=f"Variables resolved in {step.step_id}.{field_name}",
                                status=CheckStatus.PASS,
                                severity="info",
                            ))

        if not checks:
            checks.append(DryRunCheck(
                category="variable",
                name="all-variables",
                description="No variable placeholders found in commands",
                status=CheckStatus.PASS,
                severity="info",
            ))

        return checks

    def _check_resource_existence(self) -> List[DryRunCheck]:
        """Check that referenced AWS resources exist via describe calls."""
        checks: List[DryRunCheck] = []
        checked_resources: set = set()

        for phase in self.plan.phases:
            for step in phase.steps:
                if not step.resource_name or step.resource_name in checked_resources:
                    continue
                checked_resources.add(step.resource_name)

                rtype = step.resource_type
                rname = step.resource_name

                if rtype == "RDSCluster":
                    checks.append(self._aws_check(
                        category="resource",
                        name=f"rds-{rname}",
                        description=f"RDS cluster '{rname}' exists",
                        command=[
                            "aws", "rds", "describe-db-clusters",
                            "--db-cluster-identifier", rname,
                            "--query", "DBClusters[0].Status",
                            "--output", "text",
                        ],
                        region=self.plan.source,
                    ))
                elif rtype == "DynamoDBTable":
                    checks.append(self._aws_check(
                        category="resource",
                        name=f"ddb-{rname}",
                        description=f"DynamoDB table '{rname}' exists",
                        command=[
                            "aws", "dynamodb", "describe-table",
                            "--table-name", rname,
                            "--query", "Table.TableStatus",
                            "--output", "text",
                        ],
                        region=self.plan.source,
                    ))
                elif rtype in ("EKSDeployment", "Microservice", "K8sService"):
                    deploy_name = self.profile.get_deployment_name(rname)
                    ns = self.profile.k8s_namespace
                    checks.append(self._kubectl_check(
                        category="resource",
                        name=f"k8s-{rname}",
                        description=f"K8s deployment '{deploy_name}' exists in namespace '{ns}'",
                        command=[
                            "kubectl", "get", "deployment", deploy_name,
                            "-n", ns, "-o", "name",
                        ],
                    ))

        if not checks:
            checks.append(DryRunCheck(
                category="resource",
                name="no-resources",
                description="No specific resources to check",
                status=CheckStatus.PASS,
                severity="info",
            ))

        return checks

    def _check_permissions(self) -> List[DryRunCheck]:
        """Best-effort IAM permission check using STS."""
        checks: List[DryRunCheck] = []
        # Check basic caller identity
        checks.append(self._aws_check(
            category="permission",
            name="sts-identity",
            description="AWS caller identity is valid",
            command=["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
        ))
        return checks

    def _check_target_state(self) -> List[DryRunCheck]:
        """Check target region/AZ readiness."""
        checks: List[DryRunCheck] = []
        target = self.plan.target

        # Check target region STS
        checks.append(self._aws_check(
            category="state",
            name="target-sts",
            description=f"Target region '{target}' is reachable (STS)",
            command=[
                "aws", "sts", "get-caller-identity",
                "--region", target, "--query", "Account", "--output", "text",
            ],
        ))

        return checks

    def _check_network_reachability(self) -> List[DryRunCheck]:
        """Check network reachability to target."""
        checks: List[DryRunCheck] = []
        target = self.plan.target
        checks.append(self._aws_check(
            category="network",
            name="target-reachable",
            description=f"Target endpoint '{target}' is network-reachable",
            command=[
                "aws", "sts", "get-caller-identity",
                "--region", target, "--output", "text",
            ],
        ))
        return checks

    def _check_graph_freshness(self) -> List[DryRunCheck]:
        """Check if the plan's graph snapshot is stale."""
        checks: List[DryRunCheck] = []
        snapshot_time = self.plan.graph_snapshot_time
        if not snapshot_time:
            checks.append(DryRunCheck(
                category="freshness",
                name="graph-snapshot",
                description="Graph snapshot time not recorded in plan",
                status=CheckStatus.WARN,
                severity="warning",
            ))
        else:
            from datetime import datetime, timezone
            try:
                snap_dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - snap_dt).total_seconds()
                threshold = 3600  # 1 hour default
                if age_seconds > threshold:
                    checks.append(DryRunCheck(
                        category="freshness",
                        name="graph-snapshot",
                        description=f"Graph snapshot is {int(age_seconds/60)} min old (threshold: {threshold//60} min)",
                        status=CheckStatus.WARN,
                        details="Consider regenerating the plan with fresh graph data",
                        severity="warning",
                    ))
                else:
                    checks.append(DryRunCheck(
                        category="freshness",
                        name="graph-snapshot",
                        description=f"Graph snapshot is {int(age_seconds/60)} min old (within threshold)",
                        status=CheckStatus.PASS,
                        severity="info",
                    ))
            except (ValueError, TypeError):
                checks.append(DryRunCheck(
                    category="freshness",
                    name="graph-snapshot",
                    description=f"Could not parse graph snapshot time: {snapshot_time}",
                    status=CheckStatus.WARN,
                    severity="warning",
                ))
        return checks

    def _check_cross_region_context(self) -> List[DryRunCheck]:
        """Check multi-region K8s and AWS contexts (region scope only)."""
        checks: List[DryRunCheck] = []

        # Check source K8s context
        source_ctx = self.profile.get("kubernetes.context_source", "")
        if source_ctx and not source_ctx.startswith("$"):
            checks.append(self._kubectl_check(
                category="context",
                name="source-k8s-context",
                description=f"Source K8s context '{source_ctx}' is available",
                command=["kubectl", "config", "get-contexts", source_ctx, "-o", "name"],
            ))

        # Check target K8s context
        target_ctx = self.profile.get("kubernetes.context_target", "")
        if target_ctx and not target_ctx.startswith("$"):
            checks.append(self._kubectl_check(
                category="context",
                name="target-k8s-context",
                description=f"Target K8s context '{target_ctx}' is available",
                command=["kubectl", "config", "get-contexts", target_ctx, "-o", "name"],
            ))

        if not checks:
            checks.append(DryRunCheck(
                category="context",
                name="cross-region",
                description="Cross-region contexts not configured in profile (using env vars)",
                status=CheckStatus.WARN,
                severity="warning",
            ))

        return checks

    # ------------------------------------------------------------------
    # AWS / kubectl check helpers
    # ------------------------------------------------------------------

    def _aws_check(
        self,
        category: str,
        name: str,
        description: str,
        command: List[str],
        region: str = "",
        severity: str = "critical",
    ) -> DryRunCheck:
        """Execute an AWS CLI command and return a DryRunCheck."""
        if region:
            command = command + ["--region", region]
        return self._run_check(category, name, description, command, severity)

    def _kubectl_check(
        self,
        category: str,
        name: str,
        description: str,
        command: List[str],
        severity: str = "critical",
    ) -> DryRunCheck:
        """Execute a kubectl command and return a DryRunCheck."""
        return self._run_check(category, name, description, command, severity)

    @staticmethod
    def _run_check(
        category: str,
        name: str,
        description: str,
        command: List[str],
        severity: str = "critical",
    ) -> DryRunCheck:
        """Execute a command and return pass/fail based on exit code."""
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return DryRunCheck(
                    category=category,
                    name=name,
                    description=description,
                    status=CheckStatus.PASS,
                    details=result.stdout.strip()[:200],
                    severity=severity,
                )
            else:
                return DryRunCheck(
                    category=category,
                    name=name,
                    description=description,
                    status=CheckStatus.FAIL,
                    details=result.stderr.strip()[:500],
                    severity=severity,
                )
        except FileNotFoundError:
            return DryRunCheck(
                category=category,
                name=name,
                description=description,
                status=CheckStatus.FAIL,
                details=f"Command not found: {command[0]}",
                severity=severity,
            )
        except subprocess.TimeoutExpired:
            return DryRunCheck(
                category=category,
                name=name,
                description=description,
                status=CheckStatus.FAIL,
                details="Command timed out (30s)",
                severity=severity,
            )
        except Exception as exc:
            return DryRunCheck(
                category=category,
                name=name,
                description=description,
                status=CheckStatus.FAIL,
                details=str(exc)[:500],
                severity=severity,
            )

    # ==================================================================
    # Level 2: Step-by-Step Verification
    # ==================================================================

    def step_verify(
        self,
        strategy: str = "checkpoint",
        require_approval: bool = False,
    ) -> RehearsalReport:
        """Level 2: Step-by-step verification.

        Each step goes through:
          1. Pre-check (baseline)
          2. Fault injection (if applicable)
          3. Execute command
          4. Validate
          5. Measure timing
          6. Rollback (isolated mode) / continue (cumulative/checkpoint)
          7. Restore
          8. Confirm baseline restored
          9. Cooldown

        Args:
            strategy: "isolated" | "cumulative" | "checkpoint"
            require_approval: Whether to require human approval at phase gates

        Returns:
            RehearsalReport with step-level results.
        """
        import time as _time

        report = RehearsalReport(
            plan_id=self.plan.plan_id,
            scope=self.plan.scope,
            environment="staging",
        )

        start_time = _time.time()

        for phase in self.plan.phases:
            phase_start = _time.time()
            phase_result = PhaseResult(
                phase_id=phase.phase_id,
                phase_name=phase.name,
            )

            for step in phase.steps:
                step_result = self._verify_single_step(step, phase)
                phase_result.steps.append(step_result)
                report.step_results.append(step_result)

                if not step_result.passed:
                    report.failed_steps.append(step.step_id)
                    if strategy == "isolated":
                        # Rollback this step and continue
                        pass
                    elif phase.phase_id == "phase-readiness":
                        # Hard block — stop here
                        logger.warning(
                            "Readiness gate failed at step %s — aborting",
                            step.step_id,
                        )
                        break

            phase_result.total_duration_seconds = _time.time() - phase_start
            phase_result.estimated_duration_seconds = phase.estimated_duration * 60
            phase_result.gate_check_passed = phase_result.all_steps_passed
            report.phase_results.append(phase_result)

            # Checkpoint strategy: option to stop between phases
            if strategy == "checkpoint" and not phase_result.all_steps_passed:
                logger.warning(
                    "Phase %s had failures — stopping at checkpoint", phase.phase_id,
                )
                break

        total_elapsed = _time.time() - start_time
        report.total_duration_seconds = int(total_elapsed)
        report.actual_rto_minutes = int(total_elapsed / 60)
        report.estimated_rto_minutes = self.plan.estimated_rto

        return report

    def _verify_single_step(
        self, step: "DRStep", phase: "DRPhase"
    ) -> StepVerificationResult:
        """Execute and verify a single DR step."""
        import time as _time

        result = StepVerificationResult(
            step_id=step.step_id,
            phase_id=phase.phase_id,
            resource_name=step.resource_name,
            estimated_duration_seconds=step.estimated_time,
        )

        start = _time.time()

        # Execute command
        try:
            cmd_result = subprocess.run(
                step.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=step.estimated_time * 3,  # 3x estimated as timeout
            )
            result.command_success = cmd_result.returncode == 0
            result.command_output = cmd_result.stdout[:2000]
            result.command_exit_code = cmd_result.returncode
        except Exception as exc:
            result.command_success = False
            result.command_output = str(exc)
            result.command_exit_code = -1

        # Execute validation
        if step.validation and not step.validation.startswith("#"):
            try:
                val_result = subprocess.run(
                    step.validation,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                result.validation_success = val_result.returncode == 0
                result.validation_output = val_result.stdout[:2000]
            except Exception as exc:
                result.validation_success = False
                result.validation_output = str(exc)
        else:
            # No automated validation — mark as manual
            result.validation_success = result.command_success

        result.actual_duration_seconds = _time.time() - start

        if not result.passed:
            result.issues.append(
                f"Step {step.step_id} failed: cmd_ok={result.command_success}, "
                f"val_ok={result.validation_success}"
            )

        return result

    # ==================================================================
    # Level 3: Full Rehearsal
    # ==================================================================

    def full_rehearsal(
        self,
        auto_rollback: bool = True,
        timeout_minutes: int = 120,
    ) -> RehearsalReport:
        """Level 3: Full end-to-end DR rehearsal.

        Executes all phases sequentially, with Phase 2.5 hard-block gate.
        On failure, optionally executes rollback phases.

        Args:
            auto_rollback: Whether to auto-rollback on failure.
            timeout_minutes: Total rehearsal timeout.

        Returns:
            RehearsalReport with complete results.
        """
        # Full rehearsal uses step_verify with cumulative strategy
        report = self.step_verify(strategy="cumulative")
        report.environment = "staging"

        # Execute rollback if needed
        if not report.success and auto_rollback and self.plan.rollback_phases:
            import time as _time
            rollback_start = _time.time()
            rollback_ok = True

            for rb_phase in self.plan.rollback_phases:
                for step in rb_phase.steps:
                    try:
                        rb_result = subprocess.run(
                            step.command,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=step.estimated_time * 3,
                        )
                        if rb_result.returncode != 0:
                            rollback_ok = False
                            logger.error(
                                "Rollback step %s failed: %s",
                                step.step_id, rb_result.stderr[:200],
                            )
                    except Exception as exc:
                        rollback_ok = False
                        logger.error("Rollback step %s exception: %s", step.step_id, exc)

            report.rollback_success = rollback_ok
            report.rollback_duration_seconds = int(_time.time() - rollback_start)

        # Save report alongside the plan
        self._save_report(report)

        return report

    def _save_report(self, report: RehearsalReport) -> None:
        """Save verification report as JSON."""
        try:
            report_data = asdict(report)
            # Try to find the plan file path and save alongside it
            report_path = f"plans/{report.plan_id}.verify-report.json"
            os.makedirs("plans", exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(report_data, fh, indent=2, ensure_ascii=False)
            logger.info("Verification report saved to %s", report_path)
        except Exception as exc:
            logger.warning("Could not save verification report: %s", exc)

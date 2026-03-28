"""
planner/step_builder.py — Per-resource-type DR step command builder

Each builder method returns a fully populated DRStep with command,
validation, expected_result, and rollback_command.
"""

import logging
from typing import Any, Dict, Optional

from models import DRStep

logger = logging.getLogger(__name__)


class StepBuilder:
    """Build DRStep objects for each supported AWS resource type.

    For unsupported types, a generic placeholder step is generated.
    """

    def build_step(
        self,
        node: Dict[str, Any],
        source: str,
        target: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[DRStep]:
        """Dispatch to the correct per-type builder based on node type.

        For AZ-scope switchovers, regional/global services are skipped
        because they are inherently multi-AZ and unaffected by a single
        AZ failure (per AWS Fault Isolation Boundaries whitepaper).

        Args:
            node: Node dict with at least ``name`` and ``type`` keys.
            source: Source region/AZ being failed over from.
            target: Target region/AZ being failed over to.
            context: Optional extra context (e.g. step order counter).

        Returns:
            Populated DRStep, or ``None`` if the resource should be
            skipped for this switchover scope.
        """
        ctx = context or {}
        scope = ctx.get("scope", "region")
        resource_type_raw = node.get("type", "")

        # --- AZ switchover: skip regional/global services ---
        # Regional services (DynamoDB, SQS, S3, Lambda, etc.) are
        # inherently multi-AZ; they don't need AZ-level failover.
        if scope == "az":
            from registry import registry_loader
            reg = registry_loader.get_registry()
            fault_domain = reg.get_fault_domain(resource_type_raw)
            if fault_domain in ("regional", "global"):
                logger.info(
                    "Skipping %s '%s' for AZ switchover — %s service "
                    "(multi-AZ, no AZ-level failover needed).",
                    resource_type_raw, node.get("name", ""), fault_domain,
                )
                return None

        resource_type = resource_type_raw.lower()
        builder = getattr(self, f"_build_{resource_type}_step", None)
        if builder:
            return builder(node, source, target, ctx)
        logger.warning(
            "No dedicated step builder for resource type %r — falling back to generic step. "
            "Consider adding a _build_%s_step method or updating registry/custom_types.yaml.",
            resource_type_raw,
            resource_type,
        )
        return self._build_generic_step(node, source, target)

    # ------------------------------------------------------------------
    # Per-type builders
    # ------------------------------------------------------------------

    def _build_rdscluster_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """RDS/Aurora cluster failover step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for RDS cluster failover.
        """
        cluster_id = node["name"]
        return DRStep(
            step_id=f"rds-{cluster_id}",
            order=ctx.get("order", 0),
            resource_type="RDSCluster",
            resource_id=node.get("id", ""),
            resource_name=cluster_id,
            action="promote_read_replica",
            command=(
                f"aws rds failover-db-cluster "
                f"--db-cluster-identifier {cluster_id} "
                f"--region {target}"
            ),
            validation=(
                f"aws rds describe-db-clusters "
                f"--db-cluster-identifier {cluster_id} "
                f"--region {target} "
                f"--query 'DBClusters[0].Status' --output text"
            ),
            expected_result="available",
            rollback_command=(
                f"aws rds failover-db-cluster "
                f"--db-cluster-identifier {cluster_id} "
                f"--region {source}"
            ),
            estimated_time=300,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_rdsinstance_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """RDS instance reboot/failover step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for RDS instance failover.
        """
        instance_id = node["name"]
        return DRStep(
            step_id=f"rdsinstance-{instance_id}",
            order=ctx.get("order", 0),
            resource_type="RDSInstance",
            resource_id=node.get("id", ""),
            resource_name=instance_id,
            action="reboot_with_failover",
            command=(
                f"aws rds reboot-db-instance "
                f"--db-instance-identifier {instance_id} "
                f"--force-failover --region {target}"
            ),
            validation=(
                f"aws rds describe-db-instances "
                f"--db-instance-identifier {instance_id} "
                f"--region {target} "
                f"--query 'DBInstances[0].DBInstanceStatus' --output text"
            ),
            expected_result="available",
            rollback_command=(
                f"aws rds reboot-db-instance "
                f"--db-instance-identifier {instance_id} "
                f"--force-failover --region {source}"
            ),
            estimated_time=600,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_dynamodbtable_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """DynamoDB Global Table write-endpoint switch step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for DynamoDB Global Table failover.
        """
        table_name = node["name"]
        return DRStep(
            step_id=f"ddb-{table_name}",
            order=ctx.get("order", 0),
            resource_type="DynamoDBTable",
            resource_id=node.get("id", ""),
            resource_name=table_name,
            action="switch_global_table_region",
            command=(
                f"# DynamoDB Global Table: switch write endpoint to {target}\n"
                f"# Update application config (env var / Parameter Store)\n"
                f"aws ssm put-parameter --name '/petsite/dynamodb-region' "
                f"--value '{target}' --overwrite --region {target}"
            ),
            validation=(
                f"aws dynamodb describe-table "
                f"--table-name {table_name} --region {target} "
                f"--query 'Table.TableStatus' --output text"
            ),
            expected_result="ACTIVE",
            rollback_command=(
                f"aws ssm put-parameter --name '/petsite/dynamodb-region' "
                f"--value '{source}' --overwrite --region {source}"
            ),
            estimated_time=60,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_microservice_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """EKS microservice scale-up and verify step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for microservice DR activation.
        """
        svc_name = node["name"]
        tier = node.get("tier", "Tier2")
        return DRStep(
            step_id=f"svc-{svc_name}",
            order=ctx.get("order", 0),
            resource_type="Microservice",
            resource_id=node.get("id", ""),
            resource_name=svc_name,
            action="scale_up_and_verify",
            command=(
                f"kubectl scale deployment {svc_name} --replicas=3 "
                f"--context {target}-cluster\n"
                f"kubectl rollout status deployment/{svc_name} "
                f"--timeout=120s --context {target}-cluster"
            ),
            validation=(
                f"kubectl get deployment {svc_name} "
                f"--context {target}-cluster "
                f"-o jsonpath='{{.status.readyReplicas}}'"
            ),
            expected_result="3",
            rollback_command=(
                f"kubectl scale deployment {svc_name} --replicas=0 "
                f"--context {target}-cluster"
            ),
            estimated_time=120,
            requires_approval=(tier == "Tier0"),
            tier=tier,
            dependencies=[],
        )

    def _build_loadbalancer_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """ALB/NLB health-check verification and DNS cutover step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for load balancer DNS switch.
        """
        lb_name = node["name"]
        return DRStep(
            step_id=f"lb-{lb_name}",
            order=ctx.get("order", 0),
            resource_type="LoadBalancer",
            resource_id=node.get("id", ""),
            resource_name=lb_name,
            action="verify_health_and_switch_dns",
            command=(
                f"# 1. Verify target ALB health\n"
                f"aws elbv2 describe-target-health "
                f"--target-group-arn $TG_ARN --region {target}\n"
                f"# 2. Switch Route 53 DNS\n"
                f"aws route53 change-resource-record-sets "
                f"--hosted-zone-id $ZONE_ID "
                f"--change-batch file://dns-failover.json"
            ),
            validation=f"dig +short petsite.example.com",
            expected_result="<target ALB DNS>",
            rollback_command=(
                f"aws route53 change-resource-record-sets "
                f"--hosted-zone-id $ZONE_ID "
                f"--change-batch file://dns-rollback.json"
            ),
            estimated_time=180,
            requires_approval=True,
            tier=None,
            dependencies=[],
        )

    def _build_lambdafunction_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """Lambda function validation and region switch step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for Lambda function DR activation.
        """
        fn_name = node["name"]
        return DRStep(
            step_id=f"lambda-{fn_name}",
            order=ctx.get("order", 0),
            resource_type="LambdaFunction",
            resource_id=node.get("id", ""),
            resource_name=fn_name,
            action="verify_lambda_function",
            command=(
                f"aws lambda invoke --function-name {fn_name} "
                f"--region {target} "
                f"--payload '{{\"source\": \"dr-healthcheck\"}}' "
                f"/tmp/{fn_name}-response.json\n"
                f"cat /tmp/{fn_name}-response.json"
            ),
            validation=(
                f"aws lambda get-function-configuration "
                f"--function-name {fn_name} --region {target} "
                f"--query 'State' --output text"
            ),
            expected_result="Active",
            rollback_command=(
                f"# Lambda functions are stateless; update event source mapping\n"
                f"aws lambda update-event-source-mapping "
                f"--region {source} "
                f"--uuid $EVENT_SOURCE_UUID --enabled"
            ),
            estimated_time=30,
            requires_approval=False,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_k8sservice_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """Kubernetes Service endpoint update step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for K8s service DR activation.
        """
        svc_name = node["name"]
        return DRStep(
            step_id=f"k8ssvc-{svc_name}",
            order=ctx.get("order", 0),
            resource_type="K8sService",
            resource_id=node.get("id", ""),
            resource_name=svc_name,
            action="verify_k8s_service_endpoints",
            command=(
                f"kubectl get endpoints {svc_name} "
                f"--context {target}-cluster\n"
                f"kubectl describe service {svc_name} "
                f"--context {target}-cluster"
            ),
            validation=(
                f"kubectl get endpoints {svc_name} "
                f"--context {target}-cluster "
                f"-o jsonpath='{{.subsets[0].addresses[0].ip}}'"
            ),
            expected_result="<non-empty IP>",
            rollback_command=(
                f"kubectl delete endpoints {svc_name} "
                f"--context {target}-cluster\n"
                f"# Service endpoints will repopulate from source cluster"
            ),
            estimated_time=60,
            requires_approval=False,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_generic_step(
        self, node: Dict[str, Any], source: str, target: str
    ) -> DRStep:
        """Generic fallback step for unsupported resource types.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.

        Returns:
            Placeholder DRStep that requires manual intervention.
        """
        resource_name = node.get("name", "unknown")
        resource_type = node.get("type", "Unknown")
        return DRStep(
            step_id=f"generic-{resource_name}",
            order=0,
            resource_type=resource_type,
            resource_id=node.get("id", ""),
            resource_name=resource_name,
            action="manual_switchover",
            command=(
                f"# TODO: Manual switchover required for {resource_type} '{resource_name}'\n"
                f"# Source: {source} → Target: {target}\n"
                f"# Add the appropriate AWS CLI command here."
            ),
            validation=(
                f"# TODO: Add verification command for {resource_name}"
            ),
            expected_result="Resource healthy in target",
            rollback_command=(
                f"# TODO: Add rollback command for {resource_name}"
            ),
            estimated_time=120,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

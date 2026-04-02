"""
fis_backend.py - AWS FIS 故障注入后端

通过 boto3 直接调用 FIS API（确定性执行路径）。
负责：Lambda 故障 / RDS failover / EC2/EBS 故障 / 网络基础设施 / API 注入

设计原则（ADR-003）：
- 执行阶段走 boto3 直调，不经过 MCP/LLM
- 紧急熔断走最短路径：fis.stop_experiment()
- FIS 原生 CloudWatch Alarm Stop Conditions 作为兜底安全网
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from .experiment import Experiment

from .config import REGION, ACCOUNT_ID, FIS_ROLE_ARN
from .fault_registry import FIS_ACTION_MAP

logger = logging.getLogger(__name__)


def _to_iso_duration(duration: str) -> str:
    """'2m' → 'PT2M', '30s' → 'PT30S', '1h' → 'PT1H'"""
    if duration.endswith("m"):
        return f"PT{duration[:-1]}M"
    elif duration.endswith("s"):
        return f"PT{duration[:-1]}S"
    elif duration.endswith("h"):
        return f"PT{duration[:-1]}H"
    return duration


# FIS action_id → target key name used inside the action's `targets` field
# Source: aws fis get-action --id <action_id>
FIS_TARGET_KEY_MAP: dict[str, str | None] = {
    "aws:lambda:invocation-add-delay":                  "Functions",
    "aws:lambda:invocation-error":                      "Functions",
    "aws:lambda:invocation-http-integration-response":  "Functions",
    "aws:rds:failover-db-cluster":                      "Clusters",
    "aws:rds:reboot-db-instances":                      "DBInstances",
    "aws:eks:terminate-nodegroup-instances":             "Nodegroups",
    "aws:eks:pod-network-latency":                      "Pods",
    "aws:eks:pod-network-packet-loss":                  "Pods",
    "aws:eks:pod-delete":                               "Pods",
    "aws:eks:pod-cpu-stress":                           "Pods",
    "aws:eks:pod-memory-stress":                        "Pods",
    "aws:eks:pod-io-stress":                            "Pods",
    "aws:eks:pod-network-blackhole-port":               "Pods",
    "aws:eks:inject-kubernetes-custom-resource":        "Pods",
    "aws:ec2:stop-instances":                           "Instances",
    "aws:ec2:terminate-instances":                      "Instances",
    "aws:ec2:reboot-instances":                         "Instances",
    "aws:ec2:api-insufficient-instance-capacity-error": "Roles",
    "aws:ec2:asg-insufficient-instance-capacity-error": "AutoScalingGroups",
    "aws:ssm:send-command":                             "Instances",
    "aws:ebs:pause-volume-io":                          "Volumes",
    "aws:ebs:volume-io-latency":                        "Volumes",
    "aws:network:disrupt-connectivity":                 "Subnets",
    "aws:network:disrupt-vpc-endpoint":                 "VpcEndpoints",
    "aws:fis:inject-api-internal-error":                "Roles",
    "aws:fis:inject-api-throttle-error":                "Roles",
    "aws:fis:inject-api-unavailable-error":             "Roles",
    "aws:arc:start-zonal-autoshift":                    "ManagedResources",
    "aws:ec2:send-spot-instance-interruptions":          "Instances",
    "aws:ec2:disrupt-network-connectivity":              "Instances",
    "aws:dynamodb:global-table-pause-replication":       "Tables",
    "aws:elasticache:interrupt-cluster-az-power":        "ReplicationGroups",
    "aws:s3:bucket-pause-replication":                   "Buckets",
    "aws:network:route-table-disrupt-cross-region-connectivity": "RouteTables",
}

# Actions that do NOT accept a 'duration' parameter
FIS_ACTIONS_WITHOUT_DURATION = {
    "aws:rds:failover-db-cluster",
    "aws:rds:reboot-db-instances",
    "aws:eks:terminate-nodegroup-instances",
    "aws:ec2:stop-instances",
    "aws:ec2:terminate-instances",
    "aws:ec2:reboot-instances",
    "aws:eks:pod-delete",
}


class FISClient:
    """
    AWS FIS 故障注入客户端。
    接口与 ChaosMCPClient 对齐，Runner 可按 backend 字段切换。
    """

    def __init__(self):
        self._fis = None

    @property
    def fis(self):
        if self._fis is None:
            self._fis = boto3.client("fis", region_name=REGION)
        return self._fis

    def inject(self, experiment: "Experiment") -> dict:
        """
        创建 FIS 实验模板 + 启动实验。
        返回 {"experiment_id": ..., "template_id": ...}
        """
        ft = experiment.fault
        extra = ft.extra_params or {}

        action_id = FIS_ACTION_MAP.get(ft.type)
        if not action_id:
            raise ValueError(f"未知 FIS fault type: {ft.type}")

        target_key = FIS_TARGET_KEY_MAP.get(action_id)

        # 构建 action
        action_params: dict[str, str] = {}
        if action_id not in FIS_ACTIONS_WITHOUT_DURATION:
            action_params["duration"] = _to_iso_duration(ft.duration)
        if "percentage" in extra and action_id.startswith("aws:lambda:"):
            action_params["invocationPercentage"] = str(extra["percentage"])
        if "delay_ms" in extra and action_id.startswith("aws:lambda:"):
            action_params["startupDelayMilliseconds"] = str(extra["delay_ms"])
        # Lambda HTTP response params
        if action_id == "aws:lambda:invocation-http-integration-response":
            action_params["statusCode"] = str(extra.get("status_code", 502))
            action_params["contentTypeHeader"] = extra.get("content_type_header", "application/json")
            action_params["preventExecution"] = str(extra.get("prevent_execution", "true")).lower()
        if "read_io_latency_ms" in extra:
            action_params["readIOLatencyMilliseconds"] = str(extra["read_io_latency_ms"])
        if "write_io_latency_ms" in extra:
            action_params["writeIOLatencyMilliseconds"] = str(extra["write_io_latency_ms"])
        if "scope" in extra:
            action_params["scope"] = extra["scope"]
        if action_id == "aws:eks:terminate-nodegroup-instances":
            action_params["instanceTerminationPercentage"] = str(
                extra.get("instance_termination_percentage", 50)
            )
        if action_id == "aws:rds:reboot-db-instances" and "force_failover" in extra:
            action_params["forceFailover"] = str(extra["force_failover"]).lower()
        # EC2 Spot interruption params
        if action_id == "aws:ec2:send-spot-instance-interruptions":
            action_params["durationBeforeInterruption"] = extra.get(
                "durationBeforeInterruption", "PT2M"
            )
        # EC2 network disrupt params
        if action_id == "aws:ec2:disrupt-network-connectivity":
            action_params["scope"] = extra.get("scope", "all")

        # FIS API Injection 特有参数
        if action_id.startswith("aws:fis:inject-api-"):
            action_params["service"] = extra.get("service", "ec2")
            action_params["operations"] = extra.get("operations", "all")
            action_params["percentage"] = str(extra.get("percentage", 100))
        # EC2 API insufficient capacity 特有参数
        if action_id in ("aws:ec2:api-insufficient-instance-capacity-error",
                         "aws:ec2:asg-insufficient-instance-capacity-error"):
            action_params["percentage"] = str(extra.get("percentage", 100))
            action_params["availabilityZoneIdentifiers"] = extra.get(
                "availability_zone_identifiers",
                f"{REGION}a"
            )

        # EKS Pod action 特有参数
        if action_id == "aws:eks:pod-network-latency":
            action_params["delayMilliseconds"] = str(extra.get("delay_ms", 200))
            action_params["flowsPercent"] = str(extra.get("flows_percent", 100))
            action_params["interface"] = extra.get("interface", "DEFAULT")
            if extra.get("sources"):
                action_params["sources"] = extra["sources"]
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-network-packet-loss":
            action_params["lossPercent"] = str(extra.get("loss_percent", 15))
            action_params["flowsPercent"] = str(extra.get("flows_percent", 100))
            action_params["interface"] = extra.get("interface", "DEFAULT")
            if extra.get("sources"):
                action_params["sources"] = extra["sources"]
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-delete":
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-cpu-stress":
            action_params["workers"] = str(extra.get("workers", 1))
            action_params["percent"] = str(extra.get("cpu_percent", 80))
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-memory-stress":
            action_params["workers"] = str(extra.get("workers", 1))
            action_params["percent"] = str(extra.get("memory_percent", 80))
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-io-stress":
            action_params["workers"] = str(extra.get("workers", 1))
            action_params["percent"] = str(extra.get("io_percent", 80))
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )
        elif action_id == "aws:eks:pod-network-blackhole-port":
            action_params["trafficType"] = extra.get("traffic_type", "ingress")
            action_params["port"] = str(extra.get("port", 80))
            if extra.get("protocol"):
                action_params["protocol"] = extra["protocol"]
            action_params["kubernetesServiceAccount"] = extra.get(
                "kubernetes_service_account", "fis-service-account"
            )

        # SSM send-command 特有参数
        if ft.type.startswith("fis_ssm"):
            ssm_doc_map = {
                "fis_ssm_network_latency": "AWSFIS-Run-Network-Latency-Sources",
                "fis_ssm_network_loss":    "AWSFIS-Run-Network-Packet-Loss-Sources",
                "fis_ssm_cpu_stress":      "AWSFIS-Run-CPU-Stress",
                "fis_ssm_disk_stress":     "AWSFIS-Run-Disk-Fill",
            }
            doc_name = ssm_doc_map.get(ft.type, "AWSFIS-Run-CPU-Stress")
            doc_arn = f"arn:aws:ssm:{REGION}::document/{doc_name}"
            action_params["documentArn"] = doc_arn
            if extra.get("document_parameters"):
                import json as _json
                action_params["documentParameters"] = _json.dumps(extra["document_parameters"])

        # target_name is the shared key between action.targets and template.targets
        target_name = "main-target"
        action: dict = {
            "actionId": action_id,
            "parameters": action_params,
            "targets": {target_key: target_name} if target_key else {},
        }

        # 构建 target（仅当 action 需要 targets 时）
        if target_key:
            target = self._build_target(ft.type, extra)
            template_targets = {target_name: target}
        else:
            template_targets = {}

        # 构建 Stop Conditions
        stop_conditions = []
        for sc in experiment.stop_conditions:
            if sc.cloudwatch_alarm_arn:
                stop_conditions.append({
                    "source": "aws:cloudwatch:alarm",
                    "value": sc.cloudwatch_alarm_arn,
                })

        # 创建实验模板
        logger.info(f"创建 FIS 实验模板: {ft.type} → {action_id}")
        template_resp = self.fis.create_experiment_template(
            description=experiment.description or f"chaos-runner: {experiment.name}",
            actions={"fault-action": action},
            targets=template_targets,
            stopConditions=stop_conditions or [{"source": "none"}],
            roleArn=FIS_ROLE_ARN,
            tags={
                "chaos-automation": "true",
                "experiment-name": experiment.name,
                "target-service": experiment.target_service,
            },
        )
        template_id = template_resp["experimentTemplate"]["id"]
        logger.info(f"FIS 模板已创建: {template_id}")

        # 启动实验
        exp_resp = self.fis.start_experiment(
            experimentTemplateId=template_id,
            tags={"experiment-name": experiment.name},
        )
        experiment_id = exp_resp["experiment"]["id"]
        logger.info(f"✅ FIS 实验已启动: {experiment_id}")

        return {
            "experiment_id": experiment_id,
            "template_id": template_id,
        }

    def _build_target(self, fault_type: str, extra: dict) -> dict:
        """根据 fault_type 构建 FIS target"""
        if fault_type.startswith("fis_lambda"):
            return {
                "resourceType": "aws:lambda:function",
                "resourceArns": [extra["function_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_rds_failover":
            return {
                "resourceType": "aws:rds:cluster",
                "resourceArns": [extra["cluster_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_rds_reboot":
            # FIS reboot-db-instances targets individual DB instances (aws:rds:db),
            # not the cluster. Resolve the writer instance ARN from the cluster ARN.
            cluster_arn = extra["cluster_arn"]
            cluster_id = cluster_arn.split(":")[-1]
            rds = boto3.client("rds", region_name=REGION)
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            writer = next(
                m for m in resp["DBClusters"][0]["DBClusterMembers"]
                if m["IsClusterWriter"]
            )
            instance_arn = (
                f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:db:"
                f"{writer['DBInstanceIdentifier']}"
            )
            return {
                "resourceType": "aws:rds:db",
                "resourceArns": [instance_arn],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_eks_terminate_node":
            return {
                "resourceType": "aws:eks:nodegroup",
                "resourceArns": [extra["nodegroup_arn"]],
                "selectionMode": extra.get("selection_mode", "COUNT(1)"),
            }
        elif fault_type == "fis_ec2_insufficient_capacity":
            return {
                "resourceType": "aws:iam:role",
                "resourceArns": [extra["role_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_ec2_asg_insufficient_capacity":
            return {
                "resourceType": "aws:ec2:autoscaling-group",
                "resourceArns": [extra["asg_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type.startswith("fis_ec2"):
            target = {
                "resourceType": "aws:ec2:instance",
                "selectionMode": extra.get("selection_mode", "COUNT(1)"),
            }
            if "instance_arns" in extra:
                arns = extra["instance_arns"]
                target["resourceArns"] = [arns] if isinstance(arns, str) else arns
            elif "instance_ids" in extra:
                target["resourceArns"] = [
                    f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/{iid}"
                    for iid in extra["instance_ids"]
                ]
            else:
                target["resourceTags"] = extra.get("tags", {"chaos-target": "true"})
            return target
        elif fault_type.startswith("fis_ebs"):
            # volume_arns: 预解析的完整 ARN 列表（TargetResolver 写入）
            # volume_ids:  旧格式 volume ID 列表（兜底）
            if "volume_arns" in extra:
                arns = extra["volume_arns"]
                if isinstance(arns, str):
                    arns = [arns]
            else:
                arns = [
                    f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/{vid}"
                    for vid in extra.get("volume_ids", [])
                ]
            return {
                "resourceType": "aws:ec2:ebs-volume",
                "resourceArns": arns,
                "selectionMode": "ALL",
            }
        elif fault_type.startswith("fis_network") or fault_type.startswith("fis_vpc"):
            return {
                "resourceType": "aws:ec2:subnet",
                "resourceArns": [extra["subnet_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type.startswith("fis_eks_pod"):
            # FIS native EKS pod actions — 通过 cluster + namespace + label 选择 Pod
            target = {
                "resourceType": "aws:eks:pod",
                "selectionMode": extra.get("selection_mode", "ALL"),
                "parameters": {
                    "clusterIdentifier": extra.get("cluster_name", "PetSite"),
                    "namespace": extra.get("namespace", "petadoptions"),
                    "selectorType": extra.get("selector_type", "labelSelector"),
                    "selectorValue": extra.get("selector_value", ""),
                },
            }
            if extra.get("availability_zone"):
                target["parameters"]["availabilityZoneIdentifier"] = extra["availability_zone"]
            return target
        elif fault_type.startswith("fis_ssm"):
            # SSM send-command — target EC2 instances by tag or ARN
            target = {
                "resourceType": "aws:ec2:instance",
                "selectionMode": extra.get("selection_mode", "ALL"),
            }
            if "instance_arns" in extra:
                arns = extra["instance_arns"]
                target["resourceArns"] = [arns] if isinstance(arns, str) else arns
            else:
                target["resourceTags"] = extra.get("tags", {"chaos-target": "true"})
            if extra.get("availability_zone"):
                target["filters"] = [
                    {"path": "Placement.AvailabilityZone", "values": [extra["availability_zone"]]}
                ]
            return target
        elif fault_type.startswith("fis_api"):
            # API injection targets an IAM role
            return {
                "resourceType": "aws:iam:role",
                "resourceArns": [extra["role_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_ec2_insufficient_capacity":
            return {
                "resourceType": "aws:iam:role",
                "resourceArns": [extra["role_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_ec2_asg_insufficient_capacity":
            return {
                "resourceType": "aws:ec2:autoscaling-group",
                "resourceArns": [extra["asg_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_arc_zonal_autoshift":
            return {
                "resourceType": "aws:arc:zonal-shift-managed-resource",
                "resourceArns": [extra["managed_resource_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_ec2_spot_interruption":
            target = {
                "resourceType": "aws:ec2:spot-instance",
                "selectionMode": extra.get("selection_mode", "COUNT(1)"),
            }
            if "instance_arns" in extra:
                arns = extra["instance_arns"]
                target["resourceArns"] = [arns] if isinstance(arns, str) else arns
            else:
                target["resourceTags"] = extra.get("tags", {"chaos-target": "true"})
            return target
        elif fault_type == "fis_ec2_network_disrupt":
            target = {
                "resourceType": "aws:ec2:instance",
                "selectionMode": extra.get("selection_mode", "ALL"),
            }
            if "instance_arns" in extra:
                arns = extra["instance_arns"]
                target["resourceArns"] = [arns] if isinstance(arns, str) else arns
            else:
                target["resourceTags"] = extra.get("tags", {"chaos-target": "true"})
            return target
        elif fault_type == "fis_dynamodb_pause_replication":
            return {
                "resourceType": "aws:dynamodb:global-table",
                "resourceArns": [extra["global_table_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_elasticache_az_power":
            return {
                "resourceType": "aws:elasticache:replicationgroup",
                "resourceArns": [extra["cluster_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_s3_pause_replication":
            return {
                "resourceType": "aws:s3:bucket",
                "resourceArns": [extra["bucket_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_network_cross_region_route":
            return {
                "resourceType": "aws:ec2:route-table",
                "resourceArns": [extra["route_table_arn"]],
                "selectionMode": "ALL",
            }
        else:
            raise ValueError(f"Cannot build FIS target for: {fault_type}")

    def stop(self, experiment_id: str) -> None:
        """停止 FIS 实验（紧急熔断 — 最短路径，不经过 LLM/MCP）"""
        if not experiment_id:
            return
        try:
            self.fis.stop_experiment(id=experiment_id)
            logger.info(f"✅ FIS 实验已停止: {experiment_id}")
        except Exception as e:
            logger.error(f"FIS stop_experiment 失败: {e}")

    def status(self, experiment_id: str) -> str:
        """查询 FIS 实验状态"""
        try:
            resp = self.fis.get_experiment(id=experiment_id)
            state = resp["experiment"]["state"]["status"]
            return state  # initiating / running / completed / stopping / stopped / failed
        except Exception as e:
            logger.error(f"FIS get_experiment 失败: {e}")
            return "unknown"

    def delete_template(self, template_id: str) -> None:
        """清理 FIS 实验模板（实验结束后）"""
        if not template_id:
            return
        try:
            self.fis.delete_experiment_template(id=template_id)
            logger.info(f"✅ FIS 模板已删除: {template_id}")
        except Exception as e:
            logger.warning(f"FIS 模板删除失败（非致命）: {e}")

    def preflight_check(self) -> bool:
        """验证 FIS 服务可用"""
        try:
            self.fis.list_experiments(maxResults=1)
            return True
        except Exception as e:
            logger.error(f"FIS preflight 失败: {e}")
            return False

    # ─── FIS Scenario Library：多 action 复合场景 ─────────────────────────

    def inject_scenario(self, experiment: "Experiment") -> dict:
        """
        创建 FIS Scenario Library 多 action 模板 + 启动实验。
        根据 fault.type 选择对应的 JSON skeleton builder，
        缺失资源 ARN 的 action 会被 skip（不阻塞整个实验）。
        返回 {"experiment_id": ..., "template_id": ...}
        """
        ft = experiment.fault
        extra = ft.extra_params or {}
        duration_iso = _to_iso_duration(ft.duration)

        builders = {
            "fis_scenario_az_power_interruption": self._build_az_power_template,
            "fis_scenario_az_app_slowdown": self._build_az_app_slowdown_template,
            "fis_scenario_cross_az_traffic_slowdown": self._build_cross_az_traffic_template,
            "fis_scenario_cross_region_connectivity": self._build_cross_region_template,
        }
        builder = builders.get(ft.type)
        if not builder:
            raise ValueError(f"未知 FIS scenario type: {ft.type}")

        actions, targets = builder(extra, duration_iso)

        if not actions:
            raise ValueError(f"场景 {ft.type} 所有 action 均因缺少资源 ARN 被跳过，无法执行")

        # 构建 Stop Conditions
        stop_conditions = []
        for sc in experiment.stop_conditions:
            if sc.cloudwatch_alarm_arn:
                stop_conditions.append({
                    "source": "aws:cloudwatch:alarm",
                    "value": sc.cloudwatch_alarm_arn,
                })

        logger.info(f"创建 FIS scenario 模板: {ft.type} ({len(actions)} actions, {len(targets)} targets)")
        template_resp = self.fis.create_experiment_template(
            description=experiment.description or f"chaos-runner scenario: {experiment.name}",
            actions=actions,
            targets=targets,
            stopConditions=stop_conditions or [{"source": "none"}],
            roleArn=FIS_ROLE_ARN,
            tags={
                "chaos-automation": "true",
                "experiment-name": experiment.name,
                "target-service": experiment.target_service,
                "scenario-type": ft.type,
            },
        )
        template_id = template_resp["experimentTemplate"]["id"]
        logger.info(f"FIS scenario 模板已创建: {template_id}")

        exp_resp = self.fis.start_experiment(
            experimentTemplateId=template_id,
            tags={"experiment-name": experiment.name},
        )
        experiment_id = exp_resp["experiment"]["id"]
        logger.info(f"✅ FIS scenario 实验已启动: {experiment_id}")

        return {
            "experiment_id": experiment_id,
            "template_id": template_id,
        }

    def _build_az_power_template(
        self, extra: dict, duration_iso: str
    ) -> tuple[dict, dict]:
        """AZ Power Interruption: stop-ec2 + pause-ebs-io + failover-rds + interrupt-elasticache"""
        az = extra.get("availability_zone", f"{REGION}a")
        actions: dict = {}
        targets: dict = {}

        # EC2 stop — tag 筛选 + AZ filter
        actions["stop-ec2"] = {
            "actionId": "aws:ec2:stop-instances",
            "parameters": {"startInstancesAfterDuration": duration_iso},
            "targets": {"Instances": "ec2-instances"},
        }
        targets["ec2-instances"] = {
            "resourceType": "aws:ec2:instance",
            "resourceTags": {"AzImpairmentPower": "IceQualified"},
            "filters": [{"path": "Placement.AvailabilityZone", "values": [az]}],
            "selectionMode": "ALL",
        }

        # EBS pause IO — tag 筛选 + AZ filter
        actions["pause-ebs-io"] = {
            "actionId": "aws:ebs:pause-volume-io",
            "parameters": {"duration": duration_iso},
            "targets": {"Volumes": "ebs-volumes"},
        }
        targets["ebs-volumes"] = {
            "resourceType": "aws:ebs:volume",
            "resourceTags": {"AzImpairmentPower": "IceQualified"},
            "filters": [{"path": "AvailabilityZone", "values": [az]}],
            "selectionMode": "ALL",
        }

        # RDS failover（可选 — 需要 cluster ARN）
        rds_arn = extra.get("rds_cluster_arn")
        if rds_arn:
            actions["failover-rds"] = {
                "actionId": "aws:rds:failover-db-cluster",
                "parameters": {},
                "targets": {"Clusters": "rds-clusters"},
            }
            targets["rds-clusters"] = {
                "resourceType": "aws:rds:cluster",
                "resourceArns": [rds_arn],
                "selectionMode": "ALL",
            }
        else:
            logger.warning("AZ Power: rds_cluster_arn 缺失，跳过 failover-rds action")

        # ElastiCache interrupt（可选 — 需要 cluster ARN）
        elasticache_arn = extra.get("elasticache_arn")
        if elasticache_arn:
            actions["interrupt-elasticache"] = {
                "actionId": "aws:elasticache:interrupt-cluster-az-power",
                "parameters": {"duration": duration_iso},
                "targets": {"ReplicationGroups": "elasticache-clusters"},
            }
            targets["elasticache-clusters"] = {
                "resourceType": "aws:elasticache:replicationgroup",
                "resourceArns": [elasticache_arn],
                "selectionMode": "ALL",
            }
        else:
            logger.warning("AZ Power: elasticache_arn 缺失，跳过 interrupt-elasticache action")

        return actions, targets

    def _build_az_app_slowdown_template(
        self, extra: dict, duration_iso: str
    ) -> tuple[dict, dict]:
        """AZ App Slowdown: disrupt-network + slow-lambda"""
        az = extra.get("availability_zone", f"{REGION}a")
        scope = extra.get("scope", "availability-zone")
        actions: dict = {}
        targets: dict = {}

        # 网络中断 — subnet tag 筛选 + AZ filter
        actions["disrupt-network"] = {
            "actionId": "aws:network:disrupt-connectivity",
            "parameters": {"duration": duration_iso, "scope": scope},
            "targets": {"Subnets": "ec2-subnets"},
        }
        targets["ec2-subnets"] = {
            "resourceType": "aws:ec2:subnet",
            "resourceTags": {"AzImpairmentPower": "IceQualified"},
            "filters": [{"path": "AvailabilityZone", "values": [az]}],
            "selectionMode": "ALL",
        }

        # Lambda 延迟（可选 — 需要 function ARN）
        lambda_arn = extra.get("lambda_function_arn")
        if lambda_arn:
            actions["slow-lambda"] = {
                "actionId": "aws:lambda:invocation-add-delay",
                "parameters": {
                    "duration": duration_iso,
                    "invocationPercentage": str(extra.get("percentage", 100)),
                    "startupDelayMilliseconds": str(extra.get("delay_ms", 2000)),
                },
                "targets": {"Functions": "lambda-functions"},
            }
            targets["lambda-functions"] = {
                "resourceType": "aws:lambda:function",
                "resourceArns": [lambda_arn],
                "selectionMode": "ALL",
            }
        else:
            logger.warning("AZ App Slowdown: lambda_function_arn 缺失，跳过 slow-lambda action")

        return actions, targets

    def _build_cross_az_traffic_template(
        self, extra: dict, duration_iso: str
    ) -> tuple[dict, dict]:
        """Cross-AZ Traffic Slowdown: disrupt-cross-az-traffic"""
        az = extra.get("availability_zone", f"{REGION}a")
        actions: dict = {}
        targets: dict = {}

        actions["disrupt-cross-az-traffic"] = {
            "actionId": "aws:network:disrupt-connectivity",
            "parameters": {"duration": duration_iso, "scope": "availability-zone"},
            "targets": {"Subnets": "target-subnets"},
        }
        targets["target-subnets"] = {
            "resourceType": "aws:ec2:subnet",
            "resourceTags": {"AzImpairmentPower": "IceQualified"},
            "filters": [{"path": "AvailabilityZone", "values": [az]}],
            "selectionMode": "ALL",
        }

        return actions, targets

    def _build_cross_region_template(
        self, extra: dict, duration_iso: str
    ) -> tuple[dict, dict]:
        """Cross-Region Connectivity: disrupt-route-tables + disrupt-tgw"""
        actions: dict = {}
        targets: dict = {}

        # Route table（可选）
        route_table_arn = extra.get("route_table_arn")
        if route_table_arn:
            actions["disrupt-route-tables"] = {
                "actionId": "aws:network:route-table-disrupt-cross-region-connectivity",
                "parameters": {"duration": duration_iso},
                "targets": {"RouteTables": "route-tables"},
            }
            targets["route-tables"] = {
                "resourceType": "aws:ec2:routeTable",
                "resourceArns": [route_table_arn],
                "selectionMode": "ALL",
            }
        else:
            logger.warning("Cross-Region: route_table_arn 缺失，跳过 disrupt-route-tables action")

        # Transit Gateway（可选）
        tgw_arn = extra.get("transit_gateway_arn")
        if tgw_arn:
            actions["disrupt-tgw"] = {
                "actionId": "aws:network:transit-gateway-disrupt-cross-region-connectivity",
                "parameters": {"duration": duration_iso},
                "targets": {"TransitGateways": "transit-gateways"},
            }
            targets["transit-gateways"] = {
                "resourceType": "aws:ec2:transit-gateway",
                "resourceArns": [tgw_arn],
                "selectionMode": "ALL",
            }
        else:
            logger.warning("Cross-Region: transit_gateway_arn 缺失，跳过 disrupt-tgw action")

        return actions, targets

    def wait_for_completion(self, experiment_id: str, timeout: int = 600,
                            poll_interval: int = 10) -> str:
        """
        等待 FIS 实验完成（Phase 3/4 使用）。
        FIS 实验到期会自动完成，这里只是轮询状态。
        返回最终状态。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.status(experiment_id)
            if state in ("completed", "stopped", "failed", "cancelled"):
                return state
            time.sleep(poll_interval)
        logger.warning(f"FIS 实验等待超时: {experiment_id}")
        return "timeout"

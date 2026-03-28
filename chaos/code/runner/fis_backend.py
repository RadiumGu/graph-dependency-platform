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


# fault.type → FIS actionId
FIS_ACTION_MAP = {
    # Lambda
    "fis_lambda_delay":         "aws:lambda:invocation-add-delay",
    "fis_lambda_error":         "aws:lambda:invocation-error",
    "fis_lambda_http_response": "aws:lambda:invocation-http-integration-response",
    # RDS/Aurora
    "fis_rds_failover":         "aws:rds:failover-db-cluster",
    "fis_rds_reboot":           "aws:rds:reboot-db-instances",
    # EKS 节点
    "fis_eks_terminate_node":   "aws:eks:terminate-nodegroup-instances",
    # EC2
    "fis_ec2_stop":             "aws:ec2:stop-instances",
    "fis_ec2_terminate":        "aws:ec2:terminate-instances",
    # EBS
    "fis_ebs_pause_io":         "aws:ebs:pause-volume-io",
    "fis_ebs_io_latency":       "aws:ebs:volume-io-latency",
    # 网络
    "fis_network_disrupt":      "aws:network:disrupt-connectivity",
    "fis_vpc_endpoint_disrupt": "aws:network:disrupt-vpc-endpoint",
    # API 注入
    "fis_api_internal_error":   "aws:fis:inject-api-internal-error",
    "fis_api_throttle":         "aws:fis:inject-api-throttle-error",
    "fis_api_unavailable":      "aws:fis:inject-api-unavailable-error",
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

        # 构建 action
        action_params = {"duration": _to_iso_duration(ft.duration)}
        if "percentage" in extra:
            action_params["percentage"] = str(extra["percentage"])
        if "delay_ms" in extra:
            action_params["delayMilliseconds"] = str(extra["delay_ms"])

        action = {
            "actionId": action_id,
            "parameters": action_params,
            "targets": {"target-0": "target-0"},
        }

        # 构建 target
        target = self._build_target(ft.type, extra)

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
            targets={"target-0": target},
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
        elif fault_type.startswith("fis_rds"):
            return {
                "resourceType": "aws:rds:cluster",
                "resourceArns": [extra["cluster_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type == "fis_eks_terminate_node":
            return {
                "resourceType": "aws:eks:nodegroup",
                "resourceArns": [extra["nodegroup_arn"]],
                "selectionMode": extra.get("selection_mode", "COUNT(1)"),
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
                "resourceType": "aws:ebs:volume",
                "resourceArns": arns,
                "selectionMode": "ALL",
            }
        elif fault_type.startswith("fis_network") or fault_type.startswith("fis_vpc"):
            return {
                "resourceType": "aws:ec2:subnet",
                "resourceArns": [extra["subnet_arn"]],
                "selectionMode": "ALL",
            }
        elif fault_type.startswith("fis_api"):
            # API injection targets an IAM role
            return {
                "resourceType": "aws:iam:role",
                "resourceArns": [extra["role_arn"]],
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

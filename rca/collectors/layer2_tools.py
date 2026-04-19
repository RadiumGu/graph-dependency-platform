"""
layer2_tools.py — 6 个 @tool 函数，包装 Layer2 Prober 的 AWS API 查询。

每个 tool 执行具体的 AWS API 调用（boto3），返回结构化 JSON 字符串。
Strands Agent（Orchestrator 或独立 Prober）调用这些 tool 来获取观测数据。

硬规则（§6.2）：@tool 函数体内如果调用其他 engine，必须强制 direct。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import boto3
from strands import tool

logger = logging.getLogger(__name__)


def _get_region() -> str:
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("REGION") or "ap-northeast-1"


# ─────────────────────────────────────────────────────────
# Tool 1: CloudWatch Metrics (SQS + DynamoDB)
# ─────────────────────────────────────────────────────────

@tool
def probe_cloudwatch(affected_service: str) -> str:
    """探查 CloudWatch Metrics 异常：SQS 队列积压/DLQ、DynamoDB 限流/系统错误。

    返回 JSON：包含 SQS 和 DynamoDB 的异常列表。
    """
    region = _get_region()
    findings = {"sqs": [], "dynamodb": []}

    # --- SQS ---
    try:
        sqs = boto3.client("sqs", region_name=region)
        SERVICE_QUEUE_MAP = {
            "petsite": ["sqspetadoption", "petadoption"],
            "petadoption": ["sqspetadoption", "petadoption"],
            "payforadoption": ["sqspetadoption"],
        }
        patterns = SERVICE_QUEUE_MAP.get(affected_service, [affected_service])
        resp = sqs.list_queues(QueueNamePrefix="")
        all_urls = resp.get("QueueUrls", [])
        queue_urls = list({u for u in all_urls for p in patterns if p.lower() in u.lower()})

        for url in queue_urls[:10]:
            qname = url.split("/")[-1]
            attrs = sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible",
                                "ApproximateNumberOfMessagesDelayed"],
            )["Attributes"]
            visible = int(attrs.get("ApproximateNumberOfMessages", 0))
            inflight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
            is_dlq = "dlq" in qname.lower() or "dead" in qname.lower()
            if (is_dlq and visible > 0) or (not is_dlq and visible > 1000):
                findings["sqs"].append({
                    "queue": qname, "visible": visible, "inflight": inflight,
                    "is_dlq": is_dlq,
                })
    except Exception as e:
        findings["sqs_error"] = str(e)

    # --- DynamoDB ---
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        dynamodb = boto3.client("dynamodb", region_name=region)
        SERVICE_TABLE_MAP = {
            "petsite": ["petadoption", "ddbpetadoption"],
            "petadoption": ["petadoption", "ddbpetadoption"],
        }
        patterns = SERVICE_TABLE_MAP.get(affected_service, [affected_service])
        tables = []
        for page in dynamodb.get_paginator("list_tables").paginate():
            for t in page["TableNames"]:
                if any(p.lower() in t.lower() for p in patterns):
                    tables.append(t)

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=10)
        for table in tables[:5]:
            dims = [{"Name": "TableName", "Value": table}]
            for metric in ["ReadThrottleEvents", "WriteThrottleEvents", "SystemErrors"]:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/DynamoDB", MetricName=metric,
                    Dimensions=dims, StartTime=start, EndTime=end,
                    Period=300, Statistics=["Sum"],
                )
                total = sum(p["Sum"] for p in resp.get("Datapoints", []))
                if total > 0:
                    findings["dynamodb"].append({
                        "table": table, "metric": metric, "count": int(total),
                    })
    except Exception as e:
        findings["dynamodb_error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# Tool 2: X-Ray / Step Functions
# ─────────────────────────────────────────────────────────

@tool
def probe_xray(affected_service: str) -> str:
    """探查 X-Ray Traces / Step Functions 异常：执行失败、超时、限流。

    返回 JSON：Step Functions 异常指标列表。
    """
    region = _get_region()
    findings = {"stepfunctions": []}

    try:
        sf = boto3.client("stepfunctions", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)
        machines = sf.list_state_machines()["stateMachines"]
        targets = [m for m in machines if "StepFnStateMachine" in m["name"]]

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=15)
        for sm in targets[:5]:
            arn = sm["stateMachineArn"]
            dims = [{"Name": "StateMachineArn", "Value": arn}]
            for metric, threshold in [("ExecutionsFailed", 1), ("ExecutionsTimedOut", 1),
                                      ("ExecutionsAborted", 1), ("ExecutionThrottled", 1)]:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/States", MetricName=metric,
                    Dimensions=dims, StartTime=start, EndTime=end,
                    Period=300, Statistics=["Sum"],
                )
                total = sum(p["Sum"] for p in resp.get("Datapoints", []))
                if total >= threshold:
                    findings["stepfunctions"].append({
                        "state_machine": sm["name"], "metric": metric, "count": int(total),
                    })
    except Exception as e:
        findings["stepfunctions_error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# Tool 3: Neptune Graph
# ─────────────────────────────────────────────────────────

@tool
def probe_neptune(affected_service: str) -> str:
    """探查 Neptune 图谱中的拓扑异常：依赖链、服务状态、最近变更。

    返回 JSON：服务拓扑信息和异常标记。
    """
    findings = {"topology": None, "error": None}
    try:
        from runner.neptune_helpers import query_topology  # type: ignore
        topo = query_topology(affected_service)
        findings["topology"] = topo
    except ImportError:
        # Fallback: 直接查 Neptune
        try:
            from neptune.neptune_queries import NeptuneGraphManager  # type: ignore
            gm = NeptuneGraphManager()
            svc = gm.get_service(affected_service)
            if svc:
                findings["topology"] = {
                    "service": affected_service,
                    "tier": svc.get("tier", "unknown"),
                    "deps": svc.get("dependencies", []),
                }
        except Exception as e2:
            findings["error"] = str(e2)
    except Exception as e:
        findings["error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# Tool 4: CloudWatch Logs (Lambda errors)
# ─────────────────────────────────────────────────────────

@tool
def probe_logs(affected_service: str) -> str:
    """探查 CloudWatch Logs 异常：Lambda 错误率、限流、接近超时。

    返回 JSON：Lambda 函数异常指标列表。
    """
    region = _get_region()
    findings = {"lambda": []}

    try:
        lam = boto3.client("lambda", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)
        SERVICE_FUNCTION_MAP = {
            "petsite": ["petsite", "statusupdater", "StepFn"],
            "petadoption": ["statusupdater", "StepFn", "stepread", "stepprice"],
            "payforadoption": ["StepFn", "stepprice"],
        }
        patterns = SERVICE_FUNCTION_MAP.get(affected_service, [affected_service])
        functions = []
        for page in lam.get_paginator("list_functions").paginate():
            for fn in page["Functions"]:
                name = fn["FunctionName"]
                if any(p.lower() in name.lower() for p in patterns):
                    functions.append(name)

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=10)
        for fn_name in functions[:10]:
            dims = [{"Name": "FunctionName", "Value": fn_name}]
            for metric, threshold in [("Errors", 1), ("Throttles", 1)]:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/Lambda", MetricName=metric,
                    Dimensions=dims, StartTime=start, EndTime=end,
                    Period=300, Statistics=["Sum"],
                )
                total = sum(p["Sum"] for p in resp.get("Datapoints", []))
                if total >= threshold:
                    findings["lambda"].append({
                        "function": fn_name, "metric": metric, "count": int(total),
                    })

            # Duration check (near timeout)
            try:
                fn_config = lam.get_function_configuration(FunctionName=fn_name)
                timeout_ms = fn_config.get("Timeout", 30) * 1000
                resp = cw.get_metric_statistics(
                    Namespace="AWS/Lambda", MetricName="Duration",
                    Dimensions=dims, StartTime=start, EndTime=end,
                    Period=300, Statistics=["Maximum"],
                )
                pts = resp.get("Datapoints", [])
                if pts:
                    max_dur = max(p["Maximum"] for p in pts)
                    if max_dur > timeout_ms * 0.9:
                        findings["lambda"].append({
                            "function": fn_name, "metric": "Duration",
                            "value": f"{max_dur:.0f}ms (timeout={timeout_ms}ms)",
                        })
            except Exception:
                pass
    except Exception as e:
        findings["lambda_error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# Tool 5: Deployment (ECS/EKS/EC2 ASG)
# ─────────────────────────────────────────────────────────

@tool
def probe_deployment(affected_service: str, neptune_infra_fault: bool = True) -> str:
    """探查部署状态异常：EKS 节点非 Running、ASG 实例终止。

    仅在 Neptune 图层未找到基础设施故障时触发（neptune_infra_fault=False）。
    返回 JSON：非运行态节点列表。
    """
    region = _get_region()
    findings = {"unhealthy_nodes": []}

    if neptune_infra_fault:
        findings["skipped"] = "Neptune already found infra fault"
        return json.dumps(findings)

    try:
        import re
        ec2 = boto3.client("ec2", region_name=region)
        cluster = os.environ.get("EKS_CLUSTER_NAME", "PetSite")
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:eks:cluster-name", "Values": [cluster]},
            {"Name": "instance-state-name",
             "Values": ["stopped", "stopping", "shutting-down", "terminated"]},
        ])

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                reason = inst.get("StateTransitionReason", "")
                ts_match = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", reason)
                if ts_match:
                    try:
                        t = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        if t < cutoff:
                            continue
                    except Exception:
                        pass
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                findings["unhealthy_nodes"].append({
                    "id": inst["InstanceId"],
                    "name": tags.get("Name", inst["InstanceId"]),
                    "state": inst["State"]["Name"],
                    "az": inst.get("Placement", {}).get("AvailabilityZone", ""),
                })
    except Exception as e:
        findings["error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# Tool 6: Network (ALB)
# ─────────────────────────────────────────────────────────

@tool
def probe_network(affected_service: str) -> str:
    """探查网络层异常：ALB 5xx 飙升、高延迟、不健康目标、连接拒绝。

    返回 JSON：ALB 异常指标和不健康目标列表。
    """
    region = _get_region()
    findings = {"alb": [], "unhealthy_targets": []}

    try:
        elb = boto3.client("elbv2", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)

        lbs = elb.describe_load_balancers()["LoadBalancers"]
        albs = [lb for lb in lbs if "Servic-PetSi" in lb["LoadBalancerName"]]
        if not albs:
            findings["note"] = "No PetSite ALB found"
            return json.dumps(findings)

        lb = albs[0]
        lb_dim = "/".join(lb["LoadBalancerArn"].split("/")[-3:])
        dims = [{"Name": "LoadBalancer", "Value": lb_dim}]

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=10)
        metrics = {
            "HTTPCode_ELB_5XX_Count": ("Sum", 5),
            "HTTPCode_ELB_4XX_Count": ("Sum", 50),
            "TargetResponseTime": ("Average", 3.0),
            "RejectedConnectionCount": ("Sum", 1),
        }

        for metric, (stat, threshold) in metrics.items():
            resp = cw.get_metric_statistics(
                Namespace="AWS/ApplicationELB", MetricName=metric,
                Dimensions=dims, StartTime=start, EndTime=end,
                Period=300, Statistics=[stat],
            )
            pts = resp.get("Datapoints", [])
            if pts:
                value = pts[-1].get(stat, 0)
                if value > threshold:
                    findings["alb"].append({"metric": metric, "value": round(value, 3)})

        # Unhealthy targets
        tgs = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"])["TargetGroups"]
        for tg in tgs[:5]:
            health = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
            unhealthy = [t for t in health["TargetHealthDescriptions"]
                         if t["TargetHealth"]["State"] != "healthy"]
            if unhealthy:
                findings["unhealthy_targets"].append({
                    "target_group": tg["TargetGroupName"],
                    "count": len(unhealthy),
                })
    except Exception as e:
        findings["error"] = str(e)

    return json.dumps(findings, default=str)


# ─────────────────────────────────────────────────────────
# All tools list (for Orchestrator registration)
# ─────────────────────────────────────────────────────────

ALL_LAYER2_TOOLS = [
    probe_cloudwatch,
    probe_xray,
    probe_neptune,
    probe_logs,
    probe_deployment,
    probe_network,
]

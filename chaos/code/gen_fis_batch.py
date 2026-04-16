#!/usr/bin/env python3
"""
gen_fis_batch.py — 批量生成 FIS 实验 YAML 文件

从预定义的服务 × 故障类型矩阵，批量生成标准格式的 FIS 实验模板，
输出到 experiments/fis/ 对应子目录。

用法：
  # 生成全部
  python3 gen_fis_batch.py

  # 只生成指定类别
  python3 gen_fis_batch.py --category eks-pod
  python3 gen_fis_batch.py --category ec2
  python3 gen_fis_batch.py --category lambda
  python3 gen_fis_batch.py --category rds
  python3 gen_fis_batch.py --category az-scenarios
  python3 gen_fis_batch.py --category api-injection

  # 只生成指定服务
  python3 gen_fis_batch.py --service petsite

  # Dry-run（只打印不写文件）
  python3 gen_fis_batch.py --dry-run

  # 强制覆盖已存在文件
  python3 gen_fis_batch.py --overwrite
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from textwrap import dedent

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from profiles.profile_loader import EnvironmentProfile
from shared.service_registry import ServiceRegistry

_profile = EnvironmentProfile()
_registry = ServiceRegistry(_profile.get("services", {}))
_K8S_NAMESPACE = _profile.get("chaos.default_namespace", _profile.k8s_namespace)

# ─── 常量 ────────────────────────────────────────────────────────────────────

TODAY = datetime.now().strftime("%Y-%m-%d")
BASE_DIR = Path(__file__).parent
EXPERIMENTS_FIS = BASE_DIR / "experiments" / "fis"

# 服务 → K8s label selector 映射（从 profile 派生）
SERVICE_SELECTOR = {}
for _name, _cfg in _profile.get("services", {}).items():
    k8s_label = _cfg.get("k8s_label", _cfg.get("k8s_deployment", _name))
    SERVICE_SELECTOR[_name] = f"app={k8s_label}"

# 服务 → Tier（从 profile 派生）
SERVICE_TIER = {_name: _cfg.get("tier", "Tier2")
                for _name, _cfg in _profile.get("services", {}).items()}

# 服务 → Lambda 函数名关键词（仅 Lambda 类）
SERVICE_LAMBDA = {
    "petstatusupdater":  "petstatusupdater",
    "petadoptionshistory": "rca-interaction",   # Step Functions 内部步骤，用 rca-interaction 代替
}

# ─── EKS Pod 类生成器 ─────────────────────────────────────────────────────────

EKS_POD_FAULTS = [
    {
        "type": "fis_eks_pod_delete",
        "suffix": "delete",
        "description_tmpl": "Delete {service} pods via FIS native pod action, compare with ChaosMesh pod-kill behavior",
        "comment_tmpl": "# FIS Pod Action：Pod Delete（{service}）\n# 与 ChaosMesh pod-kill 对照：FIS 原生 Pod Delete，通过 K8s API 删除\n# 验证 FIS agent 路径 vs ChaosMesh 路径的行为差异",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "3m",
        "after_sr_threshold": ">= 90%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "30s",
        "max_duration": "10m",
    },
    {
        "type": "fis_eks_pod_network_latency",
        "suffix": "latency",
        "description_tmpl": "Inject 500ms network latency into {service} pods via FIS, test upstream timeout handling",
        "comment_tmpl": "# FIS Pod Action：Network Latency（{service}）\n# 注入 500ms 延迟，验证上游超时和重试",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "delay_ms": 500,
            "jitter_ms": 100,
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 90%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "30s",
        "max_duration": "10m",
    },
    {
        "type": "fis_eks_pod_network_packet_loss",
        "suffix": "loss",
        "description_tmpl": "Inject 30% packet loss into {service} pods via FIS, verify retry logic and error rates",
        "comment_tmpl": "# FIS Pod Action：Network Packet Loss（{service}）\n# 30% 丢包，验证重试逻辑",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "loss_percent": 30,
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 90%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "30s",
        "max_duration": "10m",
    },
    {
        "type": "fis_eks_pod_cpu_stress",
        "suffix": "cpu-stress",
        "description_tmpl": "Inject CPU stress into {service} pods, verify throttling & latency",
        "comment_tmpl": "# FIS Pod Action：CPU Stress（{service}）\n# CPU 压力测试，验证节流和延迟",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "workers": 2,
            "percent": 80,
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 90%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "30s",
        "max_duration": "15m",
    },
    {
        "type": "fis_eks_pod_memory_stress",
        "suffix": "memory-stress",
        "description_tmpl": "Inject memory pressure into {service} pods via FIS, verify OOM handling",
        "comment_tmpl": "# FIS Pod Action：Memory Stress（{service}）\n# 内存压力，验证 OOM 行为",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "workers": 1,
            "percent": 80,
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 90%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "30s",
        "max_duration": "15m",
    },
    {
        "type": "fis_eks_pod_io_stress",
        "suffix": "io-stress",
        "description_tmpl": "Inject IO stress on {service} pods via FIS, verify write path degradation",
        "comment_tmpl": "# FIS Pod Action：IO Stress（{service}）\n# 磁盘 IO 压力，验证写路径降级",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "workers": 1,
            "io_percent": 80,
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 85%",
        "stop_sr_threshold": "< 50%",
        "stop_window": "60s",
        "max_duration": "10m",
    },
    {
        "type": "fis_eks_pod_network_blackhole",
        "suffix": "blackhole",
        "description_tmpl": "Blackhole port 8080 traffic on {service} pods, verify upstream timeout and circuit breaker",
        "comment_tmpl": "# FIS Pod Action：Network Blackhole Port（{service} 8080）\n# 完全丢弃指定端口的流量（比丢包更极端 — 100% 黑洞）\n# 验证：上游服务的超时处理 + 熔断器行为",
        "extra_params_tmpl": lambda svc: {
            "cluster_name": "PetSite",
            "namespace": _K8S_NAMESPACE,
            "selector_type": "labelSelector",
            "selector_value": SERVICE_SELECTOR[svc],
            "traffic_type": "ingress",
            "port": 8080,
            "protocol": "tcp",
            "kubernetes_service_account": "fis-service-account",
        },
        "duration": "5m",
        "after_sr_threshold": ">= 80%",
        "stop_sr_threshold": "< 30%",
        "stop_window": "60s",
        "max_duration": "10m",
    },
]

# EKS Pod 类适用的服务（Tier0 + 部分 Tier1）
EKS_POD_SERVICES = ["petsite", "petsearch", "pethistory", "payforadoption", "petlistadoptions", "petstatusupdater"]

# 并非所有故障类型对所有服务都生成（blackhole/io 只针对部分）
EKS_POD_FAULT_SERVICE_FILTER = {
    "blackhole":   ["petsite"],                           # 只对 Tier0 入口
    "io-stress":   ["payforadoption", "pethistory"],      # 写路径服务
    "latency":     ["petsite", "pethistory", "payforadoption"],
    "loss":        ["petsite", "petsearch", "pethistory"],
    "delete":      ["petsite", "petsearch", "pethistory", "payforadoption"],
    "cpu-stress":  ["petsite", "pethistory", "petsearch", "payforadoption"],
    "memory-stress": ["petsite", "pethistory", "petsite", "payforadoption"],
}

# ─── EC2 类生成器 ─────────────────────────────────────────────────────────────

EC2_SPECS = [
    {
        "name": "fis-ec2-stop-node-1a",
        "description": "Stop EKS worker node in AZ 1a, verify cross-AZ failover and Pod rescheduling",
        "fault_type": "fis_ec2_stop",
        "az": "ap-northeast-1a",
        "comment": "# FIS EC2 — 自动生成 {date}",
    },
    {
        "name": "fis-ec2-stop-node-1c",
        "description": "Stop EKS worker node in AZ 1c, verify cross-AZ failover and Pod rescheduling",
        "fault_type": "fis_ec2_stop",
        "az": "ap-northeast-1c",
        "comment": "# FIS EC2 — 自动生成 {date}",
    },
    {
        "name": "fis-ec2-reboot-node-1a",
        "description": "Reboot EKS worker node in AZ 1a, verify graceful drain and Pod rescheduling",
        "fault_type": "fis_ec2_reboot",
        "az": "ap-northeast-1a",
        "comment": "# FIS EC2 — 自动生成 {date}",
    },
    {
        "name": "fis-ec2-reboot-node-1c",
        "description": "Reboot EKS worker node in AZ 1c, verify graceful drain and Pod rescheduling",
        "fault_type": "fis_ec2_reboot",
        "az": "ap-northeast-1c",
        "comment": "# FIS EC2 — 自动生成 {date}",
    },
]

# ─── Lambda 类生成器 ──────────────────────────────────────────────────────────

LAMBDA_SPECS = [
    {
        "name": "fis-lambda-delay-petstatusupdater",
        "service": "petstatusupdater",
        "description": "Inject 2s delay into petstatusupdater Lambda, verify event pipeline resilience",
        "fault_type": "fis_lambda_delay",
        "tier": "Tier1",
        "extra": {"service_name": "petstatusupdater", "resource_type": "lambda:function", "delay_ms": 2000, "percentage": 100},
        "comment": "# FIS Lambda — 自动生成 {date}",
        "alarm": "chaos-petstatusupdater-sr-critical",
    },
    {
        "name": "fis-lambda-delay-petadoptionshistory",
        "service": "petadoptionshistory",
        "description": "Inject 2s invocation delay into petsite-rca-interaction Lambda (proxy for adoptions history path), verify history query resilience",
        "fault_type": "fis_lambda_delay",
        "tier": "Tier1",
        "extra": {"service_name": "rca-interaction", "resource_type": "lambda:function", "delay_ms": 2000, "percentage": 100},
        "comment": "# FIS Lambda — 自动生成 {date}\n# ⚠️ petadoptionshistory 是 Step Functions 内的步骤，不是独立 Lambda\n# 使用 petsite-rca-interaction 或 petsite-rca-engine 作为替代验证目标",
        "alarm": "chaos-petstatusupdater-sr-critical",
        "graph_feedback": True,
    },
    {
        "name": "fis-lambda-http-response-petstatusupdater",
        "service": "petstatusupdater",
        "description": "Inject HTTP 500 response into petstatusupdater Lambda, verify error handling and retry",
        "fault_type": "fis_lambda_http_response",
        "tier": "Tier1",
        "extra": {"service_name": "petstatusupdater", "resource_type": "lambda:function", "status_code": 500, "percentage": 50},
        "comment": "# FIS Lambda — 自动生成 {date}",
        "alarm": "chaos-petstatusupdater-sr-critical",
    },
    {
        "name": "fis-lambda-error-petadoptionshistory",
        "service": "petadoptionshistory",
        "description": "Force Lambda error responses for petsite-rca-engine, verify error propagation",
        "fault_type": "fis_lambda_error",
        "tier": "Tier1",
        "extra": {"service_name": "rca-engine", "resource_type": "lambda:function", "percentage": 30},
        "comment": "# FIS Lambda — 自动生成 {date}",
        "alarm": "chaos-petstatusupdater-sr-critical",
    },
]

# ─── RDS 类生成器 ─────────────────────────────────────────────────────────────

RDS_SPECS = [
    {
        "name": "fis-aurora-failover",
        "service": "petlistadoptions",
        "description": "Trigger Aurora cluster failover, verify application reconnect and connection pool recovery",
        "fault_type": "fis_rds_failover",
        "tier": "Tier1",
        "extra": {"service_name": "petlistadoptions", "resource_type": "rds:cluster"},
        "comment": "# FIS RDS — 自动生成 {date}\n# petlistadoptions 和 payforadoption 共用同一个 Aurora 集群: serviceseks2-databaseb269d8bb",
        "alarm": "chaos-rds-sr-critical",
        "graph_feedback": True,
        "after_latency": True,
    },
    {
        "name": "fis-aurora-reboot",
        "service": "petlistadoptions",
        "description": "Reboot Aurora DB instance, verify connection pool and query retry",
        "fault_type": "fis_rds_reboot",
        "tier": "Tier1",
        "extra": {"service_name": "petlistadoptions", "resource_type": "rds:cluster"},
        "comment": "# FIS RDS — 自动生成 {date}",
        "alarm": "chaos-rds-sr-critical",
        "graph_feedback": True,
        "after_latency": True,
    },
    {
        "name": "fis-aurora-reboot-petlistadoptions",
        "service": "petlistadoptions",
        "description": "Reboot Aurora DB instance (serviceseks2 cluster), verify petlistadoptions reconnect & connection pool recovery",
        "fault_type": "fis_rds_reboot",
        "tier": "Tier1",
        "extra": {
            "service_name": "petlistadoptions",
            "resource_type": "rds:cluster",
            "# TargetResolver 走 rds:fallback 路径，会匹配 serviceseks2-databaseb269d8bb": None,
        },
        "comment": "# FIS RDS — 自动生成 {date}\n# petlistadoptions 和 payforadoption 共用同一个 Aurora 集群: serviceseks2-databaseb269d8bb",
        "alarm": "chaos-rds-sr-critical",
        "graph_feedback": True,
        "after_latency": True,
        "after_sr_threshold": ">= 85%",
        "stop_sr_threshold": "< 30%",
    },
]

# ─── AZ Scenario 类生成器 ─────────────────────────────────────────────────────

AZ_SCENARIO_SPECS = [
    {
        "name": "fis-scenario-az-power-interruption-1a",
        "az": "ap-northeast-1a",
        "fault_type": "fis_scenario_az_power_interruption",
        "description": "AZ 1a power interruption scenario: EC2 stop + EBS pause IO + RDS failover, verify multi-AZ HA",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-scenario-az-power-interruption-1c",
        "az": "ap-northeast-1c",
        "fault_type": "fis_scenario_az_power_interruption",
        "description": "AZ 1c power interruption scenario: EC2 stop + EBS pause IO + RDS failover, verify multi-AZ HA",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-az-app-slowdown-1a",
        "az": "ap-northeast-1a",
        "fault_type": "fis_scenario_az_app_slowdown",
        "description": "AZ 1a app slowdown scenario: network disrupt + Lambda delay, verify cross-AZ performance",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-az-app-slowdown-1c",
        "az": "ap-northeast-1c",
        "fault_type": "fis_scenario_az_app_slowdown",
        "description": "AZ 1c app slowdown scenario: network disrupt + Lambda delay, verify cross-AZ performance",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-cross-az-severe-degradation-1a",
        "az": "ap-northeast-1a",
        "fault_type": "fis_scenario_cross_az_traffic",
        "description": "Severe AZ 1a degradation: NACL-level traffic disruption, verify cross-AZ routing",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-cross-az-traffic-slowdown-1a",
        "az": "ap-northeast-1a",
        "fault_type": "fis_scenario_cross_az_traffic",
        "description": "AZ 1a cross-AZ traffic slowdown scenario",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
    {
        "name": "fis-cross-az-traffic-slowdown-1c",
        "az": "ap-northeast-1c",
        "fault_type": "fis_scenario_cross_az_traffic",
        "description": "AZ 1c cross-AZ traffic slowdown scenario",
        "comment": "# FIS AZ Scenario — 自动生成 {date}",
    },
]

# ─── API Injection 类生成器 ───────────────────────────────────────────────────

API_INJECTION_SPECS = [
    {
        "name": "fis-api-internal-error-eks",
        "service": "eks-control-plane",
        "description": "Inject AWS API 500 errors for EKS node role, verify SDK retry and error handling",
        "fault_type": "fis_api_internal_error",
        "comment": "# FIS 场景：AWS API Internal Error（500）注入\n# 对 EKS node role 的 AWS API 调用注入 500 错误\n# 验证：SDK 重试机制、exponential backoff、circuit breaker 行为",
        "extra": {
            "resource_type": "iam:role",
            "role_arn": "arn:aws:iam::926093770964:role/ServicesEks2-petsiteNodegroupworkers1aNodeGroupRole-YXpfPBKFoRCE",
            "percentage": 30,
            "service": "ec2",
            "operations": "DescribeInstances",
        },
        "rca_enabled": False,
        "graph_feedback": False,
    },
    {
        "name": "fis-api-throttle-eks",
        "service": "eks-control-plane",
        "description": "Inject AWS API throttling (429) for EKS-related calls, verify backoff behavior",
        "fault_type": "fis_api_throttle",
        "comment": "# FIS 场景：AWS API 限流注入\n# 对 EKS 相关 API 调用注入 429 限流\n# 验证：指数退避、熔断器响应",
        "extra": {
            "resource_type": "iam:role",
            "role_arn": "arn:aws:iam::926093770964:role/ServicesEks2-petsiteNodegroupworkers1aNodeGroupRole-YXpfPBKFoRCE",
            "percentage": 50,
            "service": "ec2",
            "operations": "DescribeInstances",
        },
        "rca_enabled": False,
        "graph_feedback": False,
    },
    {
        "name": "fis-api-unavailable-rds",
        "service": "rds-control-plane",
        "description": "Inject RDS API unavailability, verify connection pool and retry on control-plane outage",
        "fault_type": "fis_api_internal_error",
        "comment": "# FIS 场景：RDS API 不可用注入\n# 对 RDS 管控 API 注入错误\n# 验证连接池在控制面故障时的行为",
        "extra": {
            "resource_type": "iam:role",
            "role_arn": "arn:aws:iam::926093770964:role/petsite-rca-engine-role",
            "percentage": 30,
            "service": "rds",
            "operations": "DescribeDBInstances",
        },
        "rca_enabled": False,
        "graph_feedback": False,
    },
]

# ─── YAML 渲染函数 ────────────────────────────────────────────────────────────

def _indent_dict(d: dict, indent: int = 4) -> str:
    """把 dict 渲染成 YAML extra_params 块，过滤掉 None value（注释键）"""
    lines = []
    pad = " " * indent
    for k, v in d.items():
        if v is None:
            # 注释行
            lines.append(f"{pad}{k}")
        elif isinstance(v, str):
            lines.append(f'{pad}{k}: "{v}"')
        elif isinstance(v, bool):
            lines.append(f"{pad}{k}: {str(v).lower()}")
        else:
            lines.append(f"{pad}{k}: {v}")
    return "\n".join(lines)


def render_eks_pod(service: str, fault: dict) -> str:
    tier = SERVICE_TIER.get(service, "Tier0")
    extra = fault["extra_params_tmpl"](service)
    alarm_line = f'\n    cloudwatch_alarm_arn: "arn:aws:cloudwatch:${{REGION}}:${{ACCOUNT_ID}}:alarm:chaos-eks-sr-critical"'

    return f"""{fault["comment_tmpl"].format(service=service)}

name: fis-eks-pod-{fault["suffix"]}-{service}
description: "{fault["description_tmpl"].format(service=service)}"
backend: fis

target:
  service: {service}
  namespace: eks-pod
  tier: {tier}

fault:
  type: {fault["type"]}
  mode: all
  value: "100"
  duration: "{fault["duration"]}"
  extra_params:
{_indent_dict(extra)}

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 95%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: "{fault["after_sr_threshold"]}"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "{fault["stop_sr_threshold"]}"
    window: "{fault["stop_window"]}"
    action: abort

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: {service}

graph_feedback:
  enabled: false

options:
  max_duration: "{fault["max_duration"]}"
  save_to_bedrock_kb: false
"""


def render_ec2(spec: dict) -> str:
    comment = spec["comment"].format(date=TODAY)
    az_short = spec["az"].replace("ap-northeast-1", "")  # → 'a' or 'c'
    return f"""{comment}
name: {spec["name"]}
description: "{spec["description"]}"
backend: fis

target:
  service: eks-nodegroup
  namespace: ec2
  tier: Tier0

fault:
  type: {spec["fault_type"]}
  mode: all
  value: "100"
  duration: "5m"
  extra_params:
    service_name: "PetSite"
    resource_type: "ec2:instance"
    availability_zone: "{spec["az"]}"
    selection_mode: "COUNT(1)"

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 95%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 90%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 50%"
    window: "60s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:${{REGION}}:${{ACCOUNT_ID}}:alarm:chaos-eks-sr-critical"

rca:
  enabled: true
  trigger_after: "60s"
  expected_root_cause: eks-node

graph_feedback:
  enabled: false

options:
  max_duration: "20m"
  save_to_bedrock_kb: false
"""


def render_lambda(spec: dict) -> str:
    comment = spec["comment"].format(date=TODAY)
    alarm = spec.get("alarm", "chaos-petstatusupdater-sr-critical")
    graph_fb = spec.get("graph_feedback", False)
    graph_block = (
        "graph_feedback:\n  enabled: true\n  edges:\n    - DependsOn\n"
        if graph_fb else
        "graph_feedback:\n  enabled: false\n"
    )
    extra = {k: v for k, v in spec["extra"].items()}
    return f"""{comment}
name: {spec["name"]}
description: "{spec["description"]}"
backend: fis

target:
  service: {spec["service"]}
  namespace: lambda
  tier: {spec["tier"]}

fault:
  type: {spec["fault_type"]}
  mode: all
  value: "100"
  duration: "5m"
  extra_params:
{_indent_dict(extra)}

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 85%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 40%"
    window: "30s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:${{REGION}}:${{ACCOUNT_ID}}:alarm:{alarm}"

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: {spec["service"]}

{graph_block}
options:
  max_duration: "10m"
  save_to_bedrock_kb: false
"""


def render_rds(spec: dict) -> str:
    comment = spec["comment"].format(date=TODAY)
    alarm = spec.get("alarm", "chaos-rds-sr-critical")
    graph_fb = spec.get("graph_feedback", False)
    graph_block = (
        "graph_feedback:\n  enabled: true\n  edges:\n    - DependsOn\n"
        if graph_fb else
        "graph_feedback:\n  enabled: false\n"
    )
    after_sr = spec.get("after_sr_threshold", ">= 85%")
    stop_sr = spec.get("stop_sr_threshold", "< 30%")
    # Filter out None-value comment entries
    extra = {k: v for k, v in spec["extra"].items() if k != "# TargetResolver 走 rds:fallback 路径，会匹配 serviceseks2-databaseb269d8bb"}
    # If there's a comment key, add it inline
    extra_lines = []
    for k, v in spec["extra"].items():
        if k.startswith("#"):
            extra_lines.append(f"        {k}")
        elif v is None:
            continue
        elif isinstance(v, str):
            extra_lines.append(f'        {k}: "{v}"')
        else:
            extra_lines.append(f"        {k}: {v}")
    extra_block = "\n".join(extra_lines)

    after_block = f"""steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: "{after_sr}"
      window: "5m"
    - metric: latency_p99
      threshold: "< 8000ms"
      window: "5m"
""" if spec.get("after_latency") else f"""steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: "{after_sr}"
      window: "5m"
"""

    return f"""{comment}
name: {spec["name"]}
description: "{spec["description"]}"
backend: fis

target:
  service: {spec["service"]}
  namespace: rds
  tier: {spec["tier"]}

fault:
  type: {spec["fault_type"]}
  mode: all
  value: "100"
  duration: "5m"
  extra_params:
{extra_block}

{after_block}
stop_conditions:
  - metric: success_rate
    threshold: "{stop_sr}"
    window: "60s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:${{REGION}}:${{ACCOUNT_ID}}:alarm:{alarm}"

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: {spec["service"]}

{graph_block}
options:
  max_duration: "15m"
  save_to_bedrock_kb: false
"""


def render_az_scenario(spec: dict) -> str:
    comment = spec["comment"].format(date=TODAY)
    return f"""{comment}
name: {spec["name"]}
description: "{spec["description"]}"
backend: fis

target:
  service: eks-nodegroup
  namespace: az
  tier: Tier0

fault:
  type: {spec["fault_type"]}
  mode: all
  value: "100"
  duration: "5m"
  extra_params:
    availability_zone: "{spec["az"]}"
    service_name: "PetSite"
    rds_cluster_arn: ""   # TargetResolver 运行时填充

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 95%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 80%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 30%"
    window: "60s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:${{REGION}}:${{ACCOUNT_ID}}:alarm:chaos-eks-sr-critical"

rca:
  enabled: true
  trigger_after: "60s"
  expected_root_cause: eks-node

graph_feedback:
  enabled: false

options:
  max_duration: "20m"
  save_to_bedrock_kb: false
"""


def render_api_injection(spec: dict) -> str:
    comment = spec["comment"]
    rca_enabled = spec.get("rca_enabled", False)
    graph_fb = spec.get("graph_feedback", False)
    rca_block = "rca:\n  enabled: false\n" if not rca_enabled else "rca:\n  enabled: true\n  trigger_after: \"30s\"\n"
    graph_block = "graph_feedback:\n  enabled: false\n"
    return f"""{comment}

name: {spec["name"]}
description: "{spec["description"]}"
backend: fis

target:
  service: {spec["service"]}
  namespace: api-injection
  tier: Tier1

fault:
  type: {spec["fault_type"]}
  mode: all
  value: "100"
  duration: "5m"
  extra_params:
{_indent_dict(spec["extra"])}

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 95%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 90%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 50%"
    window: "60s"
    action: abort

{rca_block}
{graph_block}
options:
  max_duration: "10m"
  save_to_bedrock_kb: false
"""


# ─── 主逻辑 ───────────────────────────────────────────────────────────────────

def write_file(path: Path, content: str, dry_run: bool, overwrite: bool) -> bool:
    """写文件，返回是否实际写入"""
    if path.exists() and not overwrite:
        print(f"  ⏭️  已存在，跳过: {path.name}")
        return False
    if dry_run:
        print(f"  [dry-run] 会写入: {path}")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  ✅ {path.name}")
    return True


def gen_eks_pod(dry_run: bool, overwrite: bool, service_filter: str | None):
    out_dir = EXPERIMENTS_FIS / "eks-pod"
    print(f"\n📦 eks-pod → {out_dir}")
    count = 0
    for fault in EKS_POD_FAULTS:
        suffix = fault["suffix"]
        svc_list = EKS_POD_FAULT_SERVICE_FILTER.get(suffix, EKS_POD_SERVICES)
        for svc in svc_list:
            if service_filter and svc != service_filter:
                continue
            name = f"fis-eks-pod-{suffix}-{svc}.yaml"
            content = render_eks_pod(svc, fault)
            if write_file(out_dir / name, content, dry_run, overwrite):
                count += 1
    print(f"  → {count} 个文件")


def gen_ec2(dry_run: bool, overwrite: bool):
    out_dir = EXPERIMENTS_FIS / "ec2"
    print(f"\n🖥️  ec2 → {out_dir}")
    count = 0
    for spec in EC2_SPECS:
        name = f"{spec['name']}.yaml"
        content = render_ec2(spec)
        if write_file(out_dir / name, content, dry_run, overwrite):
            count += 1
    print(f"  → {count} 个文件")


def gen_lambda(dry_run: bool, overwrite: bool):
    out_dir = EXPERIMENTS_FIS / "lambda"
    print(f"\n⚡ lambda → {out_dir}")
    count = 0
    for spec in LAMBDA_SPECS:
        name = f"{spec['name']}.yaml"
        content = render_lambda(spec)
        if write_file(out_dir / name, content, dry_run, overwrite):
            count += 1
    print(f"  → {count} 个文件")


def gen_rds(dry_run: bool, overwrite: bool):
    out_dir = EXPERIMENTS_FIS / "rds"
    print(f"\n🗄️  rds → {out_dir}")
    count = 0
    for spec in RDS_SPECS:
        name = f"{spec['name']}.yaml"
        content = render_rds(spec)
        if write_file(out_dir / name, content, dry_run, overwrite):
            count += 1
    print(f"  → {count} 个文件")


def gen_az_scenarios(dry_run: bool, overwrite: bool):
    out_dir = EXPERIMENTS_FIS / "az-scenarios"
    print(f"\n🌐 az-scenarios → {out_dir}")
    count = 0
    for spec in AZ_SCENARIO_SPECS:
        name = f"{spec['name']}.yaml"
        content = render_az_scenario(spec)
        if write_file(out_dir / name, content, dry_run, overwrite):
            count += 1
    print(f"  → {count} 个文件")


def gen_api_injection(dry_run: bool, overwrite: bool):
    out_dir = EXPERIMENTS_FIS / "api-injection"
    print(f"\n🔌 api-injection → {out_dir}")
    count = 0
    for spec in API_INJECTION_SPECS:
        name = f"{spec['name']}.yaml"
        content = render_api_injection(spec)
        if write_file(out_dir / name, content, dry_run, overwrite):
            count += 1
    print(f"  → {count} 个文件")


def main():
    parser = argparse.ArgumentParser(description="批量生成 FIS 实验 YAML 文件")
    parser.add_argument(
        "--category",
        choices=["eks-pod", "ec2", "lambda", "rds", "az-scenarios", "api-injection"],
        help="只生成指定类别（默认生成全部）",
    )
    parser.add_argument("--service", help="只生成指定服务（仅 eks-pod 类有效）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    parser.add_argument("--overwrite", action="store_true", help="强制覆盖已存在文件")
    args = parser.parse_args()

    mode = "DRY-RUN" if args.dry_run else ("OVERWRITE" if args.overwrite else "SKIP-EXISTING")
    print(f"🚀 gen_fis_batch.py  mode={mode}  date={TODAY}")
    print(f"   output base: {EXPERIMENTS_FIS}")

    cats = [args.category] if args.category else ["eks-pod", "ec2", "lambda", "rds", "az-scenarios", "api-injection"]

    for cat in cats:
        if cat == "eks-pod":
            gen_eks_pod(args.dry_run, args.overwrite, args.service)
        elif cat == "ec2":
            gen_ec2(args.dry_run, args.overwrite)
        elif cat == "lambda":
            gen_lambda(args.dry_run, args.overwrite)
        elif cat == "rds":
            gen_rds(args.dry_run, args.overwrite)
        elif cat == "az-scenarios":
            gen_az_scenarios(args.dry_run, args.overwrite)
        elif cat == "api-injection":
            gen_api_injection(args.dry_run, args.overwrite)

    print("\n✅ 完成")


if __name__ == "__main__":
    main()

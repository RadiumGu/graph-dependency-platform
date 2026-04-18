"""
profiles/schema.py — Profile YAML Schema 校验

使用 Pydantic v2 校验 petsite.yaml 结构，在加载时即发现拼写错误和缺失字段。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class ServiceConfig(BaseModel):
    """单个服务的配置。"""
    tier: str = "Tier2"
    type: Optional[str] = None  # "lambda" etc
    k8s_deployment: Optional[str] = None
    k8s_label: Optional[str] = None
    neptune_name: Optional[str] = None
    lambda_name: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    cloudwatch: Optional[Dict[str, str]] = None
    deepflow_app: Optional[str] = None


class AwsResources(BaseModel):
    """AWS 资源映射。"""
    sqs_queues: Dict[str, List[str]] = Field(default_factory=dict)
    dynamodb_tables: Dict[str, List[str]] = Field(default_factory=dict)
    lambda_functions: Dict[str, List[str]] = Field(default_factory=dict)
    s3_buckets: Dict[str, str] = Field(default_factory=dict)
    primary_region: Optional[str] = None


class ApplicationConfig(BaseModel):
    """应用配置。"""
    domain: str
    health_endpoint: str = "/health"
    health_check_command: Optional[str] = None
    alarm_prefix: Optional[str] = None


class ParameterStoreConfig(BaseModel):
    """SSM Parameter Store 配置。"""
    prefix: str
    keys: Dict[str, str] = Field(default_factory=dict)


class DnsConfig(BaseModel):
    """DNS 配置。"""
    hosted_zone_id: str = "${ROUTE53_ZONE_ID}"
    primary_record: Optional[str] = None
    ttl_normal: int = 300
    ttl_pre_switchover: int = 60


class KubernetesConfig(BaseModel):
    """Kubernetes 配置。"""
    namespace: str = "default"
    cluster_name: Optional[str] = None
    context_source: Optional[str] = None
    context_target: Optional[str] = None
    deployment_map: Dict[str, str] = Field(default_factory=dict)


class NeptuneGuardRules(BaseModel):
    """Neptune 查询安全规则。"""
    forbidden_operations: List[str] = Field(
        default_factory=lambda: ["CREATE", "DELETE", "DETACH", "SET", "MERGE", "REMOVE", "DROP", "CALL"]
    )
    max_hop_depth: int = 6
    default_limit: int = 200


class NeptuneComplexKeywords(BaseModel):
    """复杂问题触发词（切重模型用）。"""
    zh: List[str] = Field(default_factory=list)
    en: List[str] = Field(default_factory=list)


class NeptuneConfig(BaseModel):
    """Neptune 配置。"""
    endpoint: Optional[str] = None
    port: int = 8182
    nl_query_examples: List[Dict[str, str]] = Field(default_factory=list)
    # Wave 1 (2026-04-18): Smart Query 游击提升 - schema 和规则从 profile 读
    graph_schema_text: Optional[str] = None
    common_relations: List[str] = Field(default_factory=list)
    guard_rules: NeptuneGuardRules = Field(default_factory=NeptuneGuardRules)
    complex_keywords: NeptuneComplexKeywords = Field(default_factory=NeptuneComplexKeywords)


class MonitoringConfig(BaseModel):
    """监控配置。"""
    cloudwatch_alarm_prefix: Optional[str] = None
    dashboard_name: Optional[str] = None


class ChaosStopCondition(BaseModel):
    """Chaos 停止条件告警。"""
    service: str
    alarm_name: str
    metric: str
    namespace: str
    threshold: int


class ChaosConfig(BaseModel):
    """Chaos 工程配置。"""
    rca_lambda_name: Optional[str] = None
    fis_stop_condition_prefix: Optional[str] = None
    default_namespace: Optional[str] = None
    stop_condition_alarms: List[ChaosStopCondition] = Field(default_factory=list)


class ProfileMeta(BaseModel):
    """Profile 元信息。"""
    name: str
    description: Optional[str] = None
    version: Optional[str] = None


class EnvironmentProfileSchema(BaseModel):
    """完整 Environment Profile Schema。"""
    profile: ProfileMeta
    services: Dict[str, ServiceConfig] = Field(default_factory=dict)
    aws_resources: Optional[AwsResources] = None
    application: Optional[ApplicationConfig] = None
    parameter_store: Optional[ParameterStoreConfig] = None
    dns: Optional[DnsConfig] = None
    kubernetes: Optional[KubernetesConfig] = None
    neptune: Optional[NeptuneConfig] = None
    monitoring: Optional[MonitoringConfig] = None
    chaos: Optional[ChaosConfig] = None

    model_config = {"extra": "allow"}  # 允许未知字段（向前兼容）

    @model_validator(mode="after")
    def validate_service_names(self) -> "EnvironmentProfileSchema":
        """确保服务的 neptune_name 与 key 一致或已显式设置。"""
        for name, svc in self.services.items():
            if svc.neptune_name and svc.neptune_name != name:
                pass  # 显式映射，OK
            elif not svc.neptune_name:
                svc.neptune_name = name  # 自动填充
        return self


def validate_profile(data: dict) -> EnvironmentProfileSchema:
    """校验 profile 数据并返回结构化对象。

    Args:
        data: 从 YAML 加载的原始字典。

    Returns:
        校验后的 EnvironmentProfileSchema 实例。

    Raises:
        pydantic.ValidationError: 校验失败时抛出，包含详细错误信息。
    """
    return EnvironmentProfileSchema.model_validate(data)

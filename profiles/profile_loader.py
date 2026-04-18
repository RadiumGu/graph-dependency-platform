"""
profiles/profile_loader.py — Environment Profile 加载器

从 YAML 文件加载环境配置，提供便捷属性访问。
所有 planner/ 模块中的硬编码值都从这里读取。
"""

import os
from typing import Any, Optional

import yaml

_DEFAULT_PROFILE = os.path.join(os.path.dirname(__file__), "petsite.yaml")


class EnvironmentProfile:
    """环境 Profile，提供客户环境的元数据。

    所有 planner/ 模块中的硬编码值都从这里读取。
    """

    def __init__(self, profile_path: Optional[str] = None) -> None:
        """从 YAML 文件加载 profile。

        Args:
            profile_path: YAML 文件路径，默认加载 petsite.yaml。
        """
        path = profile_path or _DEFAULT_PROFILE
        with open(path, encoding="utf-8") as f:
            self._data: dict = yaml.safe_load(f)

        # Schema 校验（加载时即发现错误）
        from profiles.schema import validate_profile
        validate_profile(self._data)

    # --- 便捷访问 ---

    @property
    def name(self) -> str:
        """Profile 名称。"""
        return self._data.get("profile", {}).get("name", "unknown")

    @property
    def domain(self) -> str:
        """应用主域名。"""
        return self._data.get("application", {}).get("domain", "")

    @property
    def health_endpoint(self) -> str:
        """健康检查端点路径。"""
        return self._data.get("application", {}).get("health_endpoint", "/health")

    @property
    def health_check_command(self) -> str:
        """渲染后的健康检查命令（已替换 {domain} 和 {health_endpoint}）。"""
        template = self._data.get("application", {}).get(
            "health_check_command",
            "curl -sf https://{domain}{health_endpoint} | jq '.status'",
        )
        return template.strip().format(
            domain=self.domain,
            health_endpoint=self.health_endpoint,
        )

    @property
    def alarm_prefix(self) -> str:
        """CloudWatch 告警前缀。"""
        return self._data.get("monitoring", {}).get(
            "cloudwatch_alarm_prefix",
            self._data.get("application", {}).get("alarm_prefix", "app"),
        )

    @property
    def ssm_dynamodb_region_key(self) -> str:
        """DynamoDB region 切换使用的 SSM 参数路径。"""
        return self._data.get("parameter_store", {}).get("keys", {}).get(
            "dynamodb_region",
            self._data.get("parameter_store", {}).get(
                "dynamodb_region_key", f"/{self.name}/dynamodb-region"
            ),
        )

    @property
    def dns_hosted_zone_id(self) -> str:
        """Route 53 Hosted Zone ID。"""
        return self._data.get("dns", {}).get("hosted_zone_id", "${ZONE_ID}")

    @property
    def dns_primary_record(self) -> str:
        """DNS 主记录名。"""
        return self._data.get("dns", {}).get("primary_record", self.domain)

    @property
    def dns_ttl_normal(self) -> int:
        """正常 DNS TTL（秒）。"""
        return self._data.get("dns", {}).get("ttl_normal", 300)

    @property
    def dns_ttl_pre_switchover(self) -> int:
        """切换前降低的 DNS TTL（秒）。"""
        return self._data.get("dns", {}).get("ttl_pre_switchover", 60)

    @property
    def k8s_namespace(self) -> str:
        """Kubernetes namespace。"""
        return self._data.get("kubernetes", {}).get("namespace", "default")

    def get_deployment_name(self, service_name: str) -> str:
        """获取服务对应的 K8s deployment 名（支持非 1:1 映射）。

        Args:
            service_name: 服务名称。

        Returns:
            对应的 deployment 名称，无映射时返回原名。
        """
        deploy_map = self._data.get("kubernetes", {}).get("deployment_map", {})
        return deploy_map.get(service_name, service_name)

    # --- Neptune / Smart Query (Wave 1, 2026-04-18) ---

    @property
    def neptune_graph_schema_text(self) -> str:
        """Neptune 图 schema 文本，注入 LLM system prompt。"""
        return self._data.get("neptune", {}).get("graph_schema_text", "") or ""

    @property
    def neptune_few_shot_examples(self) -> list:
        """NL->Cypher few-shot 示例列表。"""
        return self._data.get("neptune", {}).get("nl_query_examples", []) or []

    @property
    def neptune_common_relations(self) -> list:
        """通用关系名称列表（空结果重试提示用）。"""
        return self._data.get("neptune", {}).get("common_relations", []) or []

    @property
    def neptune_guard_rules(self) -> dict:
        """查询安全规则。"""
        defaults = {
            "forbidden_operations": ["CREATE", "DELETE", "DETACH", "SET", "MERGE", "REMOVE", "DROP", "CALL"],
            "max_hop_depth": 6,
            "default_limit": 200,
        }
        defaults.update(self._data.get("neptune", {}).get("guard_rules", {}) or {})
        return defaults

    @property
    def neptune_complex_keywords(self) -> dict:
        """复杂问题触发词字典：{'zh': [...], 'en': [...]}."""
        ck = self._data.get("neptune", {}).get("complex_keywords", {}) or {}
        return {"zh": list(ck.get("zh") or []), "en": list(ck.get("en") or [])}

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """点分路径访问。

        Args:
            dotted_key: 如 ``dns.ttl_normal``。
            default: 未找到时返回的默认值。

        Returns:
            配置值或默认值。

        Example::

            profile.get("dns.ttl_normal", 300)
        """
        keys = dotted_key.split(".")
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
            if node is None:
                return default
        return node

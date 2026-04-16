"""
shared/service_registry.py — 统一服务名映射

所有模块通过此模块获取服务名映射，不再各自维护副本。
从 Environment Profile 的 services 段加载。
"""

from typing import Any, Dict


class ServiceRegistry:
    """基于 Environment Profile 的统一服务注册表。

    提供所有映射方向：
    - Neptune 名 <-> K8s deployment
    - Neptune 名 -> CloudWatch 维度
    - Neptune 名 -> DeepFlow app
    - 别名解析（任意名 -> Neptune 标准名）
    """

    def __init__(self, services_config: Dict[str, Any]) -> None:
        """从 profile 的 services 段构建索引。

        Args:
            services_config: profile YAML 中 ``services`` 段的字典。
        """
        self._services = services_config
        self._build_indexes()

    def _build_indexes(self) -> None:
        """构建所有映射方向的索引。"""
        self._k8s_to_neptune: Dict[str, str] = {}
        self._neptune_to_k8s: Dict[str, str] = {}
        self._alias_to_neptune: Dict[str, str] = {}
        self._neptune_to_deepflow: Dict[str, str] = {}

        for name, cfg in self._services.items():
            neptune = cfg.get("neptune_name", name)
            k8s = cfg.get("k8s_deployment", name)

            self._neptune_to_k8s[neptune] = k8s
            self._k8s_to_neptune[k8s] = neptune

            for alias in cfg.get("aliases", []):
                self._alias_to_neptune[alias] = neptune
                self._k8s_to_neptune[alias] = neptune

            if "deepflow_app" in cfg:
                self._neptune_to_deepflow[neptune] = cfg["deepflow_app"]

    def resolve(self, name: str) -> str:
        """将任何名称（K8s / alias / Neptune）解析为 Neptune 标准名。

        Args:
            name: 任意服务名称。

        Returns:
            Neptune 标准名。
        """
        if name in self._services:
            return name
        return self._k8s_to_neptune.get(
            name, self._alias_to_neptune.get(name, name)
        )

    def neptune_to_k8s(self, neptune_name: str) -> str:
        """Neptune 名 -> K8s deployment 名。

        Args:
            neptune_name: Neptune 图谱中的服务名。

        Returns:
            K8s deployment 名称。
        """
        return self._neptune_to_k8s.get(neptune_name, neptune_name)

    def k8s_to_neptune(self, k8s_name: str) -> str:
        """K8s deployment 名 -> Neptune 名。

        Args:
            k8s_name: K8s deployment 名称。

        Returns:
            Neptune 标准名。
        """
        return self._k8s_to_neptune.get(k8s_name, k8s_name)

    def get_tier(self, service_name: str) -> str:
        """获取服务的 Tier 等级。

        Args:
            service_name: 服务名称（任意形式）。

        Returns:
            Tier 等级字符串，默认 Tier2。
        """
        name = self.resolve(service_name)
        return self._services.get(name, {}).get("tier", "Tier2")

    def get_cloudwatch_config(self, service_name: str) -> dict:
        """获取服务的 CloudWatch 监控配置。

        Args:
            service_name: 服务名称（任意形式）。

        Returns:
            CloudWatch 配置字典。
        """
        name = self.resolve(service_name)
        return self._services.get(name, {}).get("cloudwatch", {})

    def get_deepflow_app(self, service_name: str) -> str:
        """获取服务的 DeepFlow app 名称。

        Args:
            service_name: 服务名称（任意形式）。

        Returns:
            DeepFlow app 名称。
        """
        name = self.resolve(service_name)
        return self._neptune_to_deepflow.get(name, name)

    def all_service_names(self) -> list:
        """返回所有 Neptune 标准服务名列表。

        Returns:
            服务名列表。
        """
        return list(self._services.keys())

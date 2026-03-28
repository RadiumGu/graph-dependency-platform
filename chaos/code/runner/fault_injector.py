"""
fault_injector.py - 故障注入抽象层

统一接口，按 experiment.backend 字段分派到对应后端：
  - ChaosMeshBackend: 包装 ChaosMCPClient（kubectl apply Chaos Mesh CRD）
  - FISBackend:       包装 FISClient（boto3 直调 AWS FIS API）

设计原则（ADR-003）：
  - 生成阶段：LLM Agent 通过 MCP 生成并验证模板
  - 执行阶段：Runner 通过此抽象层确定性执行，不依赖 LLM
  - 紧急熔断：最短路径，不经过 MCP/LLM 链路
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .experiment import Experiment

logger = logging.getLogger(__name__)


# ─── 注入结果 ─────────────────────────────────────────────────────────────────

@dataclass
class InjectionResult:
    """故障注入结果，后端无关"""
    experiment_ref: str        # Chaos Mesh experiment name 或 FIS experiment ID
    backend: str               # "chaosmesh" | "fis"
    start_time: str            # ISO8601
    expected_duration: str     # "2m" / "5m"
    extra: dict = field(default_factory=dict)  # 后端特有信息（FIS template_id 等）


# ─── 抽象接口 ─────────────────────────────────────────────────────────────────

class FaultInjector(ABC):
    """
    故障注入统一接口。
    Runner 通过此接口注入故障，不直接依赖 Chaos Mesh 或 FIS 细节。
    """

    @abstractmethod
    def inject(self, experiment: "Experiment") -> InjectionResult:
        """
        注入故障。
        返回 InjectionResult，包含实验标识（Chaos Mesh name 或 FIS experiment ID）。
        """
        ...

    @abstractmethod
    def remove(self, injection: InjectionResult) -> None:
        """
        强制清理故障（Stop Condition 触发 / emergency cleanup）。
        Chaos Mesh: kubectl delete CRD
        FIS: stop_experiment
        """
        ...

    @abstractmethod
    def status(self, injection: InjectionResult) -> str:
        """
        查询实验状态。
        返回值: "running" | "completed" | "stopped" | "failed" | "unknown"
        """
        ...

    @abstractmethod
    def abort(self, injection: InjectionResult) -> None:
        """
        安全熔断（等同于 remove，语义上强调紧急停止）。
        走最短路径，不经过 LLM/MCP 链路。
        """
        ...

    @abstractmethod
    def preflight_check(self) -> bool:
        """
        后端健康检查（Phase 0 使用）。
        返回 True 表示后端可用。
        """
        ...


# ─── Chaos Mesh 后端 ──────────────────────────────────────────────────────────

class ChaosMeshBackend(FaultInjector):
    """
    Chaos Mesh 后端：通过 ChaosMCPClient（kubectl apply CRD）注入故障。
    覆盖：pod_kill / pod_failure / container_kill / network_* /
          http_chaos / io_chaos / time_chaos / kernel_chaos / *_stress
    """

    def __init__(self):
        from .chaos_mcp import ChaosMCPClient
        self._client = ChaosMCPClient()

    def inject(self, experiment: "Experiment") -> InjectionResult:
        ft  = experiment.fault
        svc = experiment.target_service
        ns  = experiment.target_namespace

        manifest = self._client.inject(
            fault_type=ft.type,
            service=svc,
            namespace=ns,
            duration=ft.duration,
            mode=ft.mode,
            value=ft.value,
            latency=ft.latency,
            loss=ft.loss,
            corrupt=ft.corrupt,
            container_names=ft.container_names,
            workers=ft.workers,
            load=ft.load,
            size=ft.size,
            time_offset=ft.time_offset,
            direction=ft.direction,
            external_targets=ft.external_targets,
        )
        exp_name = self._client.extract_experiment_name(manifest, ft.type)
        logger.info(f"ChaosMesh 注入完成: {exp_name} (fault={ft.type})")

        return InjectionResult(
            experiment_ref=exp_name,
            backend="chaosmesh",
            start_time=datetime.now(timezone.utc).isoformat(),
            expected_duration=ft.duration,
            extra={"fault_type": ft.type, "namespace": ns},
        )

    def remove(self, injection: InjectionResult) -> None:
        fault_type = injection.extra.get("fault_type", "")
        namespace  = injection.extra.get("namespace", "default")
        delete_type = self._client.FAULT_TO_DELETE_TYPE.get(fault_type, fault_type)
        self._client.delete(
            chaos_type=delete_type,
            name=injection.experiment_ref,
            namespace=namespace,
        )
        logger.info(f"ChaosMesh 实验已清理: {injection.experiment_ref}")

    def status(self, injection: InjectionResult) -> str:
        """
        Chaos Mesh 实验到期后 CRD 自动删除；通过 kubectl get 判断是否还存在。
        存在 → "running"，不存在 → "completed"
        """
        import subprocess
        fault_type = injection.extra.get("fault_type", "")
        namespace  = injection.extra.get("namespace", "default")
        name       = injection.experiment_ref

        kind_map = {
            "pod_kill": "podchaos", "pod_failure": "podchaos",
            "container_kill": "podchaos",
            "pod_cpu_stress": "stresschaos", "pod_memory_stress": "stresschaos",
            "network_delay": "networkchaos", "network_loss": "networkchaos",
            "network_corrupt": "networkchaos", "network_partition": "networkchaos",
            "network_duplicate": "networkchaos",
            "http_chaos": "httpchaos", "io_chaos": "iochaos",
            "time_chaos": "timechaos", "kernel_chaos": "kernelchaos",
        }
        crd = kind_map.get(fault_type, "podchaos")
        try:
            r = subprocess.run(
                ["kubectl", "get", crd, name, "-n", namespace,
                 "-o", "jsonpath={.metadata.name}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return "running"
            return "completed"
        except Exception as e:
            logger.warning(f"Chaos Mesh status 查询失败: {e}")
            return "unknown"

    def abort(self, injection: InjectionResult) -> None:
        self.remove(injection)

    def preflight_check(self) -> bool:
        """检查是否有残留的 Chaos Mesh 实验"""
        active = self._client.list_experiments()
        if active:
            logger.warning(f"发现 {len(active)} 个残留 Chaos Mesh 实验")
            return False
        return True


# ─── FIS 后端 ─────────────────────────────────────────────────────────────────

class FISBackend(FaultInjector):
    """
    AWS FIS 后端：通过 FISClient（boto3 直调）注入故障。
    覆盖：Lambda / RDS / EKS 节点 / EC2 / EBS / 网络基础设施 / API 注入
    紧急熔断走最短路径：fis.stop_experiment()
    """

    def __init__(self):
        from .fis_backend import FISClient
        self._client = FISClient()

    def inject(self, experiment: "Experiment") -> InjectionResult:
        result = self._client.inject(experiment)
        logger.info(
            f"FIS 注入完成: experiment={result['experiment_id']} "
            f"template={result.get('template_id', '')}"
        )
        return InjectionResult(
            experiment_ref=result["experiment_id"],
            backend="fis",
            start_time=datetime.now(timezone.utc).isoformat(),
            expected_duration=experiment.fault.duration,
            extra={"template_id": result.get("template_id", "")},
        )

    def remove(self, injection: InjectionResult) -> None:
        if not injection.experiment_ref:
            return
        self._client.stop(injection.experiment_ref)
        template_id = injection.extra.get("template_id", "")
        if template_id:
            self._client.delete_template(template_id)

    def status(self, injection: InjectionResult) -> str:
        if not injection.experiment_ref:
            return "unknown"
        fis_status = self._client.status(injection.experiment_ref)
        # 归一化到通用状态
        mapping = {
            "initiating": "running",
            "running":    "running",
            "completed":  "completed",
            "stopping":   "running",
            "stopped":    "stopped",
            "failed":     "failed",
        }
        return mapping.get(fis_status, "unknown")

    def abort(self, injection: InjectionResult) -> None:
        """紧急熔断：直接 stop_experiment，不清理模板（后续 Phase4 清理）"""
        if injection.experiment_ref:
            self._client.stop(injection.experiment_ref)
            logger.info(f"FIS 实验已熔断停止: {injection.experiment_ref}")

    def preflight_check(self) -> bool:
        return self._client.preflight_check()


# ─── 工厂方法 ─────────────────────────────────────────────────────────────────

def create_injector(backend: str) -> FaultInjector:
    """
    按 backend 字段创建对应的故障注入器。

    Args:
        backend: "chaosmesh" 或 "fis"

    Returns:
        FaultInjector 实例

    Raises:
        ValueError: 未知的 backend 值
    """
    if backend == "chaosmesh":
        return ChaosMeshBackend()
    elif backend == "fis":
        return FISBackend()
    else:
        raise ValueError(f"未知的故障注入后端: {backend!r}，支持: 'chaosmesh' | 'fis'")

"""
log_collector.py - 实验期间 Pod 日志后台采集

在故障注入期间后台收集受影响服务的 Pod 日志，
用于故障传播分析和报告生成。
"""
import subprocess
import threading
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """单条日志记录。"""

    pod_name: str
    timestamp: str
    message: str
    level: str = "INFO"  # INFO/WARN/ERROR


@dataclass
class LogCollectionResult:
    """日志采集结果汇总。"""

    service: str
    namespace: str
    pod_count: int = 0
    total_lines: int = 0
    error_count: int = 0
    entries: list = field(default_factory=list)
    error_summary: dict = field(default_factory=dict)  # error_type -> count

    def summary(self) -> str:
        """返回人类可读的摘要字符串。"""
        if not self.entries and self.total_lines == 0:
            return f"{self.service}: 无日志采集"
        return (
            f"{self.service}: {self.total_lines} 行日志, "
            f"{self.error_count} 个错误, {self.pod_count} 个 Pod"
        )


class PodLogCollector:
    """后台收集 K8s Pod 日志。"""

    def __init__(
        self,
        service: str,
        namespace: str = "default",
        since: str = "2m",
        max_lines: int = 500,
    ):
        """初始化日志采集器。

        Args:
            service: 逻辑服务名（Neptune 名，如 petsearch）。
            namespace: K8s namespace。
            since: kubectl logs --since 参数（如 "1m", "2m"）。
            max_lines: 最大采集行数，超过后停止跟踪。
        """
        from .config import SERVICE_TO_K8S_LABEL

        self.service = service
        self.namespace = namespace
        self.since = since
        self.max_lines = max_lines
        self.label = SERVICE_TO_K8S_LABEL.get(service, service)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lines: list[str] = []

    def start_background(self) -> None:
        """后台启动日志收集。"""
        self._stop_event.clear()
        self._lines = []
        self._thread = threading.Thread(target=self._collect, daemon=True)
        self._thread.start()
        logger.info(
            f"日志采集启动: {self.service} (app={self.label}, ns={self.namespace})"
        )

    def _collect(self) -> None:
        """后台线程：kubectl logs -f 持续跟踪。"""
        try:
            cmd = [
                "kubectl", "logs",
                "-l", f"app={self.label}",
                "-n", self.namespace,
                f"--since={self.since}",
                "--tail=200",
                "-f",        # follow
                "--prefix",  # 带 pod 名前缀
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                self._lines.append(line.strip())
                if len(self._lines) >= self.max_lines:
                    break
            proc.terminate()
        except Exception as e:
            logger.warning(f"日志采集异常 {self.service}: {e}")

    def stop_and_collect(self) -> LogCollectionResult:
        """停止采集，返回分析结果。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

        result = LogCollectionResult(
            service=self.service,
            namespace=self.namespace,
            total_lines=len(self._lines),
        )

        # 分析错误类型和 Pod 名
        pods: set[str] = set()
        error_types: dict[str, int] = {}

        for line in self._lines:
            # kubectl --prefix 格式: [pod/name] message
            if line.startswith("[pod/"):
                pod_name = line.split("]")[0].replace("[pod/", "")
                pods.add(pod_name)

            lower = line.lower()
            if any(kw in lower for kw in ["error", "exception", "fail", "panic"]):
                result.error_count += 1
                if "timeout" in lower or "timed out" in lower:
                    error_types["timeout"] = error_types.get("timeout", 0) + 1
                elif "connection refused" in lower or "connect" in lower:
                    error_types["connection"] = error_types.get("connection", 0) + 1
                elif "5xx" in lower or "500" in lower or "502" in lower or "503" in lower:
                    error_types["5xx"] = error_types.get("5xx", 0) + 1
                elif "oom" in lower or "out of memory" in lower:
                    error_types["oom"] = error_types.get("oom", 0) + 1
                else:
                    error_types["other"] = error_types.get("other", 0) + 1

        result.pod_count = len(pods)
        result.error_summary = error_types

        logger.info(f"日志采集完成: {result.summary()}")
        return result

"""AWS 调用的限流退避与分页上限 —— 6 条 ETL 共用。

## 为什么分页上限必须抛异常，不能静默截断（这是本模块最要紧的一条）

`etl_aws/graph_gc.py` 的 `run_gc` 是这个形状：

    aws_ec2 = set()
    for page in ec2_client.get_paginator('describe_instances').paginate(...):
        ...收集 InstanceId...
    _gc_vertices('EC2Instance', 'instance_id', aws_ec2)

而 `_gc_vertices` 做的是：

    stale = set(graph_map.keys()) - aws_ids     # 图里有、这轮没收到的
    for pid in stale:
        neptune_query(f"g.V('{graph_map[pid]}').drop()")   # 删掉

也就是说 **`aws_ids` 不完整 == 真实存在的资源被当成 ghost 节点删除**。

如果分页上限实现成「到上限就 return，调用方拿到部分结果」，那么加这道
「保护」本身就制造了一条数据丢失路径 —— 比不加更糟，因为它静默。

所以上限触发时 **raise**：
  · 采集路径上，被现有的 `except Exception` + logger.warning 接住
    → 该类资源这轮不更新 → 不会删任何东西
  · GC 路径上，异常穿出 `for page in ...`
    → 对应的 `_gc_vertices` 根本不执行 → 不会删任何东西

两条路径都是 fail-safe。这同时满足本仓的第一条设计原则 ——
**任何判据都必须能区分「否」与「我不知道」**：
截断后的空集合是「我不知道」，绝不能被下游读成「这些资源不存在」。

## 上限是跑飞护栏，不是限流器

`DEFAULT_MAX_PAGES` 取得很宽松，正常运行永远不该触发。它要防的是
Lambda 15 分钟硬墙下一次失控分页把整个调用拖死 —— 那种情况下函数超时，
不会留下任何部分结果，也不会有日志说明卡在哪。

## 重试模式为什么选 standard 而不是 adaptive

`adaptive` 带客户端侧限速，在普通长跑进程上是对的；但我们跑在 Lambda 里，
客户端限速会拉长墙钟时间，而墙钟时间本身就是我们最紧的约束
（15 分钟硬上限，且超时无部分结果）。`standard` 只做退避重试、不限速。

boto3 不显式配置时用的是 legacy 模式，它的重试覆盖面比 standard 窄。
所以这里显式声明，不依赖默认值。

## 与 Lambda 运行时的依赖关系

只 import boto3 / botocore，两者都由 Lambda 运行时提供，不需要进 Layer 的
依赖目录。但**本文件本身**必须在 Layer 里（`infra/lambda/shared/python/`），
且必须列进 `shared/build.sh` 的必需项校验。

调用方应当用防御式 import（见 README 或各 ETL 顶部）：

    try:
        from aws_resilience import make_client
    except ImportError:          # Layer 还没更新 — 降级为裸 boto3
        import boto3
        make_client = boto3.client

这样函数代码的部署不依赖 Layer 的部署顺序。2026-10-04 与 10-05 的两次
Layer 事故都是「函数代码先上、Layer 没跟上」这一类，见
docs/lessons/cdk-fromasset-packages-ungitted-deps.md。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterator

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# 跑飞护栏。正常运行永远不该触发 —— 触发了说明数据量超出预期或 API 行为变了。
DEFAULT_MAX_PAGES = int(os.environ.get("AWS_MAX_PAGES", "1000"))

# standard 模式的退避重试次数。boto3 standard 默认 3；ETL 扇出调用多，
# 给到 5 以吸收短时限流，同时不至于把墙钟时间拉得太长。
DEFAULT_MAX_ATTEMPTS = int(os.environ.get("AWS_MAX_ATTEMPTS", "5"))

DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("AWS_CONNECT_TIMEOUT", "5"))
DEFAULT_READ_TIMEOUT = float(os.environ.get("AWS_READ_TIMEOUT", "30"))

# 日志前缀固定，便于告警与 Logs Insights 过滤。
LOG_PREFIX_TRUNCATED = "AWS_PAGINATION_TRUNCATED"
LOG_PREFIX_DEADLINE = "AWS_PAGINATION_DEADLINE"


class PaginationTruncated(RuntimeError):
    """分页达到页数上限。

    刻意是异常而不是返回值 —— 见模块文档。调用方**不应该**吞掉它然后
    把已收到的部分结果当完整结果用，尤其不能喂给任何「图里有而这轮没收到
    就删」的逻辑。
    """


class PaginationDeadlineExceeded(RuntimeError):
    """分页过程中触达调用截止时间。

    同样是异常：提前停止产生的部分结果与完整结果不可区分，
    而下游的 GC 会把两者都当成完整结果。
    """


class Deadline:
    """调用级截止时间。

    从 Lambda context 构造：

        dl = Deadline.from_lambda_context(context, reserve_seconds=60)

    `reserve_seconds` 是留给收尾（写 Neptune、刷日志）的预算。
    """

    __slots__ = ("_expires_at",)

    def __init__(self, expires_at: float | None):
        self._expires_at = expires_at

    @classmethod
    def from_lambda_context(cls, context: Any, reserve_seconds: float = 60.0) -> "Deadline":
        getter = getattr(context, "get_remaining_time_in_millis", None)
        if getter is None:
            # 本地跑或测试桩 —— 不设限，而不是拍一个假值。
            return cls(None)
        remaining = getter() / 1000.0
        return cls(time.monotonic() + max(remaining - reserve_seconds, 0.0))

    @classmethod
    def none(cls) -> "Deadline":
        return cls(None)

    def expired(self) -> bool:
        return self._expires_at is not None and time.monotonic() >= self._expires_at

    def remaining(self) -> float | None:
        if self._expires_at is None:
            return None
        return max(self._expires_at - time.monotonic(), 0.0)


def retry_config(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    **extra: Any,
) -> Config:
    """显式声明重试与超时，不依赖 boto3 默认的 legacy 模式。"""
    return Config(
        retries={"mode": "standard", "max_attempts": max_attempts},
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        **extra,
    )


class _BoundedPaginator:
    """包装 botocore 的 paginator，给 paginate() 加页数上限与截止时间。"""

    __slots__ = ("_inner", "_service", "_operation", "_max_pages", "_deadline")

    def __init__(self, inner: Any, service: str, operation: str,
                 max_pages: int, deadline: Deadline):
        self._inner = inner
        self._service = service
        self._operation = operation
        self._max_pages = max_pages
        self._deadline = deadline

    def paginate(self, **kwargs: Any) -> Iterator[dict]:
        pages = 0
        for page in self._inner.paginate(**kwargs):
            if self._deadline.expired():
                msg = (
                    f"{LOG_PREFIX_DEADLINE} service={self._service} "
                    f"operation={self._operation} pages_consumed={pages} "
                    "—— 触达调用截止时间，已收到的部分结果不可用于陈旧判定"
                )
                logger.error(msg)
                raise PaginationDeadlineExceeded(msg)
            pages += 1
            if pages > self._max_pages:
                msg = (
                    f"{LOG_PREFIX_TRUNCATED} service={self._service} "
                    f"operation={self._operation} max_pages={self._max_pages} "
                    "—— 超出跑飞护栏。这是「我不知道」而不是「没有更多数据」，"
                    "部分结果不可用于陈旧判定"
                )
                logger.error(msg)
                raise PaginationTruncated(msg)
            yield page

    def __getattr__(self, name: str) -> Any:
        # build_full_result 等其余接口透传给原 paginator。
        return getattr(self._inner, name)


class _ResilientClient:
    """透明包装 boto3 client，只改 get_paginator 的行为。"""

    __slots__ = ("_client", "_service", "_max_pages", "_deadline")

    def __init__(self, client: Any, service: str, max_pages: int, deadline: Deadline):
        self._client = client
        self._service = service
        self._max_pages = max_pages
        self._deadline = deadline

    def get_paginator(self, operation_name: str) -> _BoundedPaginator:
        return _BoundedPaginator(
            self._client.get_paginator(operation_name),
            service=self._service,
            operation=operation_name,
            max_pages=self._max_pages,
            deadline=self._deadline,
        )

    def __getattr__(self, name: str) -> Any:
        # describe_* / list_* 等直接调用全部透传 —— 它们由 retry_config 覆盖。
        return getattr(self._client, name)


def make_client(
    service_name: str,
    *,
    region_name: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline: Deadline | None = None,
    session: Any = None,
    config: Config | None = None,
    **kwargs: Any,
) -> Any:
    """`boto3.client` 的替代品：带显式重试退避 + 分页跑飞护栏。

    关键字兼容：现有调用点形如 `boto3.client('ec2', region_name=REGION)`，
    改成 `make_client('ec2', region_name=REGION)` 即可。
    """
    factory = (session or boto3).client
    client = factory(
        service_name,
        region_name=region_name,
        config=config or retry_config(),
        **kwargs,
    )
    return _ResilientClient(
        client,
        service=service_name,
        max_pages=max_pages,
        deadline=deadline or Deadline.none(),
    )


def paginate_all(
    client: Any,
    operation_name: str,
    key: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline: Deadline | None = None,
    **kwargs: Any,
) -> list:
    """把某个分页操作的所有条目收集成一个 list。

    给那些**当前完全没分页**的调用点用（例如 `eks.list_clusters()` 每页 100，
    超过就静默少收，而少收会让 GC 删真集群）。

    上限或截止时间触发时抛异常，不返回部分结果 —— 理由同模块文档。
    """
    inner = client.get_paginator(operation_name)
    if not isinstance(inner, _BoundedPaginator):
        inner = _BoundedPaginator(
            inner,
            service=getattr(client, "_service", "unknown"),
            operation=operation_name,
            max_pages=max_pages,
            deadline=deadline or Deadline.none(),
        )
    out: list = []
    for page in inner.paginate(**kwargs):
        out.extend(page.get(key, []) or [])
    return out

"""
runner_direct.py — Direct Runner: 基于现有 runner.py 逻辑的薄封装。

不重新实现 5-phase 逻辑，而是委托给现有 ExperimentRunner。
Direct 版的存在是为了提供 RunnerBase 接口 + 标准化返回格式。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from runner.base import RunnerBase  # type: ignore

logger = logging.getLogger(__name__)


class DirectRunner(RunnerBase):
    """Direct Runner — 委托给现有 ExperimentRunner。"""

    ENGINE_NAME = "direct"

    def run(self, experiment) -> Any:
        from runner.runner import ExperimentRunner

        t0 = time.time()
        self._validate_namespace(experiment.target_namespace)

        inner = ExperimentRunner(dry_run=self.dry_run, tags=self.tags)
        result = inner.run(experiment)

        elapsed_ms = int((time.time() - t0) * 1000)

        # 附加引擎元数据
        if not hasattr(result, "engine_meta"):
            result.engine_meta = {}
        result.engine_meta.update({
            "engine": "direct",
            "model_used": None,
            "latency_ms": elapsed_ms,
            "token_usage": {
                "input": 0, "output": 0, "total": 0,
                "cache_read": 0, "cache_write": 0,
            },
        })

        return result

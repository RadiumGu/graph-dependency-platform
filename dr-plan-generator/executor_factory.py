"""
executor_factory.py — DR Executor 引擎工厂。

⚠️ dry_run=True 是不可商量的默认值。
   代码参数 + env 变量双重 gate，两个都 False 才真正执行。
"""
from __future__ import annotations

import logging
import os

from executor_base import ExecutorBase

logger = logging.getLogger(__name__)


def make_dr_executor(dry_run: bool = True) -> ExecutorBase:
    """构造 DR Executor 引擎。dry_run=True 是不可商量的默认值。"""
    engine = (os.environ.get("DR_EXECUTOR_ENGINE") or "direct").lower()

    # 双重 gate: 代码参数 + env 变量，两个都 False 才真执行
    env_dry_run = os.environ.get("DR_EXECUTOR_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run or env_dry_run  # 任一为 True → dry_run

    if not effective_dry_run:
        logger.warning("⚠️ DR Executor dry_run=False — 将执行真实 DR 操作！")

    if engine == "strands":
        try:
            from executor_strands import StrandsExecutor
            return StrandsExecutor(dry_run=effective_dry_run)
        except ImportError as e:
            logger.warning("Strands Executor 不可用 (%s)；回退 direct。", e)
        except Exception as e:
            logger.warning("Strands Executor 构造失败 (%r)；回退 direct。", e)

    from executor_direct import DirectExecutor
    return DirectExecutor(dry_run=effective_dry_run)

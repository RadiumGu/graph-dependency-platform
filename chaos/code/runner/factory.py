"""
factory.py — Runner 引擎工厂。

⚠️ dry_run=True 是不可商量的默认值。
   代码参数 + env 变量双重 gate，两个都 False 才真正执行。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def make_runner_engine(dry_run: bool = True):
    """构造 Runner 引擎。dry_run=True 是不可商量的默认值。"""
    engine = (os.environ.get("CHAOS_RUNNER_ENGINE") or "direct").lower()

    # 双重 gate: 代码参数 + env 变量，两个都 False 才真执行
    env_dry_run = os.environ.get("CHAOS_RUNNER_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run or env_dry_run  # 任一为 True → dry_run

    if not effective_dry_run:
        logger.warning("⚠️ Runner dry_run=False — 将执行真实故障注入！")

    if engine == "strands":
        try:
            from runner.runner_strands import StrandsRunner  # type: ignore
            return StrandsRunner(dry_run=effective_dry_run)
        except ImportError as e:
            logger.warning("Strands Runner 不可用 (%s)；回退 direct。", e)
        except Exception as e:
            logger.warning("Strands Runner 构造失败 (%r)；回退 direct。", e)

    from runner.runner_direct import DirectRunner  # type: ignore
    return DirectRunner(dry_run=effective_dry_run)

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
    engine = (os.environ.get("CHAOS_RUNNER_ENGINE") or "strands").lower()

    # 双重 gate: 代码参数 + env 变量，两个都 False 才真执行
    env_dry_run = os.environ.get("CHAOS_RUNNER_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run or env_dry_run  # 任一为 True → dry_run

    if not effective_dry_run:
        logger.warning("⚠️ Runner dry_run=False — 将执行真实故障注入！")

    # ── 2026-09-20：去掉 direct 回退，只保留 strands ──────────────────────
    #
    # 那个回退在生产上**一直是实际路径**：线上包里没装 strands
    # （实测条目数 0），于是 LLM 路径全在静默跑 direct，而 golden 基线
    # 测的是本地装了 strands 的环境 —— 两者从未对齐过五个月。
    # 补上依赖并部署后实测确认线上已真正走 strands（"回退 direct" 0 条）。
    #
    # ⚠️ 代价：strands 不可用时现在**直接抛异常**、不再降级。
    # 这是迁移目标（回退掩盖了真相），但要求所有部署环境装齐 strands。
    from runner.runner_strands import StrandsRunner  # type: ignore
    return StrandsRunner(dry_run=effective_dry_run)

"""
factory.py — PolicyGuard 引擎工厂。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def make_policy_guard(rules_path: str | None = None):
    """构造 PolicyGuard 引擎，切换 env：POLICY_GUARD_ENGINE=direct|strands。"""
    # ── 2026-09-20：去掉 direct 回退，只保留 strands ──────────────────────
    #
    # 回退分支删除前的问题是它**掩盖真相**：strands 不可用时只留一行
    # warning 就静默降级，而 golden 基线测的是装了 strands 的环境 ——
    # 两者可能长期不一致而无人发现（rca-layer2 就这样错了五个月）。
    #
    # ⚠️ 代价：strands 不可用时现在**直接抛异常**、不再降级。
    # 这是迁移目标，但要求所有部署环境装齐 strands。
    #
    # `_load_rules` / `_build_system_prompt` 已搬到 `guard_common.py`，
    # 所以删 guard_direct.py 不再牵动 strands 实现。
    from policy.guard_strands import StrandsPolicyGuard  # type: ignore
    return StrandsPolicyGuard(rules_path=rules_path)

"""
factory.py — PolicyGuard 引擎工厂。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def make_policy_guard(rules_path: str | None = None):
    """构造 PolicyGuard 引擎，切换 env：POLICY_GUARD_ENGINE=direct|strands。"""
    engine = (os.environ.get("POLICY_GUARD_ENGINE") or "direct").lower()
    if engine == "strands":
        try:
            from policy.guard_strands import StrandsPolicyGuard  # type: ignore
            return StrandsPolicyGuard(rules_path=rules_path)
        except ImportError as e:
            logger.warning("Strands PolicyGuard 不可用 (%s)；回退 direct。", e)
        except Exception as e:
            logger.warning("Strands PolicyGuard 构造失败 (%r)；回退 direct。", e)

    from policy.guard_direct import DirectPolicyGuard  # type: ignore
    return DirectPolicyGuard(rules_path=rules_path)

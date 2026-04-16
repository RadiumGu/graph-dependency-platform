"""
registry/policy_loader.py — DR Plan 生成策略加载器

加载顺序: plan_policy.yaml → custom_policy.yaml/.md（覆盖）→ CLI --set（最高优先级）

三层定制化架构:
  Layer 3 (最高): CLI --set 覆盖 — 一次性参数覆盖（不修改文件）
  Layer 2:        plan_policy.yaml / custom_policy.yaml / custom_policy.md — 生成策略
  Layer 1 (最低): service_types.yaml / custom_types.yaml — 资源类型定义（已有）
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional

import yaml


_DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(__file__), "plan_policy.yaml")


def deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge *override* into a **copy** of *base*.

    - Dicts are recursively merged.
    - Lists are replaced (not appended).
    - Scalar values in *override* win.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _infer_value(raw: str) -> Any:
    """Best-effort type coercion for CLI ``--set`` values."""
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


class PlanPolicy:
    """DR Plan 生成策略加载器.

    加载顺序:
      plan_policy.yaml → custom_policy.yaml/.md（覆盖）→ CLI --set（最高优先级）

    Parameters
    ----------
    custom_path:
        Path to a custom policy file (YAML or Markdown).
    cli_overrides:
        List of ``"dotted.key=value"`` strings from ``--set`` flags.
    """

    def __init__(
        self,
        custom_path: Optional[str] = None,
        cli_overrides: Optional[List[str]] = None,
    ) -> None:
        self._policy: Dict[str, Any] = self._load_yaml(_DEFAULT_POLICY_PATH)
        self._custom_rules: list = []  # populated from Markdown NL rules

        if custom_path:
            if custom_path.endswith(".md"):
                self._load_markdown_policy(custom_path)
            else:
                custom = self._load_yaml(custom_path)
                self._policy = deep_merge(self._policy, custom)

        if cli_overrides:
            self._apply_cli_overrides(cli_overrides)

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @staticmethod
    def _load_yaml(path: str) -> dict:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}

    def _load_markdown_policy(self, path: str) -> None:
        """Parse a Markdown policy file.

        Structured tables → policy dict (deep-merged).
        ``## 自定义规则`` section → self._custom_rules via LLM.
        """
        try:
            from .markdown_policy_parser import parse_markdown_policy
        except ImportError:
            # Markdown parser not yet available — treat as YAML fallback
            return

        with open(path, encoding="utf-8") as fh:
            text = fh.read()

        structured = parse_markdown_policy(text)
        self._policy = deep_merge(self._policy, structured)

        # LLM-based NL rule parsing (best-effort)
        try:
            from .rule_parser import parse_custom_rules

            self._custom_rules = parse_custom_rules(text)
        except Exception:
            pass

    def _apply_cli_overrides(self, overrides: List[str]) -> None:
        """Apply ``--set`` key=value overrides.

        Supports dotted paths such as ``phase-1.requires_approval=true``.
        """
        for item in overrides:
            if "=" not in item:
                continue
            dotted_key, raw_value = item.split("=", 1)
            value = _infer_value(raw_value)

            keys = dotted_key.split(".")
            node = self._policy
            for k in keys[:-1]:
                if k not in node or not isinstance(node[k], dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def raw(self) -> Dict[str, Any]:
        """Return the full (merged) policy dict."""
        return self._policy

    # --- Scope ---

    def get_scope_policy(self, scope: str) -> dict:
        """Return the scope-specific policy block."""
        return self._policy.get("scope_policies", {}).get(scope, {})

    # --- Phase ---

    def get_phase_policy(self, phase_id: str) -> dict:
        """Return the phase-specific policy block."""
        return self._policy.get("phase_policies", {}).get(phase_id, {})

    # --- Resource overrides ---

    def get_resource_override(self, resource_type: str) -> dict:
        """Return the override block for a resource type."""
        return self._policy.get("resource_overrides", {}).get(resource_type, {})

    def get_estimated_time(self, resource_type: str) -> int:
        """Estimated seconds for a resource type (override > default)."""
        override = self.get_resource_override(resource_type)
        default = self._policy.get("general", {}).get("default_estimated_time", 60)
        return override.get("estimated_time", default)

    def requires_approval(self, phase_id: str, resource_type: str = "") -> bool:
        """Whether a step needs approval (resource > phase > False)."""
        if resource_type:
            res_override = self.get_resource_override(resource_type)
            if "requires_approval" in res_override:
                return bool(res_override["requires_approval"])
        phase_policy = self.get_phase_policy(phase_id)
        return bool(phase_policy.get("requires_approval", False))

    def get_max_parallel(self, phase_id: str) -> int:
        """Max parallel steps within a phase."""
        if not self._policy.get("general", {}).get("parallel_within_layer", True):
            return 1
        phase_policy = self.get_phase_policy(phase_id)
        return int(phase_policy.get("max_parallel", 1))

    # --- Rollback ---

    def get_rollback_policy(self) -> dict:
        """Return the rollback policy block."""
        return self._policy.get("rollback_policy", {})

    def is_non_reversible(self, action: str) -> bool:
        """Check if an action is on the non-reversible list."""
        policy = self.get_rollback_policy()
        return action in policy.get("non_reversible_actions", [])

    # --- General helpers ---

    def get_general(self, key: str, default: Any = None) -> Any:
        return self._policy.get("general", {}).get(key, default)

    def get_variables(self) -> Dict[str, str]:
        return self._policy.get("general", {}).get("variables", {})

    def get_notification_policy(self) -> dict:
        return self._policy.get("notification", {})

    # --- Custom rules ---

    def get_custom_rules(self) -> list:
        """Return parsed custom rules (from Markdown NL section)."""
        return list(self._custom_rules)

    def get_rule_engine(self):
        """Return a RuleEngine instance for runtime rule checking."""
        try:
            from .rule_engine import RuleEngine

            return RuleEngine(self._custom_rules)
        except ImportError:
            return None

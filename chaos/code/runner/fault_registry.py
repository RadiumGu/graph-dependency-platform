"""
fault_registry.py — 故障类型注册表

从 fault_catalog.yaml 加载所有故障类型定义，提供统一的访问接口。
向后兼容导出 FAULT_DEFAULTS 和 FIS_ACTION_MAP，供现有代码无感切换。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "fault_catalog.yaml")


@dataclass
class FaultDef:
    """单条故障类型定义。"""

    type: str
    backend: str
    category: str
    description: str
    fis_action_id: str
    default_params: dict[str, Any]
    requires: list[str]
    tier: list[str]
    crd: str = ""                             # ChaosMesh CRD 类型（PodChaos, NetworkChaos 等）
    composite: bool = False
    scenario_id: str = ""                      # FIS Scenario Library 场景 ID
    sub_actions: list = field(default_factory=list)
    required_tags: dict = field(default_factory=dict)
    template_source: str = ""
    doc_url: str = ""
    recommended_for: list = field(default_factory=list)


def _load_catalog(path: str) -> dict[str, FaultDef]:
    """从 fault_catalog.yaml 加载故障定义，返回 type → FaultDef 字典。"""
    try:
        import yaml  # type: ignore
    except ImportError:
        try:
            from runner._yaml_compat import safe_load as yaml_safe_load  # type: ignore
            yaml = type("_Y", (), {"safe_load": staticmethod(yaml_safe_load)})()
        except Exception:
            logger.warning(
                "PyYAML 未安装，fault_registry 回退到空字典。"
                " 请执行 pip install pyyaml"
            )
            return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"fault_catalog.yaml 不存在: {path}，fault_registry 回退到空字典")
        return {}
    except Exception as e:
        logger.warning(f"fault_catalog.yaml 解析失败: {e}，fault_registry 回退到空字典")
        return {}

    catalog: dict[str, FaultDef] = {}
    for section_key in ("chaosmesh", "fis", "fis_scenarios"):
        entries = data.get(section_key) or []
        for entry in entries:
            try:
                fd = FaultDef(
                    type=entry["type"],
                    backend=entry["backend"],
                    category=entry["category"],
                    description=entry.get("description", ""),
                    fis_action_id=entry.get("fis_action_id", ""),
                    default_params=entry.get("default_params") or {},
                    requires=entry.get("requires") or [],
                    tier=entry.get("tier") or [],
                    composite=entry.get("composite", False),
                    sub_actions=entry.get("sub_actions") or [],
                    required_tags=entry.get("required_tags") or {},
                    template_source=entry.get("template_source", ""),
                    doc_url=entry.get("doc_url", ""),
                    recommended_for=entry.get("recommended_for") or [],
                    crd=entry.get("crd", ""),
                    scenario_id=entry.get("scenario_id", ""),
                )
                catalog[fd.type] = fd
            except KeyError as e:
                logger.warning(f"fault_catalog.yaml 条目缺少必填字段 {e}，已跳过: {entry}")
    return catalog


# ── 全局单例 ──────────────────────────────────────────────────────────────────

CATALOG: dict[str, FaultDef] = _load_catalog(_CATALOG_PATH)

if CATALOG:
    logger.debug(f"fault_registry 加载完成: {len(CATALOG)} 种故障类型")
else:
    logger.warning("fault_registry CATALOG 为空，请检查 fault_catalog.yaml")


# ── 便捷查询函数 ──────────────────────────────────────────────────────────────

def all_types() -> list[str]:
    """返回所有已注册的故障类型名列表（含复合场景）。"""
    return list(CATALOG.keys())


def by_backend(backend: str) -> dict[str, FaultDef]:
    """按 backend 过滤，返回 type → FaultDef 字典。

    Args:
        backend: "chaosmesh" 或 "fis" 或 "fis-scenario"
    """
    return {k: v for k, v in CATALOG.items() if v.backend == backend}


def by_category(category: str) -> dict[str, FaultDef]:
    """按故障域过滤，返回 type → FaultDef 字典。

    Args:
        category: compute | network | data | resources | dependencies | az-level | cross-az | cross-region
    """
    return {k: v for k, v in CATALOG.items() if v.category == category}


def by_composite(composite: bool = True) -> list[FaultDef]:
    """按是否为复合场景过滤，返回 FaultDef 列表。

    Args:
        composite: True 返回复合场景，False 返回单一故障类型
    """
    return [f for f in CATALOG.values() if f.composite == composite]


def scenarios() -> list[FaultDef]:
    """返回所有 FIS Scenario Library 复合场景列表。"""
    return by_composite(True)


def by_recommended_for(arch: str) -> list[FaultDef]:
    """按推荐架构场景过滤，返回 FaultDef 列表。

    Args:
        arch: 架构场景标签，如 "eks-microservices", "multi-az", "serverless", "all" 等
    """
    return [f for f in CATALOG.values() if arch in f.recommended_for]


# ── 向后兼容导出 ──────────────────────────────────────────────────────────────

def _build_fault_defaults() -> dict[str, dict[str, Any]]:
    """从 ChaosMesh 条目生成 FAULT_DEFAULTS 格式（type → default_params）。
    只包含 chaosmesh section，不包含 fis_scenarios。
    """
    return {k: dict(v.default_params) for k, v in CATALOG.items() if v.backend == "chaosmesh"}


def _build_fis_action_map() -> dict[str, str]:
    """从 FIS 条目生成 FIS_ACTION_MAP 格式（type → fis_action_id）。
    只包含 fis section，不包含 fis_scenarios。
    """
    return {k: v.fis_action_id for k, v in CATALOG.items() if v.backend == "fis"}


# 向后兼容常量，供现有代码直接 import 使用
FAULT_DEFAULTS: dict[str, dict[str, Any]] = _build_fault_defaults()
FIS_ACTION_MAP: dict[str, str] = _build_fis_action_map()

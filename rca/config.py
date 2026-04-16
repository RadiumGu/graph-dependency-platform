"""
config.py - 全局服务名规范映射

从 profiles/petsite.yaml 的 services 段加载，不再手工维护硬编码映射。
所有模块统一从此处导入 CANONICAL / NEPTUNE_TO_DEPLOYMENT / NEPTUNE_TO_K8S_LABEL。
"""

import os
import sys

# 确保项目根目录在 sys.path 中（供 profiles/ 和 shared/ import 使用）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from profiles.profile_loader import EnvironmentProfile
from shared.service_registry import ServiceRegistry

# 加载 profile 和 registry
_profile = EnvironmentProfile()
_registry = ServiceRegistry(_profile.get("services", {}))

# ─── 派生映射（保持向后兼容的 dict 接口）─────────────────────────

# K8s Deployment 名 / DeepFlow 服务前缀  →  Neptune 服务名
CANONICAL: dict[str, str] = {}
for _name, _cfg in _profile.get("services", {}).items():
    neptune = _cfg.get("neptune_name", _name)
    k8s_dep = _cfg.get("k8s_deployment", _name)
    CANONICAL[k8s_dep] = neptune
    # 加入 k8s_label 作为额外别名（如 service-petsite → petsite）
    if "k8s_label" in _cfg and _cfg["k8s_label"] != k8s_dep:
        CANONICAL[_cfg["k8s_label"]] = neptune
    # 加入 aliases
    for alias in _cfg.get("aliases", []):
        CANONICAL[alias] = neptune

# Neptune 服务名  →  首选 K8s Deployment 名
NEPTUNE_TO_DEPLOYMENT: dict[str, str] = {}
for _dep, _svc in CANONICAL.items():
    if _svc not in NEPTUNE_TO_DEPLOYMENT:
        NEPTUNE_TO_DEPLOYMENT[_svc] = _dep

# Neptune 服务名  →  K8s Pod app label
NEPTUNE_TO_K8S_LABEL: dict[str, str] = {}
for _name, _cfg in _profile.get("services", {}).items():
    neptune = _cfg.get("neptune_name", _name)
    k8s_label = _cfg.get("k8s_label", _cfg.get("k8s_deployment", _name))
    NEPTUNE_TO_K8S_LABEL[neptune] = k8s_label

# 导出 registry 和 profile 供其他模块直接使用
registry = _registry
profile = _profile

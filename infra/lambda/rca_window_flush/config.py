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

# ── Feature Flags ─────────────────────────────────────────────────────────────
# 控制新功能的开关，便于逐步灰度上线或紧急回滚。
#
# 2026-08-28 补入：本文件此前 **没有** 定义 FEATURE_FLAGS，而
# core/decision_engine.py:127 有 `from config import FEATURE_FLAGS`，
# 外层是 `except Exception: pass` —— ImportError 被静默吞掉，
# 导致 auto_remediation_enabled 这个用来阻止自动修复的开关在 rca 包里
# **从未被检查过**，action_level='auto' 不会被降级为 semi_auto。
# 取值与 infra/lambda/rca_window_flush/config.py 保持一致。
#
# alert_buffer_enabled          — 告警聚合缓冲，False=直通原有 RCA 流程
# topology_correlation_enabled  — 拓扑关联分组，False=每条告警独立成组
# feedback_enabled              — Slack 反馈收集，False=不显示反馈按钮
# auto_remediation_enabled      — 自动修复执行，False=仅建议不自动执行
# p0_bypass_buffer              — P0 告警跳过缓冲，True=P0 立即处理
FEATURE_FLAGS: dict[str, bool] = {
    'alert_buffer_enabled':          True,
    'topology_correlation_enabled':  True,
    'feedback_enabled':              False,
    'auto_remediation_enabled':      False,
    'p0_bypass_buffer':              True,
}

# 导出 registry 和 profile 供其他模块直接使用
registry = _registry
profile = _profile

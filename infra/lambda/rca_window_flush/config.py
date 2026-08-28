"""
config.py - 全局服务名规范映射（window-flush Lambda 副本）

与 rca/config.py 保持同一派生逻辑：所有映射从 profiles/petsite.yaml 派生，
本文件不再硬编码任何服务名。

2026-08-28 重写背景（实测漂移，非假设风险）：
  本文件原先硬编码 CANONICAL / NEPTUNE_TO_K8S_LABEL，已与唯一源头脱节：
    - 把 pethistory-deployment 映射到 'petadoptionshistory'，但活图里
      **不存在** 该 Microservice；profiles/petsite.yaml 声明的
      neptune_name 是 'pethistory'，'petadoptionshistory' 只是它的 alias
    - 含 'petfood'，活图 15 个 Microservice 里同样不存在
  即硬编码副本把 alias 当成了规范名，且多出了不存在的服务。

打包依赖：本 Lambda 的部署包内含 profiles/ 与 shared/（见 rca/deploy.sh 的
拷贝清单），因此可以和 rca/config.py 一样走 EnvironmentProfile + ServiceRegistry。
"""

import os
import sys

# 确保项目根目录在 sys.path 中（供 profiles/ 和 shared/ import 使用）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# Lambda 运行时包根就是本文件所在目录（profiles/ 与 shared/ 是同级目录）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

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
    # 加入 aliases（如 petadoptionshistory / pethistory-service → pethistory）
    for alias in _cfg.get("aliases", []):
        CANONICAL[alias] = neptune

# Neptune 服务名  →  首选 K8s Deployment 名
NEPTUNE_TO_DEPLOYMENT: dict[str, str] = {}
for _dep, _svc in CANONICAL.items():
    if _svc not in NEPTUNE_TO_DEPLOYMENT:
        NEPTUNE_TO_DEPLOYMENT[_svc] = _dep

# Neptune 服务名  →  K8s Pod app label（用于 kubectl / K8s API 查询）
NEPTUNE_TO_K8S_LABEL: dict[str, str] = {}
for _name, _cfg in _profile.get("services", {}).items():
    neptune = _cfg.get("neptune_name", _name)
    k8s_label = _cfg.get("k8s_label", _cfg.get("k8s_deployment", _name))
    NEPTUNE_TO_K8S_LABEL[neptune] = k8s_label

# ── Feature Flags ─────────────────────────────────────────────────────────────
# 控制新功能的开关，便于逐步灰度上线或紧急回滚。
# 取值与 rca/config.py 保持一致（两处都读，必须同步）。
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

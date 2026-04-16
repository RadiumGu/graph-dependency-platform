"""
config.py - 全局服务名规范映射

K8s Deployment 名（DeepFlow request_domain 前缀）与 Neptune 服务名之间的
唯一权威映射表。rca_engine.py 和 action_executor.py 均从此处导入，
不再各自维护独立的映射字典。

数据源说明：
  - CANONICAL 是主映射（deployment → neptune_name）
  - 少数 deployment 名存在别名（如 pethistory 有两个 deployment 名映射到同一服务），
    在 CANONICAL 中均列出。
  - NEPTUNE_TO_DEPLOYMENT 是反向映射（neptune_name → 首选 deployment 名），
    自动从 CANONICAL 派生，以首次出现的 entry 为准。
"""

# K8s Deployment 名 / DeepFlow 服务前缀  →  Neptune 服务名
CANONICAL: dict[str, str] = {
    'petsite-deployment':    'petsite',
    'service-petsite':       'petsite',
    'search-service':        'petsearch',
    'pay-for-adoption':      'payforadoption',
    'list-adoptions':        'petlistadoptions',
    'pethistory-deployment': 'petadoptionshistory',
    'pethistory-service':    'petadoptionshistory',
    'petfood':               'petfood',
    'traffic-generator':     'trafficgenerator',
}

# Neptune 服务名  →  首选 K8s Deployment 名（取 CANONICAL 中每个 neptune_name 的第一条）
NEPTUNE_TO_DEPLOYMENT: dict[str, str] = {}
for _dep, _svc in CANONICAL.items():
    if _svc not in NEPTUNE_TO_DEPLOYMENT:
        NEPTUNE_TO_DEPLOYMENT[_svc] = _dep

# ── Feature Flags ─────────────────────────────────────────────────────────────
# 控制新功能的开关，便于逐步灰度上线或紧急回滚。
# 所有新功能默认关闭（False），在验证稳定后再逐步开启。
#
# alert_buffer_enabled      — 告警聚合缓冲（Phase 4），False=直通原有 RCA 流程
# topology_correlation_enabled — 拓扑关联分组（Phase 4），False=每条告警独立成组
# feedback_enabled          — Slack 反馈收集（Phase 4），False=不显示反馈按钮
# auto_remediation_enabled  — 自动修复执行（Phase 4），False=仅建议不自动执行
# p0_bypass_buffer          — P0 告警跳过缓冲（Phase 4），True=P0 立即处理
FEATURE_FLAGS: dict[str, bool] = {
    'alert_buffer_enabled':          True,
    'topology_correlation_enabled':  True,
    'feedback_enabled':              False,
    'auto_remediation_enabled':      False,
    'p0_bypass_buffer':              True,
}

# Neptune 服务名  →  K8s Pod app label（用于 kubectl / K8s API 查询）
# 大部分情况下和 deployment 名一致，少数需要特殊映射
NEPTUNE_TO_K8S_LABEL: dict[str, str] = {
    'petsite':              'petsite',
    'petsearch':            'search-service',
    'payforadoption':       'pay-for-adoption',
    'petlistadoptions':     'list-adoptions',
    'petadoptionshistory':  'pethistory',
    'petfood':              'petfood',
    'trafficgenerator':     'traffic-generator',
}

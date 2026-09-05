"""
config.py — DR Plan Generator 集中配置

All configuration is read from environment variables with sensible defaults.

刻意不 import 父仓库的 ``shared`` / ``profiles``：本模块要能作为独立制品交付，
反向 ``sys.path.insert(0, '..')`` 会让它只能在父仓库树里运行。
Region 解析顺序与原 ``shared.get_region()`` 保持一致，实现搬进来。
"""

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_REGION = "ap-northeast-1"


def get_region() -> str:
    """统一获取 AWS Region。

    优先级：
    1. 环境变量 ``REGION``（显式 override）
    2. active profile 的 ``aws_resources.primary_region``
    3. 环境变量 ``AWS_DEFAULT_REGION``
    4. 默认 ``ap-northeast-1``

    第 2 步刻意用 try/except 包住：profile 未配置是**合法状态**
    （例如只跑 ``validate`` 子命令时不需要 profile），此处不应因此炸掉导入。

    Returns:
        Region 名称。
    """
    if "REGION" in os.environ:
        return os.environ["REGION"]
    try:
        from dr_profile import get_active_profile

        region = get_active_profile().region
        if region:
            return region
    except Exception:
        pass
    return os.environ.get("AWS_DEFAULT_REGION", _DEFAULT_REGION)


# Neptune connection
NEPTUNE_ENDPOINT: str = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT: int = int(os.environ.get("NEPTUNE_PORT", "8182"))

# AWS region
REGION: str = get_region()

# Bedrock LLM for optional summary generation
BEDROCK_MODEL: str = os.environ.get(
    "BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"
)

# Plan output directory
PLANS_DIR: str = os.environ.get("PLANS_DIR", "plans")

# Graph freshness threshold in seconds (warn if older than this)
GRAPH_FRESHNESS_THRESHOLD_SECONDS: int = int(
    os.environ.get("GRAPH_FRESHNESS_THRESHOLD_SECONDS", "3600")
)

# RDS replication lag threshold in ms (block switchover if exceeded)
REPLICATION_LAG_THRESHOLD_MS: int = int(
    os.environ.get("REPLICATION_LAG_THRESHOLD_MS", "1000")
)

# ---------------------------------------------------------------------------
# DR artefact storage
#
# Plans and graph snapshots MUST be readable while the primary Region is down.
# Point this at a bucket with cross-Region replication enabled (or a bucket in
# the recovery Region). Keeping artefacts only on local disk in the primary
# Region reproduces the very dependency DR is meant to remove.
# ---------------------------------------------------------------------------
PLAN_ARTIFACT_BUCKET: str = os.environ.get("PLAN_ARTIFACT_BUCKET", "")

# Where snapshots are written/read by default.
SNAPSHOT_DIR: str = os.environ.get("SNAPSHOT_DIR", "snapshots")

# Snapshot age (seconds) beyond which a WARNING is emitted. Deliberately NOT a
# hard gate: during a real disaster the freshest snapshot on hand may be hours
# old, and refusing to plan then would defeat the purpose. Default 24h.
SNAPSHOT_MAX_AGE_SECONDS: int = int(
    os.environ.get("SNAPSHOT_MAX_AGE_SECONDS", "86400")
)

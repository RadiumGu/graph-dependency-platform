"""
config.py — DR Plan Generator 集中配置

All configuration is read from environment variables with sensible defaults.
"""

import os
import sys

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROJECT_ROOT))

from shared import get_region

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

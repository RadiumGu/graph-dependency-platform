"""
export_coverage_snapshots.py — 从 DynamoDB 导出 10 个有代表性的 coverage snapshot fixture。

Golden Set 按 4 类分桶：
  l001-l003: 核心服务（petsite / payforadoption / pethistory）
  l004-l006: 特殊后端（eks-nodegroup / eks-storage / eks-control-plane）
  l007-l008: 错误输入（空 / 畸形 JSON）
  l009-l010: 边界（全量 86 条 / 单条实验）

输出到 experiments/strands-poc/fixtures/coverage_snapshot_l001.json ... l010.json
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

# path setup
_HERE = os.path.dirname(os.path.abspath(__file__))
_CHAOS_CODE = os.path.join(_HERE, "..")
_RCA = os.path.join(_HERE, "..", "..", "..", "rca")
for p in (_CHAOS_CODE, _RCA):
    ap = os.path.abspath(p)
    if ap not in sys.path:
        sys.path.insert(0, ap)

from runner.query import ExperimentQueryClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("export_fixtures")

OUTPUT_DIR = os.path.join(_HERE, "..", "..", "..", "experiments", "strands-poc", "fixtures")
os.makedirs(OUTPUT_DIR, exist_ok=True)

client = ExperimentQueryClient()


def fetch_all() -> list[dict]:
    items = []
    for status in ("PASSED", "FAILED", "ABORTED"):
        items.extend(client.list_by_status(status, days=180))
    return items


def fetch_by_service(service: str, limit: int = 50) -> list[dict]:
    return client.list_by_service(service, days=180, limit=limit)


def write_fixture(fixture_id: str, items: list[dict], description: str):
    path = os.path.join(OUTPUT_DIR, f"coverage_snapshot_{fixture_id}.json")
    data = {
        "id": fixture_id,
        "description": description,
        "experiment_count": len(items),
        "experiments": items,
    }
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"✅ {fixture_id}: {len(items)} experiments → {path}")


def main():
    all_items = fetch_all()
    logger.info(f"Total experiments: {len(all_items)}")

    # l001-l003: 核心服务
    for fixture_id, svc in [("l001", "petsite"), ("l002", "payforadoption"), ("l003", "pethistory")]:
        items = fetch_by_service(svc)
        write_fixture(fixture_id, items, f"核心服务 {svc} 的实验历史")

    # l004-l006: 特殊后端
    for fixture_id, svc in [("l004", "eks-nodegroup"), ("l005", "eks-storage"), ("l006", "eks-control-plane")]:
        items = fetch_by_service(svc)
        write_fixture(fixture_id, items, f"特殊后端 {svc} 的实验历史")

    # l007: 空 snapshot
    write_fixture("l007", [], "错误输入：空实验列表")

    # l008: 畸形数据（模拟缺关键字段的记录）
    malformed = [
        {"experiment_id": {"S": "bad-001"}, "status": {"S": "UNKNOWN"}},
        {"experiment_id": {"S": "bad-002"}},  # 缺 target_service
        {"target_service": {"S": "ghost"}, "status": {"S": "FAILED"}, "fault_type": {"S": "invalid_type"}},
    ]
    write_fixture("l008", malformed, "错误输入：畸形 JSON / 缺字段")

    # l009: 全量（边界：100+ 实验聚合）
    write_fixture("l009", all_items, f"边界：全量 {len(all_items)} 条实验")

    # l010: 单条实验
    if all_items:
        write_fixture("l010", [all_items[0]], "边界：仅 1 条实验")
    else:
        write_fixture("l010", [], "边界：仅 1 条实验（实际为空）")

    logger.info("🎉 All 10 fixtures exported.")


if __name__ == "__main__":
    main()

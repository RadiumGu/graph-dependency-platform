"""
neptune_sync.py - 将混沌实验结果同步到 Neptune 图谱（Phase A2）

写入 ChaosExperiment 节点，并建立 Microservice -[:TestedBy]-> ChaosExperiment 边。
使用 runner/neptune_client.py 的 query_opencypher（SigV4 认证，openCypher）。
所有写入使用 MERGE 保证幂等性。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def write_experiment(experiment: dict[str, Any]) -> None:
    """将混沌实验记录写入 Neptune。

    幂等写入：MERGE on experiment_id，已存在则更新 result/recovery/degradation。
    同时建立 Microservice -[:TestedBy]-> ChaosExperiment 边（MERGE）。

    Args:
        experiment: 实验数据字典，必须包含 experiment_id, target_service。
                    可选字段：fault_type, result, recovery_time_sec,
                              degradation_rate, timestamp。
    """
    from runner.neptune_client import query_opencypher

    exp_id = experiment.get('experiment_id', '')
    service = experiment.get('target_service', '')
    fault_type = experiment.get('fault_type', '').replace("'", "\\'")
    result = experiment.get('result', '').replace("'", "\\'")
    recovery_time = int(experiment.get('recovery_time_sec') or 0)
    degradation = float(experiment.get('degradation_rate') or 0.0)
    timestamp = str(experiment.get('timestamp', '')).replace("'", "\\'")

    if not exp_id or not service:
        logger.warning("neptune_sync.write_experiment: experiment_id or target_service missing, skipping")
        return

    # 幂等写入 ChaosExperiment 节点
    node_cypher = (
        f"MERGE (exp:ChaosExperiment {{experiment_id: '{exp_id}'}})"
        f" ON CREATE SET"
        f"  exp.fault_type = '{fault_type}',"
        f"  exp.result = '{result}',"
        f"  exp.recovery_time_sec = {recovery_time},"
        f"  exp.degradation_rate = {degradation},"
        f"  exp.timestamp = '{timestamp}'"
        f" ON MATCH SET"
        f"  exp.result = '{result}',"
        f"  exp.recovery_time_sec = {recovery_time},"
        f"  exp.degradation_rate = {degradation}"
    )
    try:
        query_opencypher(node_cypher)
        logger.info(f"ChaosExperiment node upserted: {exp_id}")
    except Exception as e:
        logger.error(f"Failed to upsert ChaosExperiment node {exp_id}: {e}")
        raise

    # 建立 Microservice -[:TestedBy]-> ChaosExperiment 边
    edge_cypher = (
        f"MATCH (svc:Microservice {{name: '{service}'}})"
        f" MATCH (exp:ChaosExperiment {{experiment_id: '{exp_id}'}})"
        f" MERGE (svc)-[:TestedBy]->(exp)"
    )
    try:
        query_opencypher(edge_cypher)
        logger.info(f"TestedBy edge created: {service} -> {exp_id}")
    except Exception as e:
        logger.warning(f"Failed to create TestedBy edge {service} -> {exp_id}: {e}")

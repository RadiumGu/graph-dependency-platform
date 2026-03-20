"""
query.py - DynamoDB 查询层

所有 chaos-experiments 表的读操作统一收口在此模块。
设计原则：只用 Query 走 GSI，禁止 Scan（避免全表扫描 + IAM 最小权限原则）。

对应 GSI：
  GSI-1  target_service-start_time-index   → 按服务查历史（FMEA / CLI history）
  GSI-2  status-start_time-index           → 按状态查熔断（护栏分析）
  GSI-3  experiment_name-start_time-index  → 按实验名查趋势（多次执行对比）

调用方：fmea.py / main.py CLI history 命令
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "chaos-experiments")
REGION     = "ap-northeast-1"
UTC        = timezone.utc


class ExperimentQueryClient:
    """
    chaos-experiments 表的所有查询入口。
    使用低级 DynamoDB client（与 report.py 一致，{"S": value} 格式）。
    """

    def __init__(self):
        self._ddb = None

    @property
    def ddb(self):
        if self._ddb is None:
            self._ddb = boto3.client("dynamodb", region_name=REGION)
        return self._ddb

    # ── 直接按 PK 查单条 ────────────────────────────────────────────

    def get_experiment(self, experiment_id: str, start_time: Optional[str] = None) -> Optional[dict]:
        """
        按主键精确查询单条实验记录。

        Args:
            experiment_id: 实验 ID（PK），格式 exp-{service}-{fault_type}-{yyyyMMdd-HHmmss}
            start_time:    可选，实验开始时间 ISO8601（SK）；若不提供则通过 GSI-1 推断

        Returns:
            DynamoDB item dict，或 None（不存在时）
        """
        if start_time:
            resp = self.ddb.get_item(
                TableName=TABLE_NAME,
                Key={
                    "experiment_id": {"S": experiment_id},
                    "start_time":    {"S": start_time},
                },
            )
            return resp.get("Item")

        # 未提供 start_time：通过 experiment_name 关联回查（从 ID 提取服务名）
        # experiment_id 格式: exp-{service}-{fault_type}-{yyyyMMdd-HHmmss}
        # fallback: 用 target_service GSI-1 + 客户端过滤
        parts = experiment_id.split("-")
        if len(parts) >= 2:
            service = parts[1]  # exp-{service}-...
            items = self.list_by_service(service, days=365, limit=200)
            for item in items:
                if item.get("experiment_id", {}).get("S") == experiment_id:
                    return item
        return None

    def get(self, experiment_id: str, start_time: str) -> Optional[dict]:
        """按 PK+SK 精确查单条（用于报告回查、RCA 关联等）"""
        resp = self.ddb.get_item(
            TableName=TABLE_NAME,
            Key={
                "experiment_id": {"S": experiment_id},
                "start_time":    {"S": start_time},
            },
        )
        return resp.get("Item")

    # ── GSI-1: 按服务查历史 ─────────────────────────────────────────

    def list_by_service(self, service: str, days: int = 90,
                        limit: int = 50) -> list[dict]:
        """
        查询某服务的全部历史实验，按时间倒序。

        用途：CLI history 命令 / FMEA _calc_occurrence()
        走：target_service-start_time-index（GSI-1）

        Args:
            service: 目标服务名（精确匹配，非模糊）
            days:    查询最近多少天的记录
            limit:   最多返回条数

        Returns:
            list of DynamoDB item dicts（低级格式）
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        resp = self.ddb.query(
            TableName=TABLE_NAME,
            IndexName="target_service-start_time-index",
            KeyConditionExpression="target_service = :s AND start_time >= :t",
            ExpressionAttributeValues={
                ":s": {"S": service},
                ":t": {"S": since},
            },
            ScanIndexForward=False,  # 最新的排前面
            Limit=limit,
        )
        return resp.get("Items", [])

    def list_experiments(self, service: Optional[str] = None,
                         days: int = 90, limit: int = 50) -> list[dict]:
        """
        list_by_service 的别名，提供更通用的入口。
        若 service 为 None 则不能查询（不允许 Scan），返回空列表并记录警告。
        """
        if not service:
            logger.warning("list_experiments: service 不能为空，Scan 已禁用")
            return []
        return self.list_by_service(service, days=days, limit=limit)

    # ── GSI-2: 按状态查熔断 ─────────────────────────────────────────

    def list_by_status(self, status: str, days: int = 30,
                       service_filter: Optional[str] = None) -> list[dict]:
        """
        查询所有指定状态的实验（护栏触发分析 / 本月所有 ABORTED 实验）。

        走：status-start_time-index（GSI-2）
        注：status 是 DynamoDB 保留字，需用 ExpressionAttributeNames。

        Args:
            status:         "PASSED" | "FAILED" | "ABORTED" | "ERROR"
            days:           查询最近多少天
            service_filter: 可选服务名过滤（客户端二次过滤）

        Returns:
            list of DynamoDB item dicts
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        resp = self.ddb.query(
            TableName=TABLE_NAME,
            IndexName="status-start_time-index",
            KeyConditionExpression="#st = :s AND start_time >= :t",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":t": {"S": since},
            },
            ScanIndexForward=False,
        )
        items = resp.get("Items", [])
        if service_filter:
            items = [
                i for i in items
                if i.get("target_service", {}).get("S") == service_filter
            ]
        return items

    # ── GSI-3: 按实验名查趋势 ───────────────────────────────────────

    def list_by_experiment_name(self, name: str, limit: int = 20) -> list[dict]:
        """
        查询同一实验名的历次结果（FMEA 趋势 / 报告聚合）。

        走：experiment_name-start_time-index（GSI-3）

        Args:
            name:  实验名（YAML 中的 name 字段）
            limit: 最多返回条数

        Returns:
            list of DynamoDB item dicts，按时间倒序
        """
        resp = self.ddb.query(
            TableName=TABLE_NAME,
            IndexName="experiment_name-start_time-index",
            KeyConditionExpression="experiment_name = :n",
            ExpressionAttributeValues={":n": {"S": name}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return resp.get("Items", [])

    # ── 便捷方法 ────────────────────────────────────────────────────

    def get_latest_result(self, service: str) -> Optional[dict]:
        """
        获取某服务最近一次实验记录。

        Args:
            service: 目标服务名

        Returns:
            最近一条 DynamoDB item，或 None
        """
        items = self.list_by_service(service, days=365, limit=1)
        return items[0] if items else None

    def list_results(self, service: str, limit: int = 20) -> list[dict]:
        """
        获取某服务的实验结果列表（最近 N 条）。

        Args:
            service: 目标服务名
            limit:   最多返回条数

        Returns:
            list of DynamoDB item dicts
        """
        return self.list_by_service(service, days=365, limit=limit)

    # ── FMEA 专用 ───────────────────────────────────────────────────

    def calc_failure_rate(self, service: str, days: int = 90) -> Optional[float]:
        """
        计算某服务历史实验的失败率（FMEA _calc_occurrence 专用）。

        返回 0.0~100.0 的失败率百分比。
        无历史实验记录时返回 None（调用方应 fallback 到 DeepFlow 自然错误率）。

        Args:
            service: 目标服务名
            days:    统计周期（天）

        Returns:
            失败率 0.0-100.0，或 None（无历史记录）
        """
        items = self.list_by_service(service, days=days, limit=200)
        if not items:
            return None
        total  = len(items)
        failed = sum(
            1 for i in items
            if i.get("status", {}).get("S") in ("FAILED", "ABORTED")
        )
        return round(failed / total * 100, 1)

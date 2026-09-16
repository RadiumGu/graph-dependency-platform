"""
alert_buffer.py - 告警聚合缓冲层

职责：
1. 非 P0 告警写入 DynamoDB gp-alert-buffer 表，2 分钟窗口内去重聚合
2. P0 告警 bypass（由调用方决定，不写缓冲）
3. 窗口到期时，由 EventBridge Scheduler 调用 window_flush_handler；
   本模块提供 flush_window() 方法供其读取并清空当前窗口

DynamoDB 表结构（gp-alert-buffer）：
  PK: fingerprint (S)       — 告警指纹（去重键）
  SK: window_id  (S)        — 窗口 ID（ISO 分钟截断，e.g. "2026-04-05T10:04"）
  TTL: ttl       (N)        — 5 分钟 TTL，避免遗留数据
  属性:
    alert_json   (S)        — UnifiedAlertEvent JSON
    service_name (S)        — 冗余存储，便于 GSI 过滤（可选）
    created_at   (S)        — ISO timestamp
    count        (N)        — 同指纹事件计数（去重用）
"""
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

from shared import get_region
REGION = get_region()
# 环境变量命名兼容：部署脚本(deploy.sh)与 CDK(AlertBufferStack)历史上用了
# BUFFER_TABLE_NAME / WINDOW_FLUSH_FUNCTION_ARN，而本模块原先只读
# ALERT_BUFFER_TABLE / WINDOW_FLUSH_LAMBDA_ARN，导致定时器被静默跳过
# （_schedule_flush 的跳过日志是 debug 级，生产不可见）。此处同时接受两种名称。
TABLE_NAME = (os.environ.get('ALERT_BUFFER_TABLE')
              or os.environ.get('BUFFER_TABLE_NAME')
              or 'gp-alert-buffer')
SCHEDULER_ROLE_ARN = os.environ.get('SCHEDULER_ROLE_ARN', '')
FLUSH_LAMBDA_ARN = (os.environ.get('WINDOW_FLUSH_LAMBDA_ARN')
                    or os.environ.get('WINDOW_FLUSH_FUNCTION_ARN')
                    or '')

BUFFER_WINDOW_SECONDS: int = 120   # 2 分钟缓冲窗口
P0_BYPASS_BUFFER: bool = True       # P0 告警不进缓冲，直接处理
_ALERT_TTL_SECONDS: int = 300       # DynamoDB TTL：5 分钟


def _window_id() -> str:
    """当前 2 分钟窗口 ID（ISO 格式，分钟截断到偶数）。

    例：2026-04-05T10:04（每两分钟产生一个新 ID）

    Returns:
        窗口 ID 字符串
    """
    now = datetime.now(timezone.utc)
    # 截断到 BUFFER_WINDOW_SECONDS 的倍数
    minute = (now.minute // (BUFFER_WINDOW_SECONDS // 60)) * (BUFFER_WINDOW_SECONDS // 60)
    aligned = now.replace(minute=minute, second=0, microsecond=0)
    return aligned.strftime('%Y-%m-%dT%H:%M')


def _ttl() -> int:
    """DynamoDB TTL 时间戳（当前时间 + _ALERT_TTL_SECONDS）。"""
    return int(time.time()) + _ALERT_TTL_SECONDS


class AlertBuffer:
    """DynamoDB 告警缓冲表操作封装。

    Usage:
        buf = AlertBuffer()
        buf.put_alert(event)          # 写入缓冲
        alerts = buf.flush_window()   # 取出并清空当前窗口
    """

    def __init__(self) -> None:
        self._ddb = boto3.resource('dynamodb', region_name=REGION)
        self._table = self._ddb.Table(TABLE_NAME)
        self._scheduler = boto3.client('scheduler', region_name=REGION)

    # ── 写入 ─────────────────────────────────────────────────────────────────

    def put_alert(self, event: 'UnifiedAlertEvent') -> bool:
        """将 UnifiedAlertEvent 写入缓冲表（fingerprint 去重）。

        同一 fingerprint 在同一 window_id 内只保留第一条，后续仅递增 count。

        Args:
            event: 标准化告警事件

        Returns:
            True 表示首次写入（新事件），False 表示已存在（去重命中）
        """
        from dataclasses import asdict
        wid = _window_id()
        alert_json = json.dumps(asdict(event), ensure_ascii=False, default=str)

        try:
            self._table.put_item(
                Item={
                    # 键名必须与表 schema 一致：CDK(alert-buffer-stack.ts) 定义
                    # partitionKey=window_id, sortKey=alert_fingerprint。
                    'alert_fingerprint': event.fingerprint,
                    'window_id': wid,
                    'alert_json': alert_json,
                    'service_name': event.service_name,
                    'severity': event.severity,
                    'created_at': event.start_time,
                    'count': 1,
                    'ttl': _ttl(),
                },
                # 仅在 PK 不存在时写入（幂等去重）
                ConditionExpression='attribute_not_exists(alert_fingerprint)',
            )
            logger.info(f"AlertBuffer: put {event.fingerprint[:8]}... svc={event.service_name} window={wid}")
            # 触发窗口定时器（首次写入时）
            self._schedule_flush(wid)
            return True
        except self._ddb.meta.client.exceptions.ConditionalCheckFailedException:
            # 同 fingerprint 已存在，仅递增计数
            try:
                self._table.update_item(
                    Key={'window_id': wid, 'alert_fingerprint': event.fingerprint},
                    UpdateExpression='ADD #cnt :one',
                    ExpressionAttributeNames={'#cnt': 'count'},
                    ExpressionAttributeValues={':one': 1},
                )
            except Exception as e:
                logger.warning(f"AlertBuffer count increment failed: {e}")
            return False
        except Exception as e:
            # 写入失败必须让调用方可区分「去重命中」与「真失败」，否则告警会被静默丢弃。
            logger.error(f"AlertBuffer put_item failed: {e}")
            raise

    # ── 读取 & 清空 ───────────────────────────────────────────────────────────

    def flush_window(self, window_id: Optional[str] = None) -> list:
        """读取指定窗口内所有告警，并删除（幂等消费）。

        Args:
            window_id: 要 flush 的窗口 ID；None 表示上一个完成的窗口

        Returns:
            UnifiedAlertEvent 列表（反序列化自 DDB alert_json）
        """
        from core.event_normalizer import UnifiedAlertEvent

        if window_id is None:
            # 取"刚刚结束"的窗口（比当前窗口早一个周期）
            now = datetime.now(timezone.utc)
            window_seconds = BUFFER_WINDOW_SECONDS
            minute = (now.minute // (window_seconds // 60)) * (window_seconds // 60)
            current_start = now.replace(minute=minute, second=0, microsecond=0)
            prev_start = current_start - timedelta(seconds=window_seconds)
            window_id = prev_start.strftime('%Y-%m-%dT%H:%M')

        logger.info(f"AlertBuffer: flushing window={window_id}")

        # Scan by window_id（小表，不需要 GSI）
        try:
            resp = self._table.scan(
                FilterExpression=boto3.dynamodb.conditions.Attr('window_id').eq(window_id)
            )
        except Exception as e:
            logger.error(f"AlertBuffer scan failed: {e}")
            return []

        items = resp.get('Items', [])
        events: list = []
        delete_keys: list[dict] = []

        for item in items:
            try:
                data = json.loads(item['alert_json'])
                ev = UnifiedAlertEvent(**{
                    k: v for k, v in data.items()
                    if k in UnifiedAlertEvent.__dataclass_fields__
                })
                events.append(ev)
                delete_keys.append({
                    'window_id': item['window_id'],
                    'alert_fingerprint': item['alert_fingerprint'],
                })
            except Exception as e:
                logger.warning(f"AlertBuffer deserialize failed: {e} item={item.get('alert_fingerprint','?')}")

        # 批量删除（已消费）
        if delete_keys:
            with self._table.batch_writer() as batch:
                for key in delete_keys:
                    batch.delete_item(Key=key)
            logger.info(f"AlertBuffer: flushed {len(events)} alerts from window={window_id}")

        return events

    # ── 定时器 ────────────────────────────────────────────────────────────────

    def _schedule_flush(self, window_id: str) -> None:
        """用 EventBridge Scheduler 创建一次性定时器，触发 window_flush_handler。

        若 FLUSH_LAMBDA_ARN 未配置则跳过（本地测试 / 手动触发模式）。

        Args:
            window_id: 当前窗口 ID（YYYY-MM-DDTHH:MM）
        """
        if not FLUSH_LAMBDA_ARN:
            # 提升为 warning：缺这个 ARN 会导致窗口永不 flush，缓冲的告警只能等 TTL 过期，
            # 等于静默丢弃。原先是 debug 级，生产日志里看不到。
            logger.warning(
                "AlertBuffer: WINDOW_FLUSH_LAMBDA_ARN/WINDOW_FLUSH_FUNCTION_ARN not set — "
                "window will NEVER be flushed, buffered alerts will expire via TTL"
            )
            return

        # 调度时间 = 窗口结束时间 + 5 秒缓冲
        try:
            window_end_dt = datetime.strptime(window_id, '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)
            trigger_time = window_end_dt + timedelta(seconds=BUFFER_WINDOW_SECONDS + 5)
            schedule_expr = f"at({trigger_time.strftime('%Y-%m-%dT%H:%M:%S')})"
            schedule_name = f"gp-flush-{window_id.replace(':', '-').replace('T', '-')}"

            scheduler_kwargs: dict = {
                'Name': schedule_name,
                'ScheduleExpression': schedule_expr,
                'ScheduleExpressionTimezone': 'UTC',
                'FlexibleTimeWindow': {'Mode': 'OFF'},
                'Target': {
                    'Arn': FLUSH_LAMBDA_ARN,
                    'Input': json.dumps({'window_id': window_id}),
                },
                'ActionAfterCompletion': 'DELETE',
            }
            if SCHEDULER_ROLE_ARN:
                scheduler_kwargs['Target']['RoleArn'] = SCHEDULER_ROLE_ARN

            self._scheduler.create_schedule(**scheduler_kwargs)
            logger.info(f"AlertBuffer: scheduled flush at {trigger_time.isoformat()} for window={window_id}")
        except self._scheduler.exceptions.ConflictException:
            # 同一窗口已有调度，忽略
            logger.debug(f"Scheduler for window={window_id} already exists")
        except Exception as e:
            logger.warning(f"AlertBuffer scheduler failed (non-fatal): {e}")

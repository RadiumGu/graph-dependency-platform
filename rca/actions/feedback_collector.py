"""
feedback_collector.py - 用户反馈收集与 Neptune 回写（Phase 4 闭环层）

用途：接收 SRE 对 RCA 报告的反馈（确认根因、否定根因、标记误报等），
将反馈写回 Neptune Incident 节点，形成知识闭环，持续改善 RCA 质量。

反馈来源：
  - Slack 交互回调（Slack Action Payload）
  - Lambda 直接调用（测试 / API Gateway）

反馈类型（FEEDBACK_BUTTONS）：
  confirm   — 确认根因正确
  deny      — 否定根因（根因判断有误）
  supplement — 补充信息（根因不完整）
  false_positive — 标记为误报（这不是真正的故障）
"""
import logging
import os
import time
from typing import Optional

import boto3

logger = logging.getLogger(__name__)
from shared import get_region
REGION = get_region()

# Slack 交互回调中的反馈按钮定义
FEEDBACK_BUTTONS: list[dict] = [
    {
        'action_id': 'confirm_rca',
        'type': 'button',
        'text': '✅ 确认根因',
        'value': 'confirm',
        'style': 'primary',
    },
    {
        'action_id': 'deny_rca',
        'type': 'button',
        'text': '❌ 根因有误',
        'value': 'deny',
        'style': 'danger',
    },
    {
        'action_id': 'supplement_rca',
        'type': 'button',
        'text': '📝 补充信息',
        'value': 'supplement',
    },
    {
        'action_id': 'false_positive',
        'type': 'button',
        'text': '🚫 标记误报',
        'value': 'false_positive',
    },
]

# 反馈类型 → Neptune Incident 节点字段映射
_FEEDBACK_FIELD_MAP: dict[str, str] = {
    'confirm':        'feedback_confirmed',
    'deny':           'feedback_denied',
    'supplement':     'feedback_supplemented',
    'false_positive': 'feedback_false_positive',
}


class FeedbackCollector:
    """处理用户反馈并回写 Neptune Incident 节点。

    Usage:
        collector = FeedbackCollector()
        result = collector.handle_feedback({
            'incident_id': 'inc-2026-04-05-abc123',
            'feedback_type': 'confirm',
            'user': 'alice@example.com',
            'comment': '确实是 DynamoDB 限流导致的',
        })
    """

    def handle_feedback(self, payload: dict) -> dict:
        """处理反馈 payload，写回 Neptune。

        Args:
            payload: 反馈数据字典，支持以下格式：
              Slack callback 格式（action_id + value）或
              直接格式（incident_id + feedback_type + user + comment）

        Returns:
            处理结果字典，含 incident_id, feedback_type, success, message
        """
        # 统一解析 payload
        parsed = self._parse_payload(payload)
        if not parsed:
            return {'success': False, 'message': 'invalid payload: missing incident_id or feedback_type'}

        incident_id = parsed['incident_id']
        feedback_type = parsed['feedback_type']
        user = parsed.get('user', 'unknown')
        comment = parsed.get('comment', '')

        if feedback_type not in _FEEDBACK_FIELD_MAP:
            return {
                'success': False,
                'message': f"unknown feedback_type: {feedback_type}. "
                           f"Valid: {list(_FEEDBACK_FIELD_MAP.keys())}",
            }

        # 写回 Neptune
        success = self._write_to_neptune(incident_id, feedback_type, user, comment)

        # 如果确认或误报，额外更新 Incident 状态
        if success and feedback_type == 'false_positive':
            self._mark_false_positive(incident_id, user)
        elif success and feedback_type == 'confirm':
            self._mark_confirmed(incident_id, user)

        result = {
            'success': success,
            'incident_id': incident_id,
            'feedback_type': feedback_type,
            'user': user,
            'message': 'feedback recorded' if success else 'neptune write failed',
        }
        logger.info(f"Feedback: {incident_id} {feedback_type} by {user}: {result['message']}")
        return result

    # ── Slack payload 解析 ────────────────────────────────────────────────────

    @staticmethod
    def _parse_payload(payload: dict) -> Optional[dict]:
        """从 Slack callback 或直接调用格式中提取标准字段。

        Args:
            payload: 原始 payload

        Returns:
            标准化字段字典，或 None（解析失败）
        """
        # 直接格式（测试 / API）
        if 'incident_id' in payload and 'feedback_type' in payload:
            return payload

        # Slack callback 格式
        # payload.actions[0].action_id → 映射到 feedback_type
        actions = payload.get('actions', [])
        if not actions:
            return None

        action = actions[0]
        action_id = action.get('action_id', '')
        value = action.get('value', '')

        # 从 action_id 或 value 推断 feedback_type
        feedback_type = None
        for btn in FEEDBACK_BUTTONS:
            if btn['action_id'] == action_id or btn['value'] == value:
                feedback_type = btn['value']
                break

        if not feedback_type:
            return None

        # 从 Slack payload 中提取 incident_id（存在 block_id 或 callback_id）
        incident_id = (
            payload.get('callback_id')
            or payload.get('view', {}).get('callback_id', '')
            or _extract_incident_id_from_blocks(payload)
        )
        if not incident_id:
            return None

        user_info = payload.get('user', {})
        user = user_info.get('username') or user_info.get('name') or user_info.get('id', 'slack_user')

        comment = ''
        # 如果是 supplement，从 state values 中提取文本
        state = payload.get('state', {}).get('values', {})
        for block_vals in state.values():
            for element_val in block_vals.values():
                if element_val.get('type') == 'plain_text_input':
                    comment = element_val.get('value', '')
                    break

        return {
            'incident_id': incident_id,
            'feedback_type': feedback_type,
            'user': user,
            'comment': comment,
        }

    # ── Neptune 写入 ─────────────────────────────────────────────────────────

    def _write_to_neptune(self, incident_id: str, feedback_type: str,
                          user: str, comment: str) -> bool:
        """将反馈写入 Neptune Incident 节点。

        Args:
            incident_id: Incident 节点 ID
            feedback_type: confirm | deny | supplement | false_positive
            user: 操作用户名
            comment: 补充说明文本

        Returns:
            True 表示写入成功
        """
        from neptune import neptune_client as nc

        field = _FEEDBACK_FIELD_MAP.get(feedback_type, 'feedback_unknown')
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

        try:
            nc.results("""
            MATCH (inc:Incident {id: $id})
            SET inc[$field] = true,
                inc.feedback_by = $user,
                inc.feedback_at = $ts,
                inc.feedback_comment = $comment,
                inc.feedback_type = $fb_type
            RETURN inc.id AS id
            """, {
                'id': incident_id,
                'field': field,
                'user': user,
                'ts': now_iso,
                'comment': comment[:500],
                'fb_type': feedback_type,
            })
            logger.info(f"Neptune feedback written: {incident_id} {field}=true by {user}")
            return True
        except Exception as e:
            logger.error(f"Neptune feedback write failed: {incident_id} {e}")
            return False

    def _mark_false_positive(self, incident_id: str, user: str) -> None:
        """将 Incident 标记为误报（status=false_positive）。

        Args:
            incident_id: Incident 节点 ID
            user: 操作用户名
        """
        from neptune import neptune_client as nc
        try:
            nc.results("""
            MATCH (inc:Incident {id: $id})
            SET inc.status = 'false_positive',
                inc.false_positive_by = $user,
                inc.false_positive_at = $ts
            """, {
                'id': incident_id,
                'user': user,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            logger.info(f"Incident marked as false_positive: {incident_id} by {user}")
        except Exception as e:
            logger.warning(f"mark_false_positive failed: {incident_id} {e}")

    def _mark_confirmed(self, incident_id: str, user: str) -> None:
        """将 Incident 标记为人工确认（feedback_confirmed=true）。

        Args:
            incident_id: Incident 节点 ID
            user: 操作用户名
        """
        from neptune import neptune_client as nc
        try:
            nc.results("""
            MATCH (inc:Incident {id: $id})
            SET inc.human_confirmed = true,
                inc.confirmed_by = $user,
                inc.confirmed_at = $ts
            """, {
                'id': incident_id,
                'user': user,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            logger.info(f"Incident confirmed: {incident_id} by {user}")
        except Exception as e:
            logger.warning(f"mark_confirmed failed: {incident_id} {e}")


def _extract_incident_id_from_blocks(payload: dict) -> str:
    """从 Slack block 结构中提取 incident_id。

    Slack block 消息中通常将 incident_id 存放在 block_id 或
    button value 字段中（形如 "inc-2026-04-05-abc123"）。

    Args:
        payload: Slack callback payload

    Returns:
        incident_id 字符串，或空字符串
    """
    import re
    inc_pattern = re.compile(r'inc-\d{4}-\d{2}-\d{2}-[0-9a-f]{6}')

    # 遍历 message blocks
    message = payload.get('message', {})
    for block in message.get('blocks', []):
        block_id = block.get('block_id', '')
        m = inc_pattern.search(block_id)
        if m:
            return m.group(0)
        # 检查 elements 中的 button value
        for elem in block.get('elements', []):
            for sub in elem.get('elements', [elem]):
                val = sub.get('value', '') or sub.get('action_id', '')
                m = inc_pattern.search(val)
                if m:
                    return m.group(0)

    # 在整个 payload JSON 字符串中搜索（最后手段）
    import json
    try:
        payload_str = json.dumps(payload)
        m = inc_pattern.search(payload_str)
        if m:
            return m.group(0)
    except Exception:
        pass

    return ''

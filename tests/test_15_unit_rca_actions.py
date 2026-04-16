"""
test_15_unit_rca_actions.py — Sprint 3 RCA Actions 单元测试

Tests: S3-01 ~ S3-08
全部使用 unittest.mock，不连接真实 AWS / Neptune / Slack。
"""
import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest

# sys.path configured by conftest.py (rca/ → sys.path[0])


# ─── S3-01 + S3-02: slack_notifier ──────────────────────────────────────────

class TestSlackNotifier:

    def test_s3_01_medium_risk_blocks_format(self):
        """S3-01: MEDIUM/DEPENDS 风险 P1 告警 → Block Kit 格式正确，含 section block 和服务名/等级。"""
        import actions.slack_notifier as notifier

        classification = {
            'severity': 'P1',
            'affected_service': 'petsite',
            'signal': {'metric': 'error_rate', 'value': 0.35},
            'affected_capabilities': [{'name': 'pet_adoption'}],
            'strategy': 'Parallel',
        }
        playbook = {
            'risk': 'MEDIUM',
            'playbook_name': 'Pod CrashLoopBackOff',
            'matched_playbook': 'crashloop',
            'steps': ['检查 Pod 日志', '分析错误类型'],
        }

        captured: dict = {}

        def fake_request(method, url, body=None, headers=None):
            captured['payload'] = json.loads(body)
            m = MagicMock()
            m.status = 200
            return m

        with patch.object(notifier, 'http') as mock_http, \
             patch.object(notifier, '_get_interact_url', return_value=''), \
             patch.dict(os.environ, {'SLACK_WEBHOOK_URL': 'https://hooks.slack.com/fake'}):
            mock_http.request.side_effect = fake_request
            result = notifier.notify_fault(classification, playbook)

        assert result is True, "notify_fault should return True on 200"
        assert 'blocks' in captured['payload'], "Payload must contain 'blocks'"
        blocks = captured['payload']['blocks']
        assert len(blocks) >= 2, "Must have at least 2 blocks"
        # First block: section with mrkdwn containing service name and severity
        first = blocks[0]
        assert first['type'] == 'section'
        text = first['text']['text']
        assert 'petsite' in text, "Service name must appear in first block"
        assert 'P1' in text, "Severity must appear in first block"

    def test_s3_02_http_error_returns_false_no_crash(self):
        """S3-02: HTTP 返回 500 时 notify_fault 返回 False，不抛异常（mock 抛 HTTP 错误）。"""
        import actions.slack_notifier as notifier

        classification = {
            'severity': 'P2',
            'affected_service': 'petsearch',
            'signal': {'metric': 'latency_p99', 'value': 3000},
            'affected_capabilities': [],
            'strategy': 'Diagnose-First',
        }
        playbook = {
            'risk': 'LOW',
            'playbook_name': '动态生成',
            'matched_playbook': None,
            'steps': ['Step A'],
        }

        mock_resp = MagicMock()
        mock_resp.status = 500

        with patch.object(notifier, 'http') as mock_http, \
             patch.dict(os.environ, {'SLACK_WEBHOOK_URL': 'https://hooks.slack.com/fake'}):
            mock_http.request.return_value = mock_resp
            # Must not raise, should return False
            result = notifier.notify_fault(classification, playbook)

        assert result is False, "HTTP 500 → notify_fault must return False without raising"


# ─── S3-03 + S3-04: incident_writer ─────────────────────────────────────────

class TestIncidentWriter:

    @staticmethod
    def _nc():
        """Return an already-imported neptune_client module (patched later)."""
        import neptune.neptune_client as nc_mod
        return nc_mod

    def test_s3_03_write_incident_neptune_merge(self):
        """S3-03: write_incident → MERGE Incident 节点 + TriggeredBy 边，返回 incident_id。"""
        nc_mod = self._nc()
        classification = {
            'affected_service': 'petsite',
            'severity': 'P1',
        }
        rca_result = {
            'top_candidate': {'service': 'petsearch', 'confidence': 0.82},
            'all_candidates': [{'service': 'petsearch'}],
        }

        with patch.object(nc_mod, 'results', return_value=[{'id': 'mock'}]) as mock_results:
            from actions.incident_writer import write_incident
            incident_id = write_incident(classification, rca_result)

        assert incident_id.startswith('inc-'), f"Expected 'inc-' prefix, got: {incident_id}"

        # First nc.results call is the MERGE Incident node
        first_call_cypher = mock_results.call_args_list[0][0][0]
        assert 'MERGE' in first_call_cypher
        assert 'Incident' in first_call_cypher

        # Params carry severity and affected_service
        first_params = mock_results.call_args_list[0][0][1]
        assert first_params.get('severity') == 'P1'
        assert first_params.get('svc') == 'petsite'

    def test_s3_04_idempotent_uses_merge_not_bare_create(self):
        """S3-04: 重复调用 write_incident 每次使用 MERGE（幂等），cypher 无裸 CREATE 节点语句。"""
        nc_mod = self._nc()
        classification = {'affected_service': 'payforadoption', 'severity': 'P2'}
        rca_result = {'top_candidate': {'service': 'payforadoption', 'confidence': 0.5}}
        _bare_create = re.compile(r'(?<!\bON\s)\bCREATE\b\s+\(')

        with patch.object(nc_mod, 'results', return_value=[]) as mock_results:
            from actions.incident_writer import write_incident
            id1 = write_incident(classification, rca_result)
            id2 = write_incident(classification, rca_result)

        assert id1 != id2, "Each call must produce a unique incident_id"

        for c in mock_results.call_args_list:
            cypher = c[0][0]
            assert not _bare_create.search(cypher), \
                f"Bare CREATE node found in cypher (use MERGE): {cypher[:80]}"


# ─── S3-05: playbook_engine ──────────────────────────────────────────────────

class TestPlaybookEngine:

    def test_s3_05a_crashloop_matched(self):
        """S3-05a: pod_status=CrashLoopBackOff → 匹配 crashloop playbook，risk=MEDIUM。"""
        from actions.playbook_engine import match

        classification = {
            'affected_service': 'petsite',
            'severity': 'P1',
            'signal': {'metric': 'pod_status', 'value': 'CrashLoopBackOff'},
        }
        result = match(classification)
        assert result['matched_playbook'] == 'crashloop'
        assert result['risk'] == 'MEDIUM'
        assert len(result['steps']) > 0

    def test_s3_05b_db_connection_low_risk_auto(self):
        """S3-05b: rds_connections=0.95 → db_connection_exhausted, risk=LOW, can_auto_exec=True。"""
        from actions.playbook_engine import match

        classification = {
            'affected_service': 'payforadoption',
            'severity': 'P2',
            'signal': {'metric': 'rds_connections', 'value': 0.95},
        }
        result = match(classification)
        assert result['matched_playbook'] == 'db_connection_exhausted'
        assert result['risk'] == 'LOW'
        assert result['can_auto_exec'] is True

    def test_s3_05c_no_match_returns_dynamic(self):
        """S3-05c: 未知 metric → 动态生成通用建议，matched_playbook=None, risk=UNKNOWN。"""
        from actions.playbook_engine import match

        classification = {
            'affected_service': 'petsearch',
            'severity': 'P2',
            'signal': {'metric': 'unknown_metric_xyz', 'value': 0},
        }
        result = match(classification)
        assert result['matched_playbook'] is None
        assert result['risk'] == 'UNKNOWN'
        assert result['can_auto_exec'] is False
        assert len(result['steps']) > 0

    def test_s3_05d_p0_never_auto_exec(self):
        """S3-05d: P0 故障即使 LOW risk playbook 匹配，can_auto_exec 也必须是 False。"""
        from actions.playbook_engine import match

        classification = {
            'affected_service': 'petsite',
            'severity': 'P0',
            'signal': {'metric': 'rds_connections', 'value': 0.97},
        }
        result = match(classification)
        # P0 forces can_auto_exec=False regardless of playbook risk
        assert result.get('can_auto_exec') is False


# ─── S3-06: action_executor ──────────────────────────────────────────────────

class TestActionExecutor:

    def test_s3_06_rollout_restart_dry_run_success(self):
        """S3-06a: rollout_restart dry_run=True → success=True, 跳过 K8s 调用。"""
        from actions.action_executor import rollout_restart

        with patch('actions.action_executor._check_rate_limit', return_value=True), \
             patch('actions.action_executor._audit'):
            result = rollout_restart('petsite', dry_run=True)

        assert result['success'] is True
        assert result['dry_run'] is True
        assert result['action'] == 'rollout_restart'
        assert result['service'] == 'petsite'

    def test_s3_06b_rate_limit_exceeded_blocks_exec(self):
        """S3-06b: rate limit 超限 → success=False, reason=rate_limit_exceeded，不 crash。"""
        from actions.action_executor import rollout_restart

        with patch('actions.action_executor._check_rate_limit', return_value=False):
            result = rollout_restart('petsite')

        assert result['success'] is False
        assert result['reason'] == 'rate_limit_exceeded'

    def test_s3_06c_scale_dry_run(self):
        """S3-06c: scale_deployment dry_run=True → 正确返回 replicas 目标值。"""
        from actions.action_executor import scale_deployment

        with patch('actions.action_executor._check_rate_limit', return_value=True), \
             patch('actions.action_executor._audit'):
            result = scale_deployment('petsite', replicas=4, dry_run=True)

        assert result['success'] is True
        assert result['dry_run'] is True
        assert result['replicas'] == 4


# ─── S3-07: feedback_collector ───────────────────────────────────────────────

class TestFeedbackCollector:

    def test_s3_07_confirm_feedback_written(self):
        """S3-07a: feedback_type=confirm → Neptune 写入，返回 success=True, incident_id 正确。"""
        import neptune.neptune_client as nc_mod
        from actions.feedback_collector import FeedbackCollector

        payload = {
            'incident_id': 'inc-2026-04-15-abc123',
            'feedback_type': 'confirm',
            'user': 'alice',
            'comment': 'DynamoDB 限流确认',
        }

        with patch.object(nc_mod, 'results', return_value=[{'id': 'inc-2026-04-15-abc123'}]):
            collector = FeedbackCollector()
            result = collector.handle_feedback(payload)

        assert result['success'] is True
        assert result['incident_id'] == 'inc-2026-04-15-abc123'
        assert result['feedback_type'] == 'confirm'
        assert result['user'] == 'alice'

    def test_s3_07b_deny_feedback(self):
        """S3-07b: feedback_type=deny → Neptune 写入，返回 success=True。"""
        import neptune.neptune_client as nc_mod
        from actions.feedback_collector import FeedbackCollector

        payload = {
            'incident_id': 'inc-2026-04-15-def456',
            'feedback_type': 'deny',
            'user': 'bob',
            'comment': '根因判断有误',
        }

        with patch.object(nc_mod, 'results', return_value=[]):
            collector = FeedbackCollector()
            result = collector.handle_feedback(payload)

        assert result['success'] is True

    def test_s3_07c_invalid_feedback_type_error(self):
        """S3-07c: 非法 feedback_type → success=False, message 含类型说明。"""
        from actions.feedback_collector import FeedbackCollector

        payload = {
            'incident_id': 'inc-2026-04-15-abc123',
            'feedback_type': 'not_a_real_type',
            'user': 'charlie',
        }
        collector = FeedbackCollector()
        result = collector.handle_feedback(payload)

        assert result['success'] is False
        # Message should mention the issue
        assert result['message']

    def test_s3_07d_missing_fields_returns_error(self):
        """S3-07d: payload 缺少 incident_id → success=False (invalid payload)。"""
        from actions.feedback_collector import FeedbackCollector

        collector = FeedbackCollector()
        result = collector.handle_feedback({'feedback_type': 'confirm'})

        assert result['success'] is False


# ─── S3-08: semi_auto ────────────────────────────────────────────────────────

class TestSemiAuto:

    def test_s3_08_p0_no_auto_exec(self):
        """S3-08a: P0 故障 → 不执行自动操作，mode=suggest, reason=P0_no_auto_exec。"""
        from actions.semi_auto import execute

        classification = {
            'affected_service': 'petsite',
            'severity': 'P0',
            'signal': {'metric': 'rds_connections', 'value': 0.95},
        }
        playbook = {
            'matched_playbook': 'db_connection_exhausted',
            'risk': 'LOW',
        }

        with patch('actions.slack_notifier.notify_fault', return_value=True) as mock_notify:
            result = execute(classification, playbook)

        assert result['mode'] == 'suggest'
        assert result['reason'] == 'P0_no_auto_exec'
        mock_notify.assert_called_once()

    def test_s3_08b_low_risk_semi_auto_executes(self):
        """S3-08b: LOW risk + db_connection_exhausted → 半自动执行 rollout_restart, mode=semi-auto。"""
        from actions.semi_auto import execute

        classification = {
            'affected_service': 'payforadoption',
            'severity': 'P2',
            'signal': {'metric': 'rds_connections', 'value': 0.96},
        }
        playbook = {
            'matched_playbook': 'db_connection_exhausted',
            'risk': 'LOW',
        }

        exec_result = {'success': True, 'action': 'rollout_restart', 'service': 'payforadoption'}

        with patch('actions.action_executor.rollout_restart', return_value=exec_result) as mock_exec, \
             patch('actions.semi_auto._notify_execution'):
            result = execute(classification, playbook)

        assert result['mode'] == 'semi-auto'
        assert result['executed'] == 'db_connection_exhausted'
        assert result['result']['success'] is True
        mock_exec.assert_called_once_with('payforadoption')

    def test_s3_08c_medium_risk_suggest_only(self):
        """S3-08c: MEDIUM risk → 不执行，发 Slack 建议，mode=suggest。"""
        from actions.semi_auto import execute

        classification = {
            'affected_service': 'petsite',
            'severity': 'P1',
            'signal': {'metric': 'pod_status', 'value': 'CrashLoopBackOff'},
        }
        playbook = {
            'matched_playbook': 'crashloop',
            'risk': 'MEDIUM',
        }

        with patch('actions.slack_notifier.notify_fault', return_value=True) as mock_notify:
            result = execute(classification, playbook)

        assert result['mode'] == 'suggest'
        mock_notify.assert_called_once()

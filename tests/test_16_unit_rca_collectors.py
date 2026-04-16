"""
test_16_unit_rca_collectors.py — Sprint 3 RCA Collectors 单元测试

Tests: S3-09 ~ S3-11
全部使用 unittest.mock，不连接真实 AWS / EKS。
"""
import base64
import datetime
import os
from unittest.mock import MagicMock, patch, call

import pytest

# sys.path configured by conftest.py (rca/ → sys.path[0])


# ─── S3-09: infra_collector ──────────────────────────────────────────────────

class TestInfraCollector:

    def test_s3_09_collect_returns_pods_and_databases(self):
        """S3-09a: collect() → mock sub-functions，返回 pods + databases 结构完整。"""
        from collectors.infra_collector import collect

        mock_pods = [
            {
                'name': 'petsite-abc-7d9f-xkz',
                'status': 'Running',
                'restarts': 0,
                'node': 'ip-10-1-1-1.ap-northeast-1.compute.internal',
                'reason': '',
                'az': 'ap-northeast-1a',
            }
        ]
        mock_db = {
            'cluster_id': 'petsite-aurora',
            'status': 'available',
            'connections': 42,
            'cpu_pct': 15.2,
            'freeable_memory_mb': 512,
        }
        db_mapping = [
            {
                'service': 'petsite',
                'db_cluster_id': 'petsite-aurora',
                'dbname': 'petsite-rds',
                'engine': 'aurora-mysql',
            }
        ]

        with patch('collectors.infra_collector.get_pods_for_service', return_value=mock_pods), \
             patch('collectors.infra_collector.get_service_db', return_value=db_mapping), \
             patch('collectors.infra_collector.get_db_metrics', return_value=mock_db):
            result = collect('petsite')

        assert 'pods' in result
        assert len(result['pods']) == 1
        assert result['pods'][0]['name'] == 'petsite-abc-7d9f-xkz'
        assert 'databases' in result
        assert len(result['databases']) == 1
        assert result['databases'][0]['connections'] == 42
        assert result['databases'][0]['dbname'] == 'petsite-rds'

    def test_s3_09b_get_db_metrics_cloudwatch_rds_mocked(self):
        """S3-09b: get_db_metrics → mock boto3 RDS + CloudWatch，返回 status/connections/cpu。"""
        from collectors.infra_collector import get_db_metrics

        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            'Datapoints': [
                {'Average': 87.0, 'Timestamp': datetime.datetime.utcnow()}
            ]
        }

        mock_rds = MagicMock()
        mock_rds.describe_db_clusters.return_value = {
            'DBClusters': [{'Status': 'available'}]
        }
        mock_rds.describe_db_instances.return_value = {
            'DBInstances': [{'DBInstanceStatus': 'available'}]
        }

        def fake_client(service, region_name=None):
            if service == 'cloudwatch':
                return mock_cw
            elif service == 'rds':
                return mock_rds
            return MagicMock()

        with patch('boto3.client', side_effect=fake_client):
            result = get_db_metrics('petsite-aurora-cluster')

        assert result['cluster_id'] == 'petsite-aurora-cluster'
        assert result['status'] == 'available'
        # CloudWatch returned Average=87 for all metrics
        assert result['connections'] == 87

    def test_s3_09c_pod_collection_k8s_fails_gracefully(self):
        """S3-09c: EKS token 获取失败时 get_pods_for_service 返回空列表，不 crash。"""
        from collectors.infra_collector import get_pods_for_service

        with patch('collectors.infra_collector._get_k8s_token', return_value=(None, None, None)):
            result = get_pods_for_service('petsite')

        assert result == []

    def test_s3_09d_format_for_prompt_structure(self):
        """S3-09d: format_for_prompt → 生成带 Pod + DB 状态的文本，包含关键字。"""
        from collectors.infra_collector import format_for_prompt

        infra = {
            'pods': [
                {'name': 'petsite-xyz', 'status': 'Running', 'restarts': 0,
                 'reason': '', 'az': 'ap-northeast-1a'}
            ],
            'databases': [
                {'dbname': 'petsite-rds', 'engine': 'aurora-mysql',
                 'status': 'available', 'connections': 42,
                 'cpu_pct': 15.0, 'freeable_memory_mb': 512}
            ],
        }
        text = format_for_prompt(infra)

        assert '基础设施状态' in text
        assert 'petsite-xyz' in text
        assert 'petsite-rds' in text
        assert 'aurora-mysql' in text


# ─── S3-10: aws_probers ──────────────────────────────────────────────────────

class TestAWSProbers:

    def test_s3_10_alb_probe_unhealthy_target(self):
        """S3-10a: ALBProbe → mock elbv2 返回 unhealthy target，ProbeResult healthy=False。"""
        from collectors.aws_probers import ALBProbe

        probe = ALBProbe()

        mock_elb = MagicMock()
        mock_cw = MagicMock()

        mock_elb.describe_load_balancers.return_value = {
            'LoadBalancers': [{
                'LoadBalancerName': 'Servic-PetSite-alb',
                'LoadBalancerArn':
                    'arn:aws:elasticloadbalancing:ap-northeast-1:926093770964:'
                    'loadbalancer/app/Servic-PetSite-alb/abcdef',
            }]
        }
        # No CW metric anomalies
        mock_cw.get_metric_statistics.return_value = {'Datapoints': []}

        mock_elb.describe_target_groups.return_value = {
            'TargetGroups': [{
                'TargetGroupArn': 'arn:aws:...targetgroup/petsite-tg/xyz',
                'TargetGroupName': 'petsite-tg',
            }]
        }
        mock_elb.describe_target_health.return_value = {
            'TargetHealthDescriptions': [
                {
                    'Target': {'Id': 'i-0abc12345678'},
                    'TargetHealth': {
                        'State': 'unhealthy',
                        'Reason': 'Target.ResponseCodeMismatch',
                    },
                }
            ]
        }

        def fake_client(service, region_name=None):
            if service == 'elbv2':
                return mock_elb
            elif service == 'cloudwatch':
                return mock_cw
            return MagicMock()

        with patch('boto3.client', side_effect=fake_client):
            result = probe.probe(
                {'metric': 'alb_5xx_rate', 'value': 0.3},
                'petsite'
            )

        assert result is not None
        assert result.healthy is False
        assert result.score_delta > 0
        assert result.service_name == 'ALB'
        assert any('Unhealthy' in e for e in result.evidence)

    def test_s3_10b_alb_probe_no_alb_returns_none(self):
        """S3-10b: 找不到 ALB 时 ALBProbe.probe 返回 None（服务不适用）。"""
        from collectors.aws_probers import ALBProbe

        probe = ALBProbe()

        mock_elb = MagicMock()
        mock_elb.describe_load_balancers.return_value = {'LoadBalancers': []}

        def fake_client(service, region_name=None):
            if service == 'elbv2':
                return mock_elb
            return MagicMock()

        with patch('boto3.client', side_effect=fake_client):
            result = probe.probe({'metric': 'alb_5xx_rate', 'value': 0.5}, 'petsite')

        assert result is None

    def test_s3_10c_sqs_probe_dlq_has_messages(self):
        """S3-10c: SQSProbe → DLQ 有消息 → ProbeResult healthy=False, score_delta>0。"""
        from collectors.aws_probers import SQSProbe

        probe = SQSProbe()

        mock_sqs = MagicMock()
        mock_cw = MagicMock()

        mock_sqs.list_queues.return_value = {
            'QueueUrls': [
                'https://sqs.ap-northeast-1.amazonaws.com/926093770964/sqspetadoption',
                'https://sqs.ap-northeast-1.amazonaws.com/926093770964/sqspetadoption-dlq',
            ]
        }

        def fake_attrs(QueueUrl, AttributeNames):
            if 'dlq' in QueueUrl:
                return {'Attributes': {
                    'ApproximateNumberOfMessages': '5',
                    'ApproximateNumberOfMessagesNotVisible': '0',
                    'ApproximateNumberOfMessagesDelayed': '0',
                }}
            return {'Attributes': {
                'ApproximateNumberOfMessages': '0',
                'ApproximateNumberOfMessagesNotVisible': '0',
                'ApproximateNumberOfMessagesDelayed': '0',
            }}

        mock_sqs.get_queue_attributes.side_effect = fake_attrs
        mock_cw.get_metric_statistics.return_value = {'Datapoints': []}

        def fake_client(service, region_name=None):
            if service == 'sqs':
                return mock_sqs
            elif service == 'cloudwatch':
                return mock_cw
            return MagicMock()

        with patch('boto3.client', side_effect=fake_client):
            result = probe.probe({'metric': 'error_rate', 'value': 0.3}, 'petsite')

        assert result is not None
        assert result.healthy is False
        assert result.score_delta > 0
        assert any('dlq' in e.lower() or 'DLQ' in e for e in result.evidence)

    def test_s3_10d_probe_result_prompt_block(self):
        """S3-10d: ProbeResult.to_prompt_block() → 包含服务名、状态、evidence。"""
        from collectors.aws_probers import ProbeResult

        pr = ProbeResult(
            service_name='DynamoDB',
            healthy=False,
            score_delta=25,
            summary='Throttling on 1 table',
            details={},
            evidence=['petadoption: WriteThrottleEvents=12'],
        )
        block = pr.to_prompt_block()

        assert 'DynamoDB' in block
        assert 'ANOMALY' in block
        assert 'WriteThrottleEvents=12' in block

    def test_s3_10e_total_score_delta_capped_at_40(self):
        """S3-10e: total_score_delta → 多个异常 probe 累计超 40 时上限为 40。"""
        from collectors.aws_probers import ProbeResult, total_score_delta

        results = [
            ProbeResult('SQS', False, 20, 'DLQ messages'),
            ProbeResult('DynamoDB', False, 25, 'Throttling'),
            ProbeResult('Lambda', False, 15, 'Errors'),
        ]
        delta = total_score_delta(results)
        assert delta == 40, f"Expected cap at 40, got {delta}"


# ─── S3-11: eks_auth ─────────────────────────────────────────────────────────

class TestEKSAuth:

    def test_s3_11_get_k8s_endpoint_returns_endpoint_and_ca(self):
        """S3-11a: get_k8s_endpoint → mock boto3 EKS，返回 (endpoint_url, ca_data_str)。"""
        from collectors.eks_auth import get_k8s_endpoint

        mock_eks = MagicMock()
        mock_eks.describe_cluster.return_value = {
            'cluster': {
                'endpoint': 'https://ABCDEF1234.gr7.ap-northeast-1.eks.amazonaws.com',
                'certificateAuthority': {'data': 'dGVzdGNlcnRkYXRh'},
            }
        }

        with patch('boto3.client', return_value=mock_eks):
            endpoint, ca_data = get_k8s_endpoint('PetSite')

        assert endpoint.startswith('https://')
        assert 'eks.amazonaws.com' in endpoint
        assert ca_data == 'dGVzdGNlcnRkYXRh'
        mock_eks.describe_cluster.assert_called_once_with(name='PetSite')

    def test_s3_11_write_ca_decodes_and_writes_file(self):
        """S3-11b: write_ca → base64 decode + 写临时文件，返回可读的有效路径。"""
        from collectors.eks_auth import write_ca

        sample_cert = (
            b'-----BEGIN CERTIFICATE-----\n'
            b'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtest\n'
            b'-----END CERTIFICATE-----\n'
        )
        ca_data = base64.b64encode(sample_cert).decode()

        ca_path = write_ca(ca_data)

        try:
            assert os.path.exists(ca_path), f"CA file not found: {ca_path}"
            with open(ca_path, 'rb') as f:
                content = f.read()
            assert content == sample_cert, "CA file content mismatch"
            assert ca_path.endswith('.crt')
        finally:
            if os.path.exists(ca_path):
                os.unlink(ca_path)

    def test_s3_11_get_eks_token_format(self):
        """S3-11c: get_eks_token → 返回 'k8s-aws-v1.' 前缀的 base64 token。"""
        from collectors.eks_auth import get_eks_token

        mock_creds = MagicMock()
        mock_creds.access_key = 'AKIATEST12345'
        mock_creds.secret_key = 'secretkeytest'
        mock_creds.token = 'sessiontokentest'

        mock_session = MagicMock()
        mock_session.get_credentials.return_value \
            .get_frozen_credentials.return_value = mock_creds

        # Mock AWSRequest so it has a .url attribute after signing
        mock_request_instance = MagicMock()
        mock_request_instance.url = (
            'https://sts.ap-northeast-1.amazonaws.com/'
            '?Action=GetCallerIdentity&Version=2011-06-15'
            '&X-Amz-Signature=fakesignature'
        )

        with patch('boto3.Session', return_value=mock_session), \
             patch('collectors.eks_auth.AWSRequest', return_value=mock_request_instance), \
             patch('collectors.eks_auth.SigV4QueryAuth'):
            token = get_eks_token('PetSite')

        assert token.startswith('k8s-aws-v1.'), \
            f"Token must start with 'k8s-aws-v1.', got: {token[:30]}"
        # Verify it is valid base64 after prefix
        suffix = token[len('k8s-aws-v1.'):]
        # Add padding and decode
        padded = suffix + '=' * (4 - len(suffix) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        assert b'sts' in decoded or len(decoded) > 0

    def test_s3_11_get_k8s_endpoint_eks_error_propagates(self):
        """S3-11d: EKS API 报错时 get_k8s_endpoint 让异常传播（不静默吞掉错误）。"""
        from collectors.eks_auth import get_k8s_endpoint

        mock_eks = MagicMock()
        mock_eks.describe_cluster.side_effect = Exception('ClusterNotFound: PetSite')

        with patch('boto3.client', return_value=mock_eks):
            with pytest.raises(Exception, match='ClusterNotFound'):
                get_k8s_endpoint('PetSite')

"""
tests/test_14_unit_etl_cfn.py — Sprint 2: CFN ETL + ETL Trigger 单元测试

S2-04: CFN ETL — CloudFormation 模板解析为图结构（mock boto3 cfn）
S2-05: CFN ETL — 嵌套 Stack 处理
S2-06: ETL Trigger — 定时触发逻辑/事件格式验证
"""

import os
import sys
import types
import json
from unittest.mock import MagicMock, patch, call

import pytest

# ── 0. 文件存在检查 ──────────────────────────────────────────────────────────
CFN_PATH = '/home/ubuntu/tech/graph-dependency-platform/infra/lambda/etl_cfn'
TRIGGER_PATH = '/home/ubuntu/tech/graph-dependency-platform/infra/lambda/etl_trigger'
_CFN_FILE = os.path.join(CFN_PATH, 'neptune_etl_cfn.py')
_TRIGGER_FILE = os.path.join(TRIGGER_PATH, 'neptune_etl_trigger.py')

if not os.path.exists(_CFN_FILE):
    pytest.skip('etl_cfn handler not found', allow_module_level=True)
if not os.path.exists(_TRIGGER_FILE):
    pytest.skip('etl_trigger handler not found', allow_module_level=True)

# ── 1. 环境变量 ───────────────────────────────────────────────────────────────
os.environ.setdefault('REGION', 'ap-northeast-1')
os.environ.setdefault('NEPTUNE_ENDPOINT', 'test-endpoint.example.com')
os.environ.setdefault('NEPTUNE_PORT', '8182')
os.environ.setdefault('CFN_STACK_NAMES', 'ServicesEks2,Applications')
os.environ.setdefault('ENVIRONMENT', 'test')
os.environ.setdefault('ETL_FUNCTION_NAME', 'neptune-etl-from-aws')
os.environ.setdefault('TRIGGER_DELAY_SECONDS', '30')

# ── 2. Mock neptune_client_base（Lambda Layer，测试环境不存在）────────────────
_nc_mock = types.ModuleType('neptune_client_base')
_nc_mock.neptune_query = MagicMock(
    return_value={'result': {'data': {'@value': []}}}
)
_nc_mock.safe_str = lambda s: str(s).replace("'", "\\'").replace('"', '\\"')[:128]
_nc_mock.extract_value = lambda v: v.get('@value', v) if isinstance(v, dict) else v
_nc_mock.REGION = 'ap-northeast-1'

if 'neptune_client_base' not in sys.modules:
    sys.modules['neptune_client_base'] = _nc_mock

# ── 3. 导入 CFN ETL 模块 ──────────────────────────────────────────────────────
if CFN_PATH not in sys.path:
    sys.path.insert(0, CFN_PATH)

import neptune_etl_cfn as etl_cfn  # noqa: E402

# ── 4. 导入 ETL Trigger 模块 ──────────────────────────────────────────────────
if TRIGGER_PATH not in sys.path:
    sys.path.insert(0, TRIGGER_PATH)

import neptune_etl_trigger as etl_trigger  # noqa: E402


# ── Fixture: 覆盖 conftest 的 neptune_rca（本文件全为单测）───────────────────
@pytest.fixture(scope='session')
def neptune_rca():
    """Override conftest fixture: return MagicMock to prevent real Neptune call."""
    mock_nc = MagicMock()
    mock_nc.results.return_value = []
    return mock_nc


# ─────────────────────────────────────────────────────────────────────────────
# S2-04: CloudFormation 模板解析为图结构
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_04_cfn_template_lambda_ddb_dependency():
    """S2-04: CFN ETL — CloudFormation 模板解析为图结构（mock boto3 cfn）

    验证 extract_declared_deps 从 Lambda 环境变量 Ref 中正确提取
    Lambda→DynamoDB 和 Lambda→SQS 语义依赖关系，生成 AccessesData 边描述。
    """
    template = {
        'Resources': {
            'AdoptionLambda': {
                'Type': 'AWS::Lambda::Function',
                'Properties': {
                    'FunctionName': 'petadoption-processor',
                    'Environment': {
                        'Variables': {
                            'TABLE_NAME':  {'Ref': 'AdoptionTable'},
                            'QUEUE_URL':   {'Ref': 'AdoptionQueue'},
                            'PLAIN_STR':   'some-literal-value',     # 非引用，应忽略
                        }
                    }
                }
            },
            'AdoptionTable': {
                'Type': 'AWS::DynamoDB::Table',
                'Properties': {'TableName': 'petadoption-table'}
            },
            'AdoptionQueue': {
                'Type': 'AWS::SQS::Queue',
                'Properties': {}
            },
        }
    }

    physical_map = {
        'AdoptionLambda': {
            'physical_id': 'petadoption-processor',
            'type': 'AWS::Lambda::Function',
        },
        'AdoptionTable': {
            'physical_id': 'petadoption-table',
            'type': 'AWS::DynamoDB::Table',
        },
        'AdoptionQueue': {
            'physical_id': 'https://sqs.ap-northeast-1.amazonaws.com/123/petadoption-q',
            'type': 'AWS::SQS::Queue',
        },
    }

    deps = etl_cfn.extract_declared_deps(template, physical_map)

    assert len(deps) == 2, f"Expected 2 deps (DDB + SQS), got {len(deps)}: {deps}"

    srcs = {d['src_physical'] for d in deps}
    dsts = {d['dst_physical'] for d in deps}

    assert 'petadoption-processor' in srcs
    assert 'petadoption-table' in dsts
    assert 'https://sqs.ap-northeast-1.amazonaws.com/123/petadoption-q' in dsts

    # Relationship type must be AccessesData (Lambda env var pattern)
    rel_types = {d['rel_type'] for d in deps}
    assert rel_types == {'AccessesData'}, f"Unexpected rel_types: {rel_types}"

    # Evidence should capture the env var name
    evidences = {d['evidence'] for d in deps}
    assert any(ev.startswith('env:') for ev in evidences)


def test_s2_04_cfn_stepfunction_invokes_lambda():
    """S2-04 (StepFunction): StepFunction DefinitionString 中的 Lambda Ref 被提取为 Invokes 边"""
    template = {
        'Resources': {
            'MyStateMachine': {
                'Type': 'AWS::StepFunctions::StateMachine',
                'Properties': {
                    'DefinitionString': {
                        'Fn::Sub': [
                            '{"states": {"Step1": {"Resource": "${ProcessorLambda.Arn}"}}}',
                            {'ProcessorLambda': {'Fn::GetAtt': ['ProcessorLambda', 'Arn']}}
                        ]
                    }
                }
            },
            'ProcessorLambda': {
                'Type': 'AWS::Lambda::Function',
                'Properties': {'FunctionName': 'petsite-processor'}
            },
        }
    }

    physical_map = {
        'MyStateMachine': {
            'physical_id': 'arn:aws:states:ap-northeast-1:123:stateMachine:MyStateMachine',
            'type': 'AWS::StepFunctions::StateMachine',
        },
        'ProcessorLambda': {
            'physical_id': 'petsite-processor',
            'type': 'AWS::Lambda::Function',
        },
    }

    deps = etl_cfn.extract_declared_deps(template, physical_map)

    # Should find StepFunction → Lambda Invokes relationship
    invokes_deps = [d for d in deps if d['rel_type'] == 'Invokes']
    assert len(invokes_deps) >= 1, f"Expected Invokes dep, got deps: {deps}"
    assert invokes_deps[0]['dst_physical'] == 'petsite-processor'


# ─────────────────────────────────────────────────────────────────────────────
# S2-05: 嵌套 Stack 处理
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_05_nested_stack_resource_skipped():
    """S2-05: CFN ETL — 嵌套 Stack 类型不纳入图依赖，不崩溃

    当 CloudFormation 模板包含 AWS::CloudFormation::Stack 嵌套栈时，
    extract_declared_deps 应忽略该嵌套栈资源（不在 SEMANTIC_TYPES 内），
    不生成错误或以嵌套栈为端点的依赖边。
    """
    template = {
        'Resources': {
            'NestedInfraStack': {
                'Type': 'AWS::CloudFormation::Stack',
                'Properties': {
                    'TemplateURL': 'https://s3.amazonaws.com/bucket/infra.template'
                }
            },
            'ProcessorLambda': {
                'Type': 'AWS::Lambda::Function',
                'Properties': {
                    'FunctionName': 'petsite-processor',
                    'Environment': {
                        'Variables': {
                            'TABLE_ARN': {'Ref': 'PetTable'},
                        }
                    }
                }
            },
            'PetTable': {
                'Type': 'AWS::DynamoDB::Table',
                'Properties': {}
            },
        }
    }

    physical_map = {
        'NestedInfraStack': {
            'physical_id': 'arn:aws:cloudformation:ap-northeast-1:123:stack/nested/uuid',
            'type': 'AWS::CloudFormation::Stack',
        },
        'ProcessorLambda': {
            'physical_id': 'petsite-processor',
            'type': 'AWS::Lambda::Function',
        },
        'PetTable': {
            'physical_id': 'petsite-table',
            'type': 'AWS::DynamoDB::Table',
        },
    }

    # Should not raise
    deps = etl_cfn.extract_declared_deps(template, physical_map)

    # Nested stack must NOT appear as src or dst
    nested_arn = 'arn:aws:cloudformation:ap-northeast-1:123:stack/nested/uuid'
    nested_srcs = [d for d in deps if d['src_physical'] == nested_arn]
    nested_dsts = [d for d in deps if d['dst_physical'] == nested_arn]
    assert len(nested_srcs) == 0, "Nested stack should not be a dependency source"
    assert len(nested_dsts) == 0, "Nested stack should not be a dependency target"

    # Regular Lambda → DynamoDB dep should still be found
    valid_deps = [
        d for d in deps
        if d['src_physical'] == 'petsite-processor' and d['dst_physical'] == 'petsite-table'
    ]
    assert len(valid_deps) == 1, (
        f"Expected Lambda→DynamoDB dep to still be extracted, deps={deps}"
    )


def test_s2_05_handler_cfn_event_routing():
    """S2-05 (handler): EventBridge CFN StackEvent 路由到正确的 stack

    handler 接收到 aws.cloudformation 事件时，应从 stack-id 提取 stack name，
    并只处理配置列表中已知的 stack（不处理未知 stack 的事件会全量运行）。
    """
    with patch.object(etl_cfn, 'run_etl') as mock_run:
        mock_run.return_value = {'total_deps': 5}

        # Valid CloudFormation stack event for a configured stack
        event = {
            'source': 'aws.cloudformation',
            'detail': {
                'stack-id': 'arn:aws:cloudformation:ap-northeast-1:123:stack/ServicesEks2/uuid',
                'status-details': {'status': 'UPDATE_COMPLETE'},
            }
        }

        result = etl_cfn.handler(event, None)

    assert result['statusCode'] == 200
    mock_run.assert_called_once_with(['ServicesEks2'])


# ─────────────────────────────────────────────────────────────────────────────
# S2-06: ETL Trigger — 定时触发逻辑/事件格式验证
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_06_trigger_sqs_event_invokes_etl():
    """S2-06: ETL Trigger — SQS 消息触发 neptune-etl-from-aws Lambda（异步调用）

    验证 handler 正确解析 SQS body、等待 DELAY_SECONDS、然后以 Event（异步）模式
    调用 ETL Lambda，并将 event_sources 写入 payload。
    """
    with patch.object(etl_trigger, 'lambda_client') as mock_lc, \
         patch('time.sleep') as mock_sleep:

        mock_lc.invoke.return_value = {'StatusCode': 202}

        event = {
            'Records': [
                {
                    'body': json.dumps({
                        'source': 'aws.rds',
                        'detail-type': 'RDS DB Instance Event',
                        'detail': {'SourceIdentifier': 'petsite-db-primary'},
                    })
                }
            ]
        }

        result = etl_trigger.handler(event, None)

    assert result['statusCode'] == 200

    # Must sleep to let AWS data settle
    mock_sleep.assert_called_once_with(30)

    # Must invoke ETL Lambda asynchronously
    mock_lc.invoke.assert_called_once()
    invoke_kwargs = mock_lc.invoke.call_args[1]
    assert invoke_kwargs['FunctionName'] == 'neptune-etl-from-aws'
    assert invoke_kwargs['InvocationType'] == 'Event'

    # Payload must contain event source info
    payload = json.loads(invoke_kwargs['Payload'])
    assert payload['trigger_source'] == 'event_driven'
    sources = payload['event_sources']
    assert len(sources) == 1
    assert sources[0]['source'] == 'aws.rds'
    assert sources[0]['resource'] == 'petsite-db-primary'


def test_s2_06_trigger_multiple_records_batched():
    """S2-06: ETL Trigger — 多条 SQS 消息批量处理，仅触发一次 ETL 调用

    批量 SQS 消息（多个 AWS 事件）应合并为一次 ETL 调用，避免并发写入 Neptune。
    """
    with patch.object(etl_trigger, 'lambda_client') as mock_lc, \
         patch('time.sleep'):

        mock_lc.invoke.return_value = {'StatusCode': 202}

        event = {
            'Records': [
                {
                    'body': json.dumps({
                        'source': 'aws.ec2',
                        'detail-type': 'EC2 Instance State-change Notification',
                        'detail': {'instance-id': 'i-0abcdef1234567890'},
                    })
                },
                {
                    'body': json.dumps({
                        'source': 'aws.elasticloadbalancing',
                        'detail-type': 'AWS API Call via CloudTrail',
                        'detail': {
                            'requestParameters': {
                                'targetGroupArn': 'arn:aws:elasticloadbalancing:...'
                            }
                        },
                    })
                },
            ]
        }

        result = etl_trigger.handler(event, None)

    assert result['statusCode'] == 200
    # Only ONE Lambda.invoke call regardless of record count
    assert mock_lc.invoke.call_count == 1

    payload = json.loads(mock_lc.invoke.call_args[1]['Payload'])
    assert len(payload['event_sources']) == 2
    event_source_types = {e['source'] for e in payload['event_sources']}
    assert 'aws.ec2' in event_source_types
    assert 'aws.elasticloadbalancing' in event_source_types


def test_s2_06_trigger_empty_records_skips_etl():
    """S2-06: ETL Trigger — 空 SQS 消息不触发 ETL 调用"""
    with patch.object(etl_trigger, 'lambda_client') as mock_lc, \
         patch('time.sleep') as mock_sleep:

        result = etl_trigger.handler({'Records': []}, None)

    assert result['statusCode'] == 200
    mock_lc.invoke.assert_not_called()
    mock_sleep.assert_not_called()

"""test_41_fis_catalog_reconcile.py —— fault_catalog.yaml 与 AWS FIS 真实能力对账。

## 这组测试守什么

`fault_catalog.yaml` 声明了 60 条故障（chaosmesh 19 / fis 37 / scenarios 4），
但**声明不等于可执行**。2026-08-31 对账实测查出 3 类偏差，每一类都意味着
对应的故障类型从未真正跑过：

| 偏差 | 实例 | 后果 |
|---|---|---|
| action id 在 AWS 侧不存在 | `aws:ec2:disrupt-network-connectivity`、`aws:elasticache:interrupt-cluster-az-power` | 建模板即失败 |
| 构建的目标类型与 action 要求不符 | `fis_vpc_endpoint_disrupt` 建 `aws:ec2:subnet`，而 action 要 `aws:ec2:vpc-endpoint` | 建模板即失败 |
| 只在特定环境有目标可打 | `disrupt-vpc-endpoint` 需要 interface 端点，PetSite VPC 里只有 1 个 | 能建模板但选不出资源 |

第三类**测不出来**（依赖具体环境），只能靠文档记录；前两类可以自动对账，
这就是本文件的职责。

## 为什么比「backend 构建的类型」而不是目录的 `requires`

`requires` 描述的是**输入契约**（调用方要提供什么），AWS 的 `targets` 描述的是
**目标资源类型**，两者本来就可以不同 —— `fis_rds_reboot` 的 `requires` 是
`cluster_arn`，而 fis_backend 内部把它转成 writer 实例 ARN 建
`aws:rds:db`，AWS 侧接受、实验实跑成功（EXPqjusfhv86R3t8F4 completed）。
拿 `requires` 去比会产生假警报，所以必须比 backend 真正构建出来的东西。

## 需要 AWS 凭证

`aws fis list-actions` / `get-action` 是只读调用。没有凭证时整组 skip 而不是失败 ——
这类对账属于「与外部真相同步」，本地无凭证环境跑不了不代表代码有问题。
"""
from __future__ import annotations

import json
import subprocess
import sys
import pathlib

import pytest
import yaml

from paths import PROJECT_ROOT  # noqa: F401

_CHAOS = str(pathlib.Path(PROJECT_ROOT) / 'chaos' / 'code')
if _CHAOS not in sys.path:
    sys.path.insert(0, _CHAOS)

CATALOG = pathlib.Path(PROJECT_ROOT) / 'chaos' / 'code' / 'runner' / 'fault_catalog.yaml'
REGION = 'ap-northeast-1'


def _aws(*args) -> object:
    r = subprocess.run(['aws', *args, '--region', REGION, '--output', 'json'],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"AWS 调用不可用（无凭证或无权限），跳过对账: {r.stderr[:160]}")
    return json.loads(r.stdout or 'null')


@pytest.fixture(scope='module')
def catalog() -> dict:
    return yaml.safe_load(CATALOG.read_text())


@pytest.fixture(scope='module')
def aws_actions() -> dict:
    """{action_id: {target_name: resourceType}}，只拉目录真正用到的那些。"""
    ids = _aws('fis', 'list-actions', '--query', 'actions[].id')
    return set(ids or [])


@pytest.fixture(scope='module')
def aws_action_targets(catalog, aws_actions) -> dict:
    used = sorted({f['fis_action_id'] for f in catalog['fis']
                   if f.get('fis_action_id')} & aws_actions)
    out = {}
    for a in used:
        t = _aws('fis', 'get-action', '--id', a, '--query', 'action.targets')
        out[a] = {k: v['resourceType'] for k, v in (t or {}).items()}
    return out


# ── g01：每个 action id 都必须在 AWS 侧存在 ─────────────────────────────────

def test_f01_every_declared_action_exists_in_aws(catalog, aws_actions):
    """目录里的 fis_action_id 必须真实存在。

    2026-08-31 实测查出两个不存在的，均已修：
      aws:ec2:disrupt-network-connectivity        -> aws:network:disrupt-connectivity
      aws:elasticache:interrupt-cluster-az-power  -> aws:elasticache:replicationgroup-interrupt-az-power
    """
    missing = sorted({f['fis_action_id'] for f in catalog['fis']
                      if f.get('fis_action_id')} - aws_actions)
    assert not missing, (
        "以下 fis_action_id 在 AWS 侧不存在，对应的故障类型无法执行：\n  "
        + "\n  ".join(missing)
        + "\n用 `aws fis list-actions` 核对真实 id。")


def test_f02_scenario_sub_actions_exist(catalog, aws_actions):
    """复合场景的 sub_actions 同样要对账 —— 它们也是真实 action id。"""
    missing = set()
    for sc in catalog.get('fis_scenarios', []):
        for a in sc.get('sub_actions') or []:
            if a and a not in aws_actions:
                missing.add(f"{sc.get('type')}: {a}")
    assert not missing, "场景的 sub_actions 里有不存在的 action：\n  " + "\n  ".join(sorted(missing))


# ── g02：backend 构建的目标类型必须与 action 要求一致 ───────────────────────

# 每种故障类型跑目标构建所需的最小 extra_params。
# 只为让构建器走到 return，取值本身不发往 AWS。
_MIN_EXTRA = {
    'subnet_arn': f'arn:aws:ec2:{REGION}:123456789012:subnet/subnet-0aaa',
    'vpc_endpoint_arn': f'arn:aws:ec2:{REGION}:123456789012:vpc-endpoint/vpce-0aaa',
    'route_table_arn': f'arn:aws:ec2:{REGION}:123456789012:route-table/rtb-0aaa',
    'function_arn': f'arn:aws:lambda:{REGION}:123456789012:function:f',
    'cluster_arn': f'arn:aws:rds:{REGION}:123456789012:cluster:c',
    'nodegroup_arn': f'arn:aws:eks:{REGION}:123456789012:nodegroup/c/ng/uuid',
    'instance_arn': f'arn:aws:ec2:{REGION}:123456789012:instance/i-0aaa',
    'volume_arns': [f'arn:aws:ec2:{REGION}:123456789012:volume/vol-0aaa'],
    'volume_ids': ['vol-0aaa'],
    'table_arn': f'arn:aws:dynamodb:{REGION}:123456789012:table/t',
    'bucket_arn': 'arn:aws:s3:::b',
    'availability_zone': f'{REGION}a',
    'cluster_name': 'PetSite',
    'namespace': 'petadoptions',
    'selector_value': 'app=x',
    'scope': 'all',
    'role_arn': 'arn:aws:iam::123456789012:role/r',
    'asg_arn': f'arn:aws:autoscaling:{REGION}:123456789012:autoScalingGroup:uuid:autoScalingGroupName/g',
    'global_table_arn': f'arn:aws:dynamodb:{REGION}:123456789012:global-table/t',
    'replication_group_arn': f'arn:aws:elasticache:{REGION}:123456789012:replicationgroup:rg',
    'managed_resource_arn': f'arn:aws:route53-recovery-readiness::123456789012:resource/r',
}

# 目标构建器会**真的调 AWS** 的故障类型：合成 ARN 必然查不到资源，
# 那是夹具的限制不是缺陷，故只在这几类上豁免「构建失败」断言。
# 名单刻意写死而不是靠捕获 botocore 异常 —— 后者会把真实的权限/网络问题也吞掉。
_LIVE_LOOKUP = {'fis_rds_reboot', 'fis_rds_failover'}


def test_f03_backend_target_type_matches_action(catalog, aws_action_targets):
    """fis_backend 构建的 resourceType 必须在 action 声明的目标类型集合里。

    **比 backend 实际构建的东西，不比目录的 `requires`** —— 后者是输入契约，
    与 AWS 目标类型本来就可以不同（fis_rds_reboot: 输入 cluster_arn，
    内部转成 writer 实例建 aws:rds:db，AWS 接受且实跑成功）。

    2026-08-31 实测查出 1 条真实不符，已修：
      fis_vpc_endpoint_disrupt 建 aws:ec2:subnet，而 action 要 aws:ec2:vpc-endpoint
    """
    from runner.fis_backend import FISClient

    mismatches, unbuildable = [], []
    for f in catalog['fis']:
        ftype, aid = f.get('type'), f.get('fis_action_id')
        if not aid or aid not in aws_action_targets:
            continue
        allowed = set(aws_action_targets[aid].values())
        if not allowed:                      # 无目标类型的 action（少数存在）
            continue
        extra = dict(_MIN_EXTRA)
        extra.update(f.get('default_params') or {})
        try:
            # 用未初始化实例调用：_build_target 只读 fault_type/extra，
            # 不触碰 self 上的 boto3 客户端，所以不需要真实凭证
            built = FISClient._build_target(FISClient.__new__(FISClient), ftype, extra)
        except NotImplementedError:
            continue
        except ValueError as e:
            # 「没有对应分支」是真缺陷：目录声明了故障类型，backend 却处理不了。
            # 2026-08-31 实测查出 fis_eks_inject_k8s_custom 就是这样 ——
            # fis_backend 只有 `startswith("fis_eks_pod")` 分支，
            # 而它的前缀是 fis_eks_（无 pod），调用即抛 ValueError。
            unbuildable.append(f"{ftype}: 无对应的目标构建分支 —— {e}")
            continue
        except Exception as e:
            if ftype in _LIVE_LOOKUP:
                continue                     # 夹具用的是合成 ARN，查不到资源属预期
            unbuildable.append(f"{ftype}: {type(e).__name__}: {e}")
            continue
        if not isinstance(built, dict) or 'resourceType' not in built:
            unbuildable.append(f"{ftype}: 构建结果里没有 resourceType -> {built!r}")
            continue
        if built['resourceType'] not in allowed:
            mismatches.append(
                f"{ftype}: 构建 {built['resourceType']}，"
                f"而 {aid} 要求 {sorted(allowed)}")

    assert not mismatches, (
        "fis_backend 构建的目标类型与 AWS action 要求不符（这些故障类型无法执行）：\n  "
        + "\n  ".join(mismatches))
    # 构建失败单独断言：信息量不同，混在一起会看不清是哪类问题
    assert not unbuildable, (
        "以下故障类型的目标构建直接失败：\n  " + "\n  ".join(unbuildable))


# ── g03：离线一致性（无需 AWS） ─────────────────────────────────────────────

def test_f04_declared_counts_match_body(catalog):
    """目录三段的条数与文档/记忆里引用的数字一致。

    历史上「FIS 15 种」「Chaos Mesh 30 种」这类计数错误反复出现过，
    这里把真值锚在文件自身。
    """
    assert len(catalog['chaosmesh']) == 19
    # 36 而非 37：2026-08-31 删除了 fis_ec2_network_disrupt ——
    # 它的 action id 在 AWS 侧不存在，描述的「实例级网络隔离」这个能力也不存在
    # （唯一对应的真 action 目标是子网），改成真 action 后又与 fis_network_disrupt
    # 完全重复。详见 fault_catalog.yaml 该位置的注释。
    assert len(catalog['fis']) == 36
    assert len(catalog['fis_scenarios']) == 4


def test_f05_every_fis_fault_declares_an_action(catalog):
    """非复合的 fis 故障必须有 fis_action_id —— 空串只允许出现在 scenarios 里。"""
    naked = [f['type'] for f in catalog['fis'] if not f.get('fis_action_id')]
    assert not naked, f"以下 fis 故障没有 action id: {naked}"

"""tests/test_69_edge_traffic_probe_honesty.py

边级流量探针的三条纪律。全部来自 2026-09-09 的实测事故。

## 事故经过

要给 52 条待验边做流量前置检查，先拿 **9 条已 confirmed 的边**反向自检
探针 —— 只有 1 条测出流量。三个独立缺陷叠在一起：

1. **图谱名 ≠ K8s 工作负载名**。图谱名来自链路追踪的逻辑服务名，
   DeepFlow 的 pod_group 是 K8s 名，本环境大面积不一致：

       collect_edge_flow('petsite', 'petsearch')      -> 0 次
       collect_edge_flow('petsite', 'search-service') -> 20,660 次

   全系统最忙的那条路径被读成 0。而 0 在判据里意味着「无从打断，
   任何退化数字都是噪声」—— 前置检查从保护变成了伤害。

2. **DeepFlow 对一半的目标类型是瞎的**。它抓 L7；MySQL 协议与到 AWS
   端点的 TLS 都解析不了。实测 `petsite-deployment` 在 DeepFlow 里
   **根本没有**到 RDS 或 AWS 服务的流（去掉协议过滤也一样）。
   对这些目标，0 的含义是「瞎了」不是「无流量」。

3. **X-Ray 对 AWS 服务用资源级节点**。图谱的 AWSServiceEndpoint 是
   服务级抽象（`dynamodb`），X-Ray 是 `AWS::DynamoDB::Table` + 表名，
   按名字永远匹配不上；`petsite -> ssm` 在 X-Ray 里叫
   `PetSite -> SimpleSystemsManagement`。

修完之后 `petsearch -> dynamodb` 从 0 变成 17,564 次。

## 贯穿这三条的同一条原则

**「测不出」与「确实为零」必须是两个结论。** 这与仓库既有的
「采集失败必须 ok=False」「零流量不判 refuted」是同一条原则的不同侧面。
把「测不出」当「为零」会把一批可验的边错标成休眠，而休眠标注会让它们
退出验证队列 —— 一个测量缺陷就变成了永久的覆盖率损失。
"""
from __future__ import annotations

import json
import pathlib
import sys

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'chaos' / 'code', ROOT / 'scripts'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_t69_01_名字解析必须读ETL用的同一份映射():
    """不得另立一套服务名映射表。

    本仓库已经因为「同类清单各处一份」踩过坑（rca 那份依赖边清单
    少 Invokes，线上漏 16 条边）。
    """
    from runner import service_names

    assert service_names._MAPPING_PATH.name == 'service_mappings.json', (
        '名字映射必须读 ETL 用的 service_mappings.json，不得硬编码')
    assert service_names._MAPPING_PATH.exists(), (
        f'映射文件不存在: {service_names._MAPPING_PATH}')

    # 已知的关键别名必须解析得出 —— 每一条都对应一次实测事故
    for graph_name, k8s_name in (('petsearch', 'search-service'),
                                 ('petlistadoptions', 'list-adoptions'),
                                 ('payforadoption', 'pay-for-adoption'),
                                 ('petsite', 'petsite-deployment'),
                                 ('trafficgenerator', 'traffic-generator')):
        assert service_names.to_k8s(graph_name) == k8s_name, (
            f'{graph_name} 应解析为 {k8s_name}，实际 '
            f'{service_names.to_k8s(graph_name)!r} —— '
            f'解析不出会让这条边的流量测成 0，进而被错标成休眠')
        assert k8s_name in service_names.k8s_candidates(graph_name)


def test_t69_02_两份映射表必须同源():
    """etl_aws 与 etl_deepflow 的 service_mappings.json 必须一致。

    实测踩到的漂移：`trafficgenerator -> traffic-generator` 存在于
    `etl_aws/collectors/eks.py` 的内联表，却**不在** JSON 里，
    于是 trafficgenerator 的流量测成 0。
    """
    a = json.loads((ROOT / 'infra' / 'lambda' / 'etl_aws'
                    / 'service_mappings.json').read_text(encoding='utf-8'))
    b = json.loads((ROOT / 'infra' / 'lambda' / 'etl_deepflow'
                    / 'service_mappings.json').read_text(encoding='utf-8'))
    for key in ('neptune_to_k8s', 'k8s_alias'):
        assert a.get(key) == b.get(key), (
            f'两份 service_mappings.json 的 {key} 不一致 —— '
            f'ETL 与 chaos runner 会对同一个服务给出不同的名字')


def test_t69_03_JSON必须覆盖eks内联表的全部别名():
    """内联表里的每个别名都必须在 JSON 里 —— 这是上面那次漂移的根因。"""
    import re

    src = (ROOT / 'infra' / 'lambda' / 'etl_aws' / 'collectors'
           / 'eks.py').read_text(encoding='utf-8')
    m = re.search(r'_K8S_SVC_ALIAS\s*=\s*\{([\s\S]*?)\n\}', src)
    assert m, '找不到 eks.py 的 _K8S_SVC_ALIAS'
    inline = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", m.group(1)))

    j = json.loads((ROOT / 'infra' / 'lambda' / 'etl_aws'
                    / 'service_mappings.json').read_text(encoding='utf-8'))
    alias = j.get('k8s_alias') or {}
    n2k = j.get('neptune_to_k8s') or {}

    missing = []
    for k8s, graph in inline.items():
        if k8s == graph:
            continue                      # 恒等项无需登记
        if alias.get(k8s) != graph and n2k.get(graph) != k8s:
            missing.append(f'{k8s} -> {graph}')
    assert not missing, (
        f'eks.py 内联表有这些别名而 service_mappings.json 没有: {missing}\n'
        f'chaos runner 只读 JSON，缺一条就有一个服务的边级流量测成 0，'
        f'而 0 会被判据读成「链路无流量」。')


def test_t69_04_DeepFlow盲区目标不得走DeepFlow():
    """MySQL / 到 AWS 端点的 TLS，DeepFlow 解析不了。

    实测：`petsite-deployment` 在 DeepFlow 里根本没有到 RDS 或 AWS 服务的流，
    去掉协议过滤也一样。对这些目标返回 0 是「瞎了」不是「无流量」。
    """
    import preflight_edge_traffic as pf

    for label in ('AWSServiceEndpoint', 'RDSCluster', 'RDSInstance',
                  'DynamoDBTable', 'S3Bucket'):
        assert label in pf._DEEPFLOW_BLIND_TARGETS, (
            f'{label} 不在 DeepFlow 盲区清单里 —— '
            f'它会走 DeepFlow 并测出 0，然后被错标成休眠')


def test_t69_05_AWS端点必须按Type前缀匹配而非名字():
    """图谱是服务级抽象，X-Ray 是资源级节点，按名字永远匹配不上。"""
    from runner import service_names

    # ssm 有两种 Type，实测两种都出现过
    assert 'AWS::SimpleSystemsManagement' in \
        service_names.xray_aws_type_prefixes('ssm'), (
            'ssm 缺 AWS::SimpleSystemsManagement 前缀 —— '
            '实测 petsite -> ssm 这条已确证的边在 X-Ray 里就叫这个名字')
    assert service_names.xray_aws_type_prefixes('dynamodb') == ('AWS::DynamoDB',)
    # 没有映射时必须返回空，让调用方走「测不出」而不是「无流量」
    assert service_names.xray_aws_type_prefixes('nonexistent-service') == ()


def test_t69_06_源侧有量时边级必须报测不出而非零():
    """退到源侧指标是有损的：源被调用不等于这条边被走到。

    源侧有量、边级看不到时必须返回 None（测不出）。返回 0 会把这条边
    标成休眠并踢出验证队列 —— 而它可能只是没开 Active 追踪。
    """
    src = (ROOT / 'scripts' / 'preflight_edge_traffic.py').read_text(
        encoding='utf-8')
    # _lambda_traffic 里源侧有量的分支必须 return None
    i = src.find('def _lambda_traffic')
    assert i != -1
    body = src[i:src.find('\ndef ', i + 10)]
    assert 'return None, (f\'源函数 24h 有' in body, (
        '_lambda_traffic 在「源函数有调用但 X-Ray 看不到边」时必须返回 None。'
        '返回 0 会把一条可能可验的边标成休眠。')

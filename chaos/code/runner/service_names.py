"""chaos/code/runner/service_names.py — 图谱服务名 ↔ K8s 工作负载名

## 为什么必须有这层（2026-09-09 实测）

图谱的 Microservice 名来自**链路追踪的逻辑服务名**，DeepFlow 的
`pod_group` 名是**K8s 工作负载名**，两者在本环境里大面积不一致：

    图谱名              K8s / DeepFlow 名        近 1h 服务端流量
    petsearch        -> search-service                60,459
    petlistadoptions -> list-adoptions                 9,312
    payforadoption   -> pay-for-adoption               9,342
    petsite          -> petsite-deployment           156,321
    pethistory       -> pethistory-deployment          6,450

`chaos/code/runner/metrics.py::collect_edge_flow` 直接拿图谱名去
`LIKE '%{name}%'` 匹配，于是：

    collect_edge_flow('petsite', 'petsearch')      -> 0 次
    collect_edge_flow('petsite', 'search-service') -> 20,660 次

**全系统最忙的那条路径（petsite -Calls-> petsearch，21k 次/15 分钟）
被读成 0 次调用。** 而按仓库自己的判据，边级流量为 0 意味着
「无从打断，任何退化数字都是噪声」—— 于是前置检查会挡掉一条
本来完全可验的边。这让前置检查从保护变成了伤害。

## 映射表不是新造的

`infra/lambda/*/service_mappings.json` 早就有 `neptune_to_k8s` 与
`k8s_alias` 两个方向的表，ETL 一直在用（etl_deepflow 正是靠它把
`search-service` 落成 `petsearch`，所以图谱里才没有 `search-service`
这个重复节点）。chaos runner 是唯一漏用它的地方。

这里**读同一份文件**，不另立一套 —— 本仓库已经因为「同类清单各处一份」
踩过坑（rca 那份依赖边清单少 Invokes，线上漏 16 条边）。
"""
from __future__ import annotations

import json
import logging
import pathlib

logger = logging.getLogger(__name__)

#: 权威映射的位置。三份 service_mappings.json 里 etl_aws 与 etl_deepflow
#: 内容一致（md5 相同），取 etl_aws 那份。
#: 注意 etl_xray 那份已漂移，**刻意不读它** —— 读到漂移的表比读不到更糟。
_MAPPING_PATH = (pathlib.Path(__file__).resolve().parents[3]
                 / 'infra' / 'lambda' / 'etl_aws' / 'service_mappings.json')

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(_MAPPING_PATH.read_text(encoding='utf-8'))
    except Exception as exc:                                  # noqa: BLE001
        # 读不到不是致命错 —— 退化成「只用原名」，与修复前行为一致。
        # 但必须响一声：静默退化会让边级流量重新变成 0，
        # 而 0 会被判据读成「链路无流量」。
        logger.warning('读不到 service_mappings.json（%s）: %r —— '
                       '名字解析退化为只用原名，边级流量可能测成 0',
                       _MAPPING_PATH, exc)
        _cache = {}
    return _cache


def to_k8s(graph_name: str) -> str:
    """图谱服务名 -> K8s 工作负载名。查不到就原样返回。"""
    if not graph_name:
        return graph_name
    return (_load().get('neptune_to_k8s') or {}).get(graph_name, graph_name)


def to_graph(k8s_name: str) -> str:
    """K8s 工作负载名 -> 图谱服务名。查不到就原样返回。"""
    if not k8s_name:
        return k8s_name
    return (_load().get('k8s_alias') or {}).get(k8s_name, k8s_name)


def k8s_candidates(graph_name: str) -> list[str]:
    """给一个图谱名，返回**所有**该试的 K8s 名，按可靠性排序。

    为什么要返回多个而不是一个：

      1. `neptune_to_k8s` 是权威，但它只覆盖有别名的服务；
      2. 没进表的服务（petfood 等）K8s 名与图谱名相同；
      3. 还有一类只差连字符（trafficgenerator / traffic-generator），
         实测它**不在** neptune_to_k8s 里却真实存在于 DeepFlow ——
         所以必须补一条去连字符的候选，否则又是一个静默的 0。

    调用方应依次尝试，第一个有流量的即为命中。
    """
    if not graph_name:
        return []
    out = []
    mapped = to_k8s(graph_name)
    if mapped and mapped != graph_name:
        out.append(mapped)
    out.append(graph_name)
    # 去连字符后与某个候选相同的形态：traffic-generator <-> trafficgenerator。
    # 只在与已有候选都不同时才加，避免重复查。
    for variant in _hyphen_variants(graph_name):
        if variant not in out:
            out.append(variant)
    return out


def _hyphen_variants(name: str) -> list[str]:
    """连字符的常见变体。只做**保守**的两种，不做任意分词。

    刻意不做「在每个可能位置插连字符」的暴力枚举：那会产出
    `pet-site` / `p-etsite` 一堆不存在的名字，把一次查询放大成几十次，
    而且可能误命中同前缀的别的服务。
    """
    out = []
    if '-' in name:
        out.append(name.replace('-', ''))
    else:
        # 已知的真实形态只有「在语义词边界加连字符」，无法从字符串推出来。
        # 这里只处理 traffic-generator 这一类：整体没有连字符时，
        # 交给调用方用 LIKE 模糊匹配兜底，不在这里瞎猜。
        pass
    return out


#: 图谱 `AWSServiceEndpoint` 名 -> X-Ray 服务图节点的 **Type 前缀**。
#:
#: ## 为什么必须按 Type 而不是按 Name 匹配（2026-09-09 从真实服务图归纳）
#:
#: 图谱的 AWSServiceEndpoint 是**服务级**抽象（`dynamodb`、`s3`），
#: 而 X-Ray 把这些表示成**资源级**节点，Name 是具体资源名：
#:
#:     AWS::DynamoDB::Table   ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM
#:     AWS::S3::Bucket        serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb
#:     AWS::SSM               SSM
#:     AWS::SimpleSystemsManagement  SimpleSystemsManagement
#:     AWS::STS               STS
#:
#: 所以 `_name_matches('dynamodb', 'ServicesEks2-ddbpetadoption...')` 永远是 False，
#: 而 `petsite -> ssm` 这条**已确证**的边在 X-Ray 里叫
#: `PetSite -> SimpleSystemsManagement`，按名字也匹配不上。
#:
#: 同一个服务可能有多个 Type（SSM 既有 `AWS::SSM` 又有
#: `AWS::SimpleSystemsManagement`），所以值是前缀列表。
_XRAY_AWS_TYPE_PREFIXES = {
    'dynamodb':       ('AWS::DynamoDB',),
    's3':             ('AWS::S3',),
    'sns':            ('AWS::SNS',),
    'sqs':            ('AWS::SQS',),
    'ssm':            ('AWS::SSM', 'AWS::SimpleSystemsManagement'),
    'sts':            ('AWS::STS',),
    'stepfunctions':  ('AWS::States', 'AWS::StepFunctions', 'AWS::SFN'),
    'secretsmanager': ('AWS::SecretsManager',),
    'xray':           ('AWS::XRay',),
    'bedrock':        ('AWS::Bedrock',),
}


def xray_aws_type_prefixes(graph_name: str) -> tuple:
    """给一个图谱 AWSServiceEndpoint 名，返回该匹配的 X-Ray Type 前缀。

    返回空元组表示「没有已知映射」—— 调用方**必须**把它与
    「匹配到但零调用」区分开，否则又是一次把「测不出」当「无流量」。
    """
    return _XRAY_AWS_TYPE_PREFIXES.get((graph_name or '').strip().lower(), ())

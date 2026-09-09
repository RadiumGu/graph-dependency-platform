"""tests/test_71_fis_api_injection_service_whitelist.py

FIS API 注入只支持 `ec2` / `kinesis`。写别的 service 会在**真跑时**才被
AWS 拒绝 —— dry-run 一路全绿，这是最费时间的一类失败。

## 实测（2026-09-09）

按交接书 `todo/task-tier0-fault-injection_20260909.md` 的建议，用
`aws:fis:inject-api-unavailable-error` 打 DynamoDB，试图验证
`petsearch -[AccessesData]-> DynamoDBTable`：

- dry-run 全绿：安全规则引擎放行、稳态基线通过、
  观测方基线识别正常（`petsite (AccessesData): success=99.9% total=1697`）
- 真跑时 AWS 拒绝：

      ValidationException: The service parameter value is not supported
      for the action. Specify a valid service and try again.

AWS FIS Actions reference 对 `aws:fis:inject-api-internal-error` 的原文：

    service – The target AWS API namespace.
    The supported value is `ec2` and `kinesis`.

## 为什么值得一条门禁

dry-run 不校验这个参数，于是「写好实验 → dry-run 通过 → 排期真跑 →
在注入那一刻失败」这条路要走完整个流程才知道错。而实验 YAML 是会被
批量生成的（`gen_fis_batch.py`、`experiments/generated/`），
一个不支持的 service 会复制到很多文件里。

本门禁在**静态阶段**就把它挡住。

## 顺带钉住一个既有隐患

仓库里 `fis-api-unavailable-rds.yaml` 写的是 `service: "rds"`，
按同一份文档它也不在白名单里。它大概从未真跑过 ——
别拿它当已验证的范式。本门禁会把它标出来，
所以它列在 `_KNOWN_UNRUNNABLE` 里而不是直接让测试红掉：
删掉或修正它是另一件事，这里只保证**不再新增**同类文件。
"""
from __future__ import annotations

import pathlib

import yaml

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
EXP_DIR = ROOT / 'chaos' / 'code' / 'experiments'

#: AWS FIS API 注入动作支持的 service 取值。
#: 来源：AWS FIS Actions reference（aws:fis:inject-api-* 的 service 参数）。
#: 改这个集合前先查文档 —— 它是 AWS 侧的能力边界，不是本仓库的约定。
FIS_API_SUPPORTED_SERVICES = {'ec2', 'kinesis'}

#: 使用 FIS API 注入的故障类型。
_API_FAULT_TYPES = {'fis_api_unavailable', 'fis_api_throttle',
                    'fis_api_internal_error'}

#: 已知写了不支持的 service、且**先于本门禁存在**的文件。
#: 它们大概从未真跑成功过。列在这里是为了让门禁能上线守住新增，
#: 而不是把修历史文件和加门禁绑成一件事。
_KNOWN_UNRUNNABLE = {
    'fis-api-unavailable-rds.yaml',
    # 2026-09-09 我自己按交接书建议写的，实测被 AWS 拒绝。
    # 保留文件是因为它的 IRSA 角色定位与观测方配置都是对的、可复用 ——
    # 只有 service 这一项走不通。
    'fis-api-unavailable-dynamodb-petsearch.yaml',
}


#: **先于本门禁存在**、且没有声明观测方的 API 注入实验。
#:
#: 它们不是坏文件，但**只能做稳态回归、不能用于边验证** ——
#: runner 会明确告知「本实验不能用于边验证」。列在这里的含义是：
#:
#:   · 门禁能上线守住新增文件，而不必把「修 4 个历史文件」和
#:     「加门禁」绑成一次改动；
#:   · 更重要的是把这笔债务**写在代码里**而不是留在某人记忆里 ——
#:     谁要统计「有多少实验能验边」时，这个清单就是答案的一部分。
#:
#: 想清掉某一项：给它加 observation_targets（写法见 t71_03 的报错文本），
#: 然后从这个清单里删掉。
_NO_OBSERVER_BASELINE = {
    'fis-api-internal-error-eks.yaml',
    'fis-api-throttle-eks.yaml',
    'fis-api-unavailable-rds.yaml',
    'payforadoption-fis-api-unavailable-h006.yaml',
}


def _api_injection_experiments() -> list[tuple[pathlib.Path, dict]]:
    out = []
    for p in sorted(EXP_DIR.rglob('*.yaml')):
        try:
            d = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
        except Exception:                                      # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        fault = d.get('fault') or {}
        if isinstance(fault, dict) and fault.get('type') in _API_FAULT_TYPES:
            out.append((p, d))
        for a in (d.get('faults') or []):
            if isinstance(a, dict) and a.get('type') in _API_FAULT_TYPES:
                out.append((p, d))
                break
    return out


def test_t71_01_不得新增写了不支持service的API注入实验():
    """dry-run 不校验这个参数，只有真跑到注入那一刻才会失败。"""
    bad = []
    for path, d in _api_injection_experiments():
        if path.name in _KNOWN_UNRUNNABLE:
            continue
        fault = d.get('fault') or {}
        extra = (fault.get('extra_params') or {}) if isinstance(fault, dict) else {}
        svc = str(extra.get('service') or '').strip().lower()
        if svc and svc not in FIS_API_SUPPORTED_SERVICES:
            bad.append(f'{path.relative_to(ROOT)}: service={svc!r}')
    assert not bad, (
        '这些 FIS API 注入实验写了不支持的 service:\n  '
        + '\n  '.join(bad)
        + f'\n\nAWS FIS API 注入只支持 {sorted(FIS_API_SUPPORTED_SERVICES)}。'
          f'\n写别的值 dry-run 一路全绿，只有真跑到注入那一刻才被 AWS 拒绝：'
          f'\n  ValidationException: The service parameter value is not supported'
          f'\n\n托管服务（DynamoDB/S3/RDS 数据面）的依赖验证请走复合实验'
          f'（scripts/verify_external_target_edges.py --compound）——'
          f'网络层切断也打不断（端点 IP 轮换、Gateway 端点、长连接+DNS 缓存）。'
    )


def test_t71_02_已知不可跑清单必须真的存在():
    """防止清单腐烂：文件被删/改名后，豁免项要跟着清掉。

    留着不存在的豁免项，会让下一个人以为某个问题还在，
    或者更糟 —— 新文件恰好用了同名就被静默豁免。
    """
    names = {p.name for p in EXP_DIR.rglob('*.yaml')}
    for label, lst in (('_KNOWN_UNRUNNABLE', _KNOWN_UNRUNNABLE),
                       ('_NO_OBSERVER_BASELINE', _NO_OBSERVER_BASELINE)):
        stale = sorted(n for n in lst if n not in names)
        assert not stale, (
            f'{label} 里这些文件已不存在，请从清单里删掉: {stale}\n'
            f'留着不存在的豁免项有两个害处：让人以为某个问题还在，'
            f'或者更糟 —— 新文件恰好同名就被静默豁免。')


def test_t71_03_API注入实验必须声明观测方():
    """没有 observation_targets 的实验**不能用于边验证**。

    runner 会明说「只能做稳态回归」——因为它只采注入目标自己的指标，
    而「打断 B 之后 B 是否退化」近乎恒真，根本没有检验任何边。
    这正是历史上 72 个实验全部 passed、零失败的原因。
    """
    missing = []
    for path, d in _api_injection_experiments():
        if path.name in _NO_OBSERVER_BASELINE:
            continue
        obs = d.get('observation_targets') or (d.get('target') or {}).get('observers')
        if not obs:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, (
        '这些 API 注入实验没有声明观测方，跑了也验不了边:\n  '
        + '\n  '.join(missing)
        + '\n\n写法: observation_targets: [{service: <上游>, edge_label: <边类型>,'
          ' min_baseline_requests: 20}]'
    )

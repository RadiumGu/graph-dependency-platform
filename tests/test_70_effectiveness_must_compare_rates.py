"""tests/test_70_effectiveness_must_compare_rates.py

生效性判定必须比**速率**而不是绝对计数。这条门禁来自一次真实的数据污染事故。

## 事故经过（2026-09-09）

跑 `petsearch -> DynamoDBTable` 与 `petsearch -> S3Bucket` 的注入实验。
`xray_metrics.took_effect` 当时拿不等长窗口的绝对计数相减：

    基线窗 1800s: 8470 次  ->  4.706 次/秒
    注入窗  120s:  548 次  ->  4.567 次/秒   ← 速率几乎没变

`thin = 8470 - 548 = 7922 > 0` 判成「打断确认生效」。而基线窗比注入窗
长 15 倍，绝对计数必然下降 —— **这个判据是恒真的**。

真相是注入完全没生效：`externalTargets` 只封住 apply 时解析出的那一个 IP，
而 AWS 区域端点有多个轮换 IP；S3 是 Gateway 端点，封 IP 根本不在路径上。
旁证是注入期 `petsite -> search-service` 响应码全 200、平均延迟
190-350ms、p99 恒定 ~3008ms —— 完全平坦。

## 污染是怎么发生的

假的 `injection_confirmed=True` 喂进判定链后：

    生效性=True + 观测方零退化 + 有独立观测源
      -> 「soft dependency（打断它本就不该影响调用方）」

于是两条**根本没验到**的边被写上了 `dependency_class=soft`。
`soft` 不是中性标签：DR 影响面分析会把它读成「这条依赖不影响可用性」，
在故障预案里降级。**用测量 bug 得出的 soft 比 untested 危险得多** ——
untested 只是没有信息，soft 是错误信息。

已用 `scripts/retract_false_soft_verdicts.py` 撤回，留痕在
`todo/retracted-false-soft-verdicts_*.json`。

## 这条门禁钉住什么

1. 不等长窗口下，速率不变必须判「未生效」
2. 速率真的塌下来才判生效
3. 判据里不得出现绝对计数相减
"""
from __future__ import annotations

import pathlib
import sys

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'chaos' / 'code',):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _fake_xray(base_count: int, inj_count: int):
    """构造一个服务图：基线窗与注入窗返回不同的调用数。

    `_edge_stats` 会按窗口长度被调两次，用调用顺序区分。
    """
    calls = {'n': 0}

    class _Paginator:
        def paginate(self, **kw):
            calls['n'] += 1
            span = int(kw['EndTime']) - int(kw['StartTime'])
            # 第一次问的是基线窗（更长），第二次是注入窗
            n = base_count if calls['n'] == 1 else inj_count
            return [{'Services': [
                {'ReferenceId': 1, 'Name': 'src', 'Type': 'AWS::Lambda',
                 'Edges': [{'ReferenceId': 2,
                            'SummaryStatistics': {'TotalCount': n,
                                                  'OkCount': n,
                                                  'TotalResponseTime': n * 0.01}}]},
                {'ReferenceId': 2, 'Name': 'dst', 'Type': 'AWS::DynamoDB::Table'},
            ], '_span': span}]

    class _XRay:
        def get_paginator(self, _n):
            return _Paginator()

    return _XRay()


def test_t70_01_速率不变必须判未生效():
    """事故的原始数字：1800s/8470 次 vs 120s/548 次 —— 速率几乎相同。

    旧实现判 True（因为 8470-548>0），正确答案是 False。
    """
    from runner.xray_metrics import XRayEdgeMetrics

    xr = XRayEdgeMetrics(client=_fake_xray(8470, 548))
    eff, why = xr.took_effect('src', 'dst',
                              injection_seconds=120, baseline_seconds=1800)
    assert eff is False, (
        f'速率 4.71 -> 4.57 次/秒（几乎没变）却判成 {eff}（{why}）。\n'
        f'这正是 2026-09-09 那次数据污染的成因：拿不等长窗口的绝对计数相减，'
        f'基线窗长 15 倍 -> 计数必然下降 -> 判据恒真。'
    )
    assert '次/秒' in why, f'理由里必须给出速率而不是只有计数: {why}'


def test_t70_02_速率真的塌了才判生效():
    """同样的窗口比例，但注入期速率降到约 1/10 —— 这才是真生效。"""
    from runner.xray_metrics import XRayEdgeMetrics

    # 基线 1800s/9000 次 = 5.0/s；注入 120s/60 次 = 0.5/s，降 90%
    xr = XRayEdgeMetrics(client=_fake_xray(9000, 60))
    eff, why = xr.took_effect('src', 'dst',
                              injection_seconds=120, baseline_seconds=1800)
    assert eff is True, f'速率降 90% 应判生效，实际 {eff}（{why}）'


def test_t70_03_调用完全停止必须判生效():
    from runner.xray_metrics import XRayEdgeMetrics

    xr = XRayEdgeMetrics(client=_fake_xray(9000, 0))
    eff, why = xr.took_effect('src', 'dst',
                              injection_seconds=120, baseline_seconds=1800)
    assert eff is True, f'注入期零调用应判生效，实际 {eff}（{why}）'


def test_t70_04_判据源码里不得再用绝对计数相减():
    """防回归：`b_total - i_total` 这种写法必须不再作为生效性判据。"""
    src = (ROOT / 'chaos' / 'code' / 'runner' / 'xray_metrics.py').read_text(
        encoding='utf-8')
    i = src.find('def took_effect')
    assert i != -1
    body = src[i:]
    assert 'thin = b_total - i_total' not in body, (
        '生效性判据里又出现了绝对计数相减 —— '
        '基线窗与注入窗长度不同时这个判据恒真，会产出假的 injection_confirmed。')
    assert '_QPS_DROP_THRESHOLD_PCT' in body, (
        '生效性判据必须用速率下降阈值 _QPS_DROP_THRESHOLD_PCT')


def test_t70_05_速率阈值必须显著高于成功率阈值():
    """数量信号比质量信号更容易被流量自然波动干扰。

    成功率退化 5% 是「请求失败了」，速率下降 5% 可能只是流量抖动。
    实测同一条边相邻窗口的速率自然波动可达 ±20%。
    """
    from runner.xray_metrics import _QPS_DROP_THRESHOLD_PCT

    assert _QPS_DROP_THRESHOLD_PCT >= 30.0, (
        f'速率下降阈值 {_QPS_DROP_THRESHOLD_PCT}% 太低 —— '
        f'X-Ray 分钟级聚合的自然抖动可达 ±20%，'
        f'阈值太低会把抖动读成「注入生效」。')

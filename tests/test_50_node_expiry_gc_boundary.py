"""节点过期收敛与 graph_gc 的职责边界守门测试。

锁住 2026-09-05 实测发现的机制冲突：

图里有两个机制在判「节点还该不该在」，判据不同：

    graph_gc      列出 AWS 真实存在的资源 → 图里不在这个集合的才删。比对**事实**。
    TTL 过期收敛  超过 expires_seconds 没被刷新 → 判过期。只知道**有没有被写过**。

实测冲突：7 个 `ServicesEks2-awscdkawseks-*` LambdaFunction 节点已 177 天未刷新
（`aws-etl` 采集范围收窄留下的孤儿），但 `lambda get-function` 逐个核验 **7/7 仍在**。
GC 判「该留」是对的；TTL 会判「该失活」是错的 —— 把活着的资源标成 active=false
是错误陈述，不是过期陈述。

本文件最要紧的一条是 test_m01：它**从 graph_gc.py 源码解析**出被 GC 覆盖的类型，
再断言排除集合覆盖它们。抄一份静态清单在这里是没用的 —— 将来有人给 graph_gc
新增一个类型，静态清单不会报错，冲突会静默回来。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
GC_SRC = REPO / 'infra' / 'lambda' / 'etl_aws' / 'graph_gc.py'

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


@pytest.fixture(scope='module')
def cleanup():
    import graph_cleanup
    return graph_cleanup


def _labels_gc_actually_reconciles() -> set[str]:
    """从 graph_gc.py 源码里解析出它实际比对的节点类型。

    用解析而不是维护第二份清单：两份清单必然漂移，而漂移方向恰好是
    「新增的 GC 类型忘了排除」——也就是缺陷会回来。
    """
    src = GC_SRC.read_text()
    return set(re.findall(r"_gc_vertices\(\s*'([A-Za-z][\w]*)'", src))


def test_m01_排除集合必须覆盖graph_gc实际比对的全部类型(cleanup):
    """本测试是这个文件存在的理由。"""
    gc_labels = _labels_gc_actually_reconciles()
    assert gc_labels, 'graph_gc.py 里应能解析出 _gc_vertices 调用；解析失效比断言失败更危险'
    missing = sorted(gc_labels - set(cleanup.GC_RECONCILED_LABELS))
    assert not missing, (
        f"graph_gc 会拿真实 AWS 状态比对这些类型，但它们没被 TTL 收敛排除：{missing}。\n"
        f"后果：TTL 只知道「没人刷新我」，会把 AWS 里仍存在、只是采集范围收窄后"
        f"不再被刷新的节点标成 active=false —— 那是错误陈述。\n"
        f"修法：把它们加进 graph_cleanup.GC_RECONCILED_LABELS。")


def test_m02_排除集合不该包含GC碰不到的类型(cleanup):
    """反向约束：多排除也是错的 —— 那些类型就没人管了。

    Pod 是典型：graph_gc 用 AWS API，看不见 EKS 里的 Pod，所以 Pod 只能靠 TTL。
    把 Pod 误加进排除集合，744 个 Pod 节点（集群实际只有 74 个）就永远不会收敛。
    """
    gc_labels = _labels_gc_actually_reconciles()
    extra = sorted(set(cleanup.GC_RECONCILED_LABELS) - gc_labels)
    assert not extra, (
        f"这些类型不在 graph_gc 的比对范围内，却被 TTL 收敛排除了：{extra}。\n"
        f"结果是两个机制都不管它们，节点会无限累积。")
    assert 'Pod' not in cleanup.GC_RECONCILED_LABELS, (
        'Pod 必须留给 TTL —— graph_gc 用 AWS API，看不到 EKS 里的 Pod')


def test_m03_expiring_node_labels已排除GC类型(cleanup):
    labels = {lb for lb, _ in cleanup.expiring_node_labels()}
    overlap = sorted(labels & set(cleanup.GC_RECONCILED_LABELS))
    assert not overlap, f'expiring_node_labels 仍返回了 GC 覆盖的类型: {overlap}'
    assert 'Pod' in labels, 'Pod 必须仍在 TTL 收敛范围内'


def test_m04_收敛查询绝不触碰GC覆盖的类型(cleanup, monkeypatch):
    """端到端：跑一遍 expire_stale_nodes，检查发出的查询里没有 GC 类型。"""
    monkeypatch.setattr(cleanup, 'node_expiry_enabled', lambda: True)
    seen: list[str] = []

    def fake(q):
        seen.append(q)
        return {'result': {'data': {'@value': [1]}}}

    cleanup.expire_stale_nodes(fake, round_ts=1_000_000)
    assert seen, '应该发出了查询'
    for lb in cleanup.GC_RECONCILED_LABELS:
        offending = [q for q in seen if f"hasLabel('{lb}')" in q]
        assert not offending, (
            f'收敛查询触碰了 GC 覆盖的类型 {lb}：{offending[0][:140]}')
    assert any("hasLabel('Pod')" in q for q in seen), 'Pod 应被收敛'


def test_m05_默认dry_run(cleanup, monkeypatch):
    """与边侧同一姿态：部署代码不等于立刻改图。"""
    monkeypatch.setattr(cleanup, 'node_expiry_enabled', lambda: False)
    seen: list[str] = []

    def fake(q):
        seen.append(q)
        return {'result': {'data': {'@value': [3]}}}

    out = cleanup.expire_stale_nodes(fake, round_ts=1_000_000)
    assert out['enabled'] is False
    assert all(v['expired'] == 0 for v in out['per_label'].values())
    assert not [q for q in seen if "property('active'" in q], \
        '未开启时不得有任何写入查询'
    assert any(v['stale'] > 0 for v in out['per_label'].values()), \
        'dry-run 仍要统计，否则无法评估开启后的影响面'


def test_m06_缺时间戳的节点单独计数而不是当成不过期(cleanup, monkeypatch):
    """`unjudgeable` 必须被带出来 —— 缺判据字段的节点是「判不了」而非「没问题」。

    模块注释记着这条判据的来由：2026-09-04 实测 1077 个节点里只有 15 个带
    last_seen，覆盖率 1.4%。那种状态下所有 count 都返回 0，
    「0 条陈旧」会被读成「图谱很干净」—— 与真的干净完全同形。
    """
    monkeypatch.setattr(cleanup, 'node_expiry_enabled', lambda: True)

    def fake(q):
        # 「缺时间戳」查询的形状是 .not(__.has('last_seen')) 且不带 lt( 比较；
        # 「陈旧」查询带 lt(cutoff)。用 lt( 区分，而不是 hasNot ——
        # 实现用的是 .not(__.has(...))，第一版测试拿 hasNot 判别，判错了。
        is_unjudgeable = 'lt(' not in q
        return {'result': {'data': {'@value': [5 if is_unjudgeable else 0]}}}

    out = cleanup.expire_stale_nodes(fake, round_ts=1_000_000)
    assert out['unjudgeable_total'] > 0, \
        '缺 last_seen 的节点数必须上报，否则权限/采集缺失会伪装成 0 风险'
    assert all(v['stale'] == 0 for v in out['per_label'].values())
    assert all(v['unjudgeable'] == 5 for v in out['per_label'].values())

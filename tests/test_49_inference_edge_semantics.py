"""稀疏观测边（dependency_kind='inference'）的守门测试。

锁住 2026-09-05 修的这个语义错误：

agent 的工具调用被标成 `dependency_kind='dynamic'`，于是
`deactivate_stale_dynamic_edges` 把 6 小时没被观测到的 `Retrieves -> nutrition-kb`
置成了 `active=false`。而那个知识库客观存在（控制面 list_knowledge_bases 返回它）、
nutrition agent 也确实依赖它 —— 只是几个小时没人问营养问题。
**图谱因此给出了一个错误陈述，而不是过期陈述。**

违反的是本项目最核心的不变量：
    零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted

为什么「把 6h 窗口写进文档」不算修复：本项目自己实测过
「漂移量与有没有门禁相关，与声明得好不好无关」—— 同一份 YAML 里被
assert_edge_type 检查的类型零漂移。注释到不了 `has('active', true)` 的消费方。
所以这里用测试钉住，而不是只写注释。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
ETL = REPO / 'infra' / 'lambda' / 'etl_agentcore' / 'neptune_etl_agentcore.py'

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


@pytest.fixture(scope='module')
def cleanup():
    import graph_cleanup
    return graph_cleanup


@pytest.fixture(scope='module')
def etl_src() -> str:
    return ETL.read_text()


class _Recorder:
    """记录发给 Neptune 的每条查询，计数查询统一返回 n。"""

    def __init__(self, n: int = 3):
        self.queries: list[str] = []
        self.n = n

    def __call__(self, q: str):
        self.queries.append(q)
        if '.count()' in q:
            return {'result': {'data': {'@value': [self.n]}}}
        return {'result': {'data': {'@value': []}}}


def test_m01_dynamic清扫绝不触碰inference边(cleanup, monkeypatch):
    """这是整个修复的核心断言。"""
    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: True)
    rec = _Recorder(n=5)
    cleanup.deactivate_stale_dynamic_edges(rec, round_ts=1_000_000)
    assert rec.queries, '应该发出了查询'
    for q in rec.queries:
        assert "has('dependency_kind','dynamic')" in q, (
            f"清扫查询必须显式限定 dynamic，否则会误伤 static 与 inference：{q[:160]}")
        assert "'inference'" not in q, f'清扫路径不得出现 inference：{q[:160]}'


def test_m02_inference标记绝不改写active(cleanup, monkeypatch):
    """标记路径只写诊断属性。碰 active 就等于把不确定性伪装成确定的否定。"""
    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: True)
    rec = _Recorder(n=2)
    out = cleanup.mark_stale_inference_edges(rec, round_ts=1_000_000)
    assert out['enabled'] is True
    writes = [q for q in rec.queries if '.iterate()' in q]
    assert writes, '开启后应该有写入查询'
    for q in writes:
        assert "property('active'" not in q, (
            f"标记路径绝不能改写 active —— active=false 断言「依赖不存在」，"
            f"而我们只能证明「窗口内没观测到」：{q[:200]}")
        assert "has('dependency_kind','inference')" in q, (
            f'标记路径必须限定 inference：{q[:160]}')
    assert any("drift_status','observed_then_silent'" in q for q in writes), \
        '必须写 drift_status=observed_then_silent'


def test_m03_重新被观测到要把drift_status翻回ok(cleanup, monkeypatch):
    """少了这步，边被标 silent 后即使 agent 又开始调用也永远显示 silent。"""
    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: True)
    rec = _Recorder(n=0)  # 零条陈旧 —— 复位查询仍必须发出
    cleanup.mark_stale_inference_edges(rec, round_ts=1_000_000)
    resets = [q for q in rec.queries
              if "drift_status','ok'" in q and '.iterate()' in q]
    assert resets, '即使本轮没有陈旧边，也必须尝试把已刷新的边翻回 ok'
    for q in resets:
        assert 'gte(' in q, f'复位判据应是 last_seen >= cutoff：{q[:160]}'


def test_m04_默认dry_run不改图(cleanup, monkeypatch):
    """与 deactivate 同一姿态：部署代码不等于立刻改图。"""
    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: False)
    rec = _Recorder(n=7)
    out = cleanup.mark_stale_inference_edges(rec, round_ts=1_000_000)
    assert out['enabled'] is False
    assert all(v['marked'] == 0 for v in out['per_label'].values())
    assert not [q for q in rec.queries if '.iterate()' in q], \
        '未开启时不得有任何写入查询'
    assert any(v['silent'] > 0 for v in out['per_label'].values()), \
        'dry-run 仍要统计，否则无法评估开启后的影响面'


def test_m05_agentcore的ETL不再写dynamic(etl_src):
    """回归锁：agent 的调用不满足 dynamic「持续观测到流量」的定义。

    断言只针对**写入语句**（`property(...)`），不针对文档串 —— 注释里引用旧行为
    （`has('dependency_kind','dynamic')`）是必要的，那是缺陷记录的一部分。
    第一版把两者混在一起，结果测试匹配到了自己的文档。
    """
    flat = etl_src.replace(' ', '').replace('\n', '')
    assert "property('dependency_kind','dynamic')" not in flat, \
        'etl_agentcore 的写入语句不得再把边标成 dynamic'
    assert "property('dependency_kind','{dependency_kind}')" in flat, \
        '写入语句应使用参数化的 dependency_kind'
    assert "dependency_kind: str = 'inference'" in etl_src, \
        '_upsert_edge 的默认取值应为 inference'


def test_m06_控制面来源的边标static而非inference(etl_src):
    """RoutesTo 与 gateway 侧 DependsOn 来自控制面，与流量无关。"""
    # 找出所有显式传 dependency_kind 的调用点
    explicit = re.findall(r"_upsert_edge\(\s*'(\w+)'.*?dependency_kind='(\w+)'",
                          etl_src, re.S)
    kinds = dict(explicit)
    assert kinds.get('RoutesTo') == 'static', (
        f'RoutesTo 来自 Gateway 的 target 列表，应为 static，实际 {kinds.get("RoutesTo")}')


def test_m07_三种kind的作用域互不重叠(cleanup, monkeypatch):
    """static 既不被清扫也不被标记 —— 它的存在性不依赖观测。"""
    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: True)
    rec_d, rec_i = _Recorder(n=1), _Recorder(n=1)
    cleanup.deactivate_stale_dynamic_edges(rec_d, round_ts=1_000_000)
    cleanup.mark_stale_inference_edges(rec_i, round_ts=1_000_000)
    for q in rec_d.queries + rec_i.queries:
        assert "'static'" not in q, (
            f"static 边不该出现在任何观测式失效路径里 —— 那是 drift_status 的 "
            f"declared_not_observed 要表达的信息：{q[:160]}")


def test_m08_稀疏源边只被标记不被失效(cleanup, monkeypatch):
    """契约声明的 sparse_observation_sources 必须两条路径互补覆盖。

    **互补性是整个机制的要害**：这些边的 `dependency_kind` 是 `dynamic`
    （它们确实是运行时观测来的），所以失效路径天然会命中它们。如果只把它们
    加进标记路径而忘了从失效路径排除，两条路径会同时碰同一条边，
    而失效那条会写下 `active=false` —— 恰好是本机制要防的那个 bug，
    且症状隐蔽（标记也做了，看起来像生效了）。

    为什么 DNS 源需要这个待遇（2026-09-07 实测）：
      · 22 条 deepflow-dns 边里 4 条的 last_drift_check 停在 172 天前、
        12 条停在 9 天前，而 AccessesData 的 TTL 是 6 小时。
      · 原因不是依赖消失，而是 DNS 可见性纯由**连接行为**决定 ——
        持连接池的 pod 一次解析后长期不再查询，会重连的服务每天几百次。
      · 按 dynamic 处置会把它们全判 active=false，即断言「依赖不存在」，
        而能证明的只是「6 小时窗口内没解析」。

    这与 2026-09-05 的 `Retrieves -> nutrition-kb` 事故同源：
    图谱给出的是**错误陈述**，不是过期陈述。
    """
    srcs = sorted(cleanup._sparse_sources())
    assert srcs, (
        '契约应声明 sparse_observation_sources 且生成器要导出它。'
        '为空说明 gen_graph_contract.py 没同步 —— 稀疏源边会回落到 dynamic 处置。')

    monkeypatch.setattr(cleanup, 'expiry_enabled', lambda: True)
    rec_d, rec_i = _Recorder(n=1), _Recorder(n=1)
    cleanup.deactivate_stale_dynamic_edges(rec_d, round_ts=1_000_000)
    cleanup.mark_stale_inference_edges(rec_i, round_ts=1_000_000)

    for s in srcs:
        # 失效路径必须**排除**它
        for q in rec_d.queries:
            assert f"not(__.has('source',within(" in q and s in q, (
                f"失效路径未排除稀疏源 {s} —— 这些边的 dependency_kind 是 dynamic，"
                f"不显式排除就会被置 active=false：{q[:200]}")
        # 标记路径必须**包含**它
        for q in rec_i.queries:
            assert s in q, (
                f"标记路径未覆盖稀疏源 {s}，它将得不到 observed_then_silent 标记，"
                f"于是既不失效也不标陈旧 —— 变成永不过期的墓碑：{q[:200]}")

    # 标记路径绝不能碰 active（与 m02 同一不变量，这里对稀疏源再钉一次）
    for q in rec_i.queries:
        assert "'active'" not in q, (
            f"稀疏源边的标记路径不得改写 active：{q[:200]}")

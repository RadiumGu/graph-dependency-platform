"""
test_132_etl_collection_must_not_truncate_silently.py

守两条不变量，都是 2026-10-07 评估 DeepFlow 拓扑能力时发现的缺陷
（docs/lessons/deepflow-topology-api-vs-self-built-etl-2026-10.md）：

① **ClickHouse 结果集必须有上限，且超限抛异常而不是静默截断。**
   原来 etl_deepflow 的主调用图查询写死 `ORDER BY calls DESC LIMIT 100`，
   第 101 条边起无声丢掉。依赖图要喂 DR 的拓扑排序，少一条边可能把恢复
   顺序排错，而且无从察觉。

② **采集失败必须留痕，且不得驱动失活判定。**
   `ch_query` 原来失败一律 `except` 后 `return []` —— 「ClickHouse 挂了」
   和「这段时间真的没有依赖」在下游长得一模一样。而 reconcile_calls_edges
   会把本轮没观测到的边置 `active=false`，于是一次 ClickHouse 故障就能
   让一批健康依赖被集体标死。

这两条与 tests/test_131（boto3 分页上限与限流退避）是同一个判据在
不同数据通路上的两次落地：**宁可失败得吵，也不要悄悄给出不完整的结果。**
test_131 的护栏装在 boto3 分页器上，管不到手写 SQL，所以要这一条补位。

本文件是纯静态/单元门禁，不连 ClickHouse、不连 Neptune。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

ETL = (pathlib.Path(__file__).resolve().parents[1]
       / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py')


@pytest.fixture(scope='module')
def src() -> str:
    return ETL.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def mod():
    """只加载模块取其纯函数，不触发任何 I/O。

    该文件 import 时会读 profile 并建 boto3 session，但不发请求，
    所以直接 import 是安全的；真正发请求的是 fetch_* / run_etl。
    """
    sys.path.insert(0, str(ETL.parent))
    sys.path.insert(0, str(ETL.resolve().parents[2] / 'lambda' / 'shared' / 'python'))
    spec = importlib.util.spec_from_file_location('_etl_deepflow_under_test', ETL)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"etl_deepflow 无法在测试环境 import：{e!r}")
    return m


# ---------------------------------------------------------------- 不变量 ①

def test_t132_01_no_hardcoded_limit_in_sql(src: str):
    """SQL 里不得再出现写死的小 LIMIT —— 那就是静默截断。

    允许 `LIMIT {max_rows + 1}` 这种由 ch_query_bounded 拼出来的形式，
    以及 Gremlin 侧的 limit()。只禁 SQL 文本里的字面量 LIMIT <数字>。
    """
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # 跳过注释行：讲述这段历史的注释本身会提到旧的 `LIMIT 100`。
        if stripped.startswith('#'):
            continue
        m = re.search(r'\bLIMIT\s+(\d+)\b', line)
        if not m:
            continue
        # ch_query_bounded 自己拼的那行是唯一合法来源
        if 'max_rows' in line:
            continue
        offenders.append(f"  第 {i} 行: {stripped[:110]}")
    assert not offenders, (
        "SQL 里出现写死的 LIMIT —— 这是静默截断，超出的行会无声丢掉。\n"
        + "\n".join(offenders)
        + "\n\n请改走 ch_query_bounded（超限抛 ClickHouseResultTruncated），"
          "理由见 tests/test_132 的模块 docstring。"
    )


def test_t132_02_truncation_raises_not_returns(mod):
    """超限必须抛 ClickHouseResultTruncated，而不是返回部分结果。"""
    assert hasattr(mod, 'ClickHouseResultTruncated'), "缺少 ClickHouseResultTruncated"
    assert issubclass(mod.ClickHouseResultTruncated, Exception)

    calls = {'n': 0}

    def fake_ch_query(sql, what='x'):
        calls['n'] += 1
        # 返回 max_rows + 1 行，模拟「上游还有更多」
        n = mod.CH_MAX_ROWS + 1
        return [['a', 'b'] for _ in range(n)]

    orig = mod.ch_query
    mod.ch_query = fake_ch_query
    try:
        with pytest.raises(mod.ClickHouseResultTruncated):
            mod.ch_query_bounded("SELECT 1", what='unit-test')
    finally:
        mod.ch_query = orig
    assert calls['n'] == 1, "ch_query_bounded 应只发一次查询（LIMIT cap+1 探测）"


def test_t132_03_bounded_probes_with_cap_plus_one(mod):
    """必须用 cap+1 探测，否则拿满 cap 行时分不清是刚好还是被截断。"""
    seen = {}

    def fake_ch_query(sql, what='x'):
        seen['sql'] = sql
        return [['a']]

    orig = mod.ch_query
    mod.ch_query = fake_ch_query
    try:
        mod.ch_query_bounded("SELECT 1", what='unit-test', max_rows=7)
    finally:
        mod.ch_query = orig
    assert 'LIMIT 8' in seen['sql'], f"应以 max_rows+1 探测，实际 SQL：{seen['sql']!r}"
    assert 'FORMAT TSV' in seen['sql'], "ch_query 解析的是 TSV，必须带 FORMAT TSV"


def test_t132_04_under_cap_returns_rows(mod):
    """未超限时正常返回，不得因为加了护栏就改变正常路径行为。"""
    def fake_ch_query(sql, what='x'):
        return [['a'], ['b'], ['c']]

    orig = mod.ch_query
    mod.ch_query = fake_ch_query
    try:
        rows = mod.ch_query_bounded("SELECT 1", what='unit-test', max_rows=10)
    finally:
        mod.ch_query = orig
    assert rows == [['a'], ['b'], ['c']]


# ---------------------------------------------------------------- 不变量 ②

def test_t132_05_collection_health_api_exists(mod):
    for fn in ('reset_collection_health', 'note_collection_failure',
               'note_collection_truncation', 'collection_is_complete',
               'collection_health_summary'):
        assert hasattr(mod, fn), f"缺少 {fn}"


def test_t132_06_failure_marks_round_incomplete(mod):
    """采集失败后本轮必须判为不完整 —— 这是跳过失活对账的依据。"""
    mod.reset_collection_health()
    assert mod.collection_is_complete(), "reset 后应为完整"
    mod.note_collection_failure('unit-test', RuntimeError('boom'))
    assert not mod.collection_is_complete(), "有采集失败却仍判为完整"
    s = mod.collection_health_summary()
    assert s['complete'] is False
    assert s['failures'] and s['failures'][0]['what'] == 'unit-test'
    mod.reset_collection_health()


def test_t132_07_truncation_marks_round_incomplete(mod):
    mod.reset_collection_health()
    mod.note_collection_truncation('unit-test', 5000)
    assert not mod.collection_is_complete(), "有截断却仍判为完整"
    mod.reset_collection_health()


def test_t132_08_ch_query_records_failure_not_silent(mod):
    """ch_query 失败时必须留痕，不能只是 return []。"""
    mod.reset_collection_health()

    class _Boom:
        @staticmethod
        def post(*a, **kw):
            raise OSError('connection refused')

    real_import = __builtins__['__import__'] if isinstance(__builtins__, dict) else __import__

    def fake_import(name, *a, **kw):
        if name == 'requests':
            return _Boom
        return real_import(name, *a, **kw)

    import builtins
    builtins.__import__ = fake_import
    try:
        rows = mod.ch_query("SELECT 1", what='unit-test-boom')
    finally:
        builtins.__import__ = real_import

    assert rows == [], "失败时仍应返回空列表（保持其它源继续采集）"
    assert not mod.collection_is_complete(), (
        "ch_query 失败了但本轮仍判为完整 —— 这正是「采集失败与真的没数据不可区分」"
        "那个缺陷。失败必须经 note_collection_failure 留痕。"
    )
    mod.reset_collection_health()


def test_t132_09_reconcile_is_gated_on_completeness(src: str):
    """失活对账必须被 collection_is_complete() 守住。

    这是本文件最重要的一条：护栏存在但没接到危险路径上，等于没有。
    """
    m = re.search(r'if\s+collection_is_complete\(\)\s*:\s*\n\s*reconcile_stats\s*=\s*reconcile_calls_edges\(',
                  src)
    assert m, (
        "没找到 `if collection_is_complete(): reconcile_stats = reconcile_calls_edges(...)` 这个形状。\n"
        "reconcile_calls_edges 会把本轮没观测到的边置 active=false，"
        "采集不完整时调用它会误标死健康依赖。"
    )


def test_t132_10_incomplete_round_leaves_graph_trace(src: str):
    """采集不完整时要在图里留痕，而不是只写日志。

    用 TopologyChange 事件 —— 该节点类型的契约 writer 就是 etl_deepflow。
    """
    assert 'collection_incomplete' in src, "缺少 collection_incomplete 事件 kind"
    seg = src[src.find('collection_incomplete') - 1200: src.find('collection_incomplete') + 600]
    assert '_emit_topology_changes' in seg, (
        "collection_incomplete 没有经 _emit_topology_changes 落图 —— "
        "只写日志的话下游仍然无法区分「这条边不存在」与「这次没采到」。"
    )


def test_t132_11_must_not_write_verify_attrs(src: str):
    """采集侧不得写 verify_* —— 那组属性的 authority 是 ['chaos-runner']。

    这条守的是写权限边界：edge_verification 的六档证据状态只有混沌实验
    有权写。采集侧想表达「没采到」必须另开属性（本仓选择 TopologyChange
    事件），不能借用证据等级。
    """
    offenders = [f"  第 {i} 行: {ln.strip()[:100]}"
                 for i, ln in enumerate(src.splitlines(), 1)
                 if re.search(r"['\"]verify_(status|confidence|last|by|experiment"
                              r"|degradation|blocked_reason|blocked_class)['\"]", ln)]
    assert not offenders, (
        "etl_deepflow 写了 verify_* 属性，但 edge_verification 的 authority 是 "
        "['chaos-runner'] —— 采集侧无权写。\n" + "\n".join(offenders)
    )


def test_t132_12_contract_authority_still_chaos_runner():
    """反向固定前提：如果契约把 authority 放宽了，上面那条门禁的理由就变了。"""
    import graph_contract_data as d
    ev = getattr(d, 'EDGE_VERIFICATION', None)
    if not ev:
        pytest.skip("契约里没有 EDGE_VERIFICATION")
    assert ev.get('authority') == ['chaos-runner'], (
        f"EDGE_VERIFICATION.authority 变了：{ev.get('authority')}。"
        "test_132_11 的理由建立在「只有 chaos-runner 能写 verify_*」之上，"
        "契约放宽时请一起重新评估采集侧该怎么表达「没采到」。"
    )

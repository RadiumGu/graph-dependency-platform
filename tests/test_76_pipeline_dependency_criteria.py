"""内部数据管道类依赖的判据门禁。

守的是一件事：**这类边的观测方是产出物，不是用户功能。**

背景（2026-09-17）：`LambdaFunction neptune-etl-from-xray ->
NeptuneCluster petsite-neptune` 这条边，能力上可注入
（IAM 认证已开、Lambda 有执行角色、`neptune-db:*` 能 deny），
但拿 `business_probes` 去验会得到"业务未退化" ——
因为 ETL 的消费者是**图谱自己**，没有任何用户功能会变化。

这不是"依赖不承重"，是**观测方选错了**。
本项目在 agent 委派边上已经因为同一个错误误判三次。
"""

import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 只加 chaos/code —— 直接把 runner/ 加进 sys.path 会让 runner 被当成
# 模块而不是包，其内部相对 import 全炸（test_47 的注释里有完整记载，
# 而 test_67/68 曾因此让 test_47 的 18 个 fixture 全 error）。
for p in (str(_ROOT / 'chaos' / 'code'),):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_t76_01_产出物探针区分探针失败与产出停滞():
    """`ok=False`（查不到）与 `value=0`（停滞）必须是两件事。

    混成一件就会造出假 confirmed：查不到数据的形态与
    "产出真的停了"在结构上无法区分，而前者只说明探针瞎了。
    这是本仓库反复踩的同一个坑（X-Ray 边级采集 `ok=False` 那次）。
    """
    from runner import pipeline_probes as pp

    class _NoData:
        def results(self, *_a, **_k):
            return [{"newest": None}]

    r = pp.probe_pipeline_freshness(_NoData(), 'xray')
    assert r['ok'] is False, '查不到 last_seen 时必须 ok=False'
    assert '探针失败' in r['detail'], (
        'detail 里必须说清这是探针失败而不是产出停滞 —— '
        '读的人会把 value=0 当成"管道断了"')


def test_t76_02_新鲜度阈值必须留周期余量():
    """阈值不能刚好等于一个调度周期。

    `neptune-etl-from-xray` 是 `rate(1 hour)`，正常情况下产出物
    最旧就落后一个周期。阈值取 1 小时会把"刚好卡在周期边界"
    判成停滞 —— 那是假 confirmed，比漏判更糟。
    """
    from runner import pipeline_probes as pp
    assert pp.FRESH_WITHIN_SECONDS >= 2 * 3600, (
        f'FRESH_WITHIN_SECONDS={pp.FRESH_WITHIN_SECONDS} 太紧 —— '
        f'ETL 周期是 1 小时，至少要 2 倍余量')


def test_t76_03_只登记已核实幂等的源():
    """`PIPELINE_OUTPUTS` 是主动触发的白名单，纪律与 SEVERANCE_METHODS 同源。

    主动触发意味着**真的再跑一次那个源**。不幂等的源会把混沌实验
    变成数据损坏，所以登记前必须核实幂等。

    本用例钉的是"文档写清了这条纪律"，不是自动验证幂等
    （幂等要读源码判断，测试里做不了）。
    """
    from runner import pipeline_probes as pp
    assert pp.PIPELINE_OUTPUTS, 'PIPELINE_OUTPUTS 不该为空'
    doc = pp.__doc__ or ''
    src = pathlib.Path(pp.__file__).read_text(encoding='utf-8')
    assert '幂等' in doc or '幂等' in src, (
        '没有写明主动触发需要幂等 —— '
        '下一个人会给一个非幂等的源登记条目，然后损坏数据')
    assert '不幂等' in src, (
        '没有明确写出"不幂等的源不得用这个手段"这条禁令')


def test_t76_04_未登记返回None的含义必须写清():
    """`output_source_for` 返回 None 是"未核实"，不是"不可验"。

    这个区分很关键：把"未核实"读成"不可验"会让这类边永久停在
    blocked，而它们其实只是还没人去核实幂等。
    """
    from runner import pipeline_probes as pp
    assert pp.output_source_for('petsite') is None
    assert pp.output_source_for('neptune-etl-from-xray') == 'xray'
    doc = pp.output_source_for.__doc__ or ''
    assert '未核实' in doc or '还没核实' in doc, (
        'output_source_for 的 docstring 没说清 None 的含义 —— '
        '会被读成"这类边不可验"')


def test_t76_05_产出物活性必须看时间戳而非数量():
    """图谱是 upsert，切断 ETL 不会让边消失，所以数量对此完全不敏感。

    拿 `count()` 当判据会永远判"未退化"。
    """
    from runner import pipeline_probes as pp
    import inspect, ast, textwrap
    body = inspect.getsource(pp._newest_last_seen)
    assert 'max(r.last_seen)' in body, (
        '产出物活性判据不是 max(last_seen) —— '
        '用数量会永远判未退化，因为图谱是 upsert 而非重建')
    # 只看**代码**，不看注释与 docstring —— 那里正解释着
    # "为什么不用 count()"，按文本搜会命中自己的说明文字。
    # 用 dedent 而不是 cleandoc：后者会重排函数体缩进导致语法错误。
    tree = ast.parse(textwrap.dedent(body))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    doc = ast.get_docstring(fn) or ''
    queries = [s for s in literals if s != doc and 'MATCH' in s]
    assert queries, '取不到查询语句 —— 断言失去意义，改坏了就该发现'
    for q in queries:
        assert 'count(' not in q, (
            f'查询里出现了 count：{q[:80]} —— 数量不是活性信号')


def test_t76_06_触发成功但产出未推进必须单独记():
    """`invoke` 成功而产出物没前进，与 `invoke` 失败，是两种证据。

    前者说明源可能自己吞了错误 —— 本项目已经在 agent 委派上
    被这种吞错骗过（`_via_gateway` 把 403 包成 JSON 返回给 LLM，
    业务探针据此判"未退化"，我因此误判三次）。
    """
    from runner import pipeline_probes as pp
    src = pathlib.Path(pp.__file__).read_text(encoding='utf-8')
    assert 'invoke_error' in src, (
        'trigger_and_observe 没有单独记录 invoke 的错误 —— '
        '"源吞了错误"与"源正常但写不进去"会混成一个结论')
    i = src.index('def trigger_and_observe')
    body = src[i:i + 2600]
    assert '吞' in body, (
        'trigger_and_observe 的注释没警告"源可能吞错误"这个模式 —— '
        '那正是 agent 委派边误判三次的原因')

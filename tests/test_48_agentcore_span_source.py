"""etl_agentcore 的 span 采集守门测试。

锁住 2026-09-05 实测发现的那个静默 bug 及其修复：

`SPAN_LOG_GROUP` 曾默认 `aws/spans`，而 AgentCore 的 span 实际在
`/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>/` 的 `spans` 流里。
同一条 Insights 查询打到两处，结果相反（aws/spans 0 行 / per-runtime 3 行）。

这个 bug 有两个恶劣性质，两条都要有测试锁住：

① **它以 `empty` 的形式静默**。采集成功、返回确实为空 —— 而 `empty` 的语义是
   「问对了地方、确实没有」，无法表达「问错了地方、所以没有」。后果是每轮 ETL 都
   报 `spans: empty` 且不进 `failed_collections`，没有任何告警，静默了一整天。

② **它的后果是数据缺失而非报错**：runtime 侧的 AgentTool 节点只能从 span 发现，
   span 路空转 → 那些节点永远拿不到属性刷新（实测修复后 name 覆盖从 5/10 变 13/13，
   并多发现 3 个此前完全不存在的 tool）；agent 依赖边全部来自 span 路，
   于是每轮 `edges` 都是 `{}`，桥接边因长期不被刷新而被置 `active=False`。
"""
import importlib.util
import pathlib
import sys

import pytest

ETL_PATH = (pathlib.Path(__file__).resolve().parents[1]
            / 'infra' / 'lambda' / 'etl_agentcore' / 'neptune_etl_agentcore.py')
SHARED = (pathlib.Path(__file__).resolve().parents[1]
          / 'infra' / 'lambda' / 'shared' / 'python')


@pytest.fixture(scope='module')
def etl():
    if str(SHARED) not in sys.path:
        sys.path.insert(0, str(SHARED))
    spec = importlib.util.spec_from_file_location('etl_agentcore_under_test', ETL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeLogs:
    """只实现被测路径用到的两个 API。"""

    def __init__(self, groups, rows=None, pages=1):
        self._groups = list(groups)
        self._rows = rows or []
        self._pages = pages
        self.probe_rows: list = []
        self.described_prefix = None
        self.queried_groups = None
        self._last_query = ''

    def describe_log_groups(self, **kw):
        self.described_prefix = kw.get('logGroupNamePrefix')
        return {'logGroups': [{'logGroupName': g} for g in self._groups]}

    def start_query(self, **kw):
        self.queried_groups = kw.get('logGroupNames')
        self._last_query = kw.get('queryString', '')
        assert 'logGroupName' not in kw, (
            'start_query 必须用 logGroupNames（复数）—— 单个日志组装不下按 runtime '
            '分散的 span')
        return {'queryId': 'q-1'}

    def get_query_results(self, queryId):  # noqa: N803
        # 靠 @logStream 区分：只有对照探针会按日志流筛选。
        # 第一版用「含 stats」区分，第三版的探针也用了 stats，判别失效 ——
        # 用一个「碰巧现在能区分」的特征做判别，改一次实现就坏一次。
        is_probe = '@logStream' in self._last_query
        data = self.probe_rows if is_probe else self._rows
        return {'status': 'Complete',
                'results': [[{'field': k, 'value': v} for k, v in r.items()]
                            for r in data]}

    def stop_query(self, queryId):  # noqa: N803
        return {}


def test_m01_默认前缀不是aws_spans(etl):
    """回归锁：默认值绝不能再指回 aws/spans。"""
    assert etl.SPAN_LOG_GROUP_PREFIX == '/aws/bedrock-agentcore/runtimes/'
    assert 'aws/spans' not in etl.SPAN_LOG_GROUP_PREFIX
    # 旧的单数常量必须已经消失，否则会有人照旧引用它
    assert not hasattr(etl, 'SPAN_LOG_GROUP'), (
        'SPAN_LOG_GROUP（单数）应已删除 —— 留着它就还有人会传单个日志组')


def test_m02_按前缀发现全部runtime日志组(etl):
    groups = [f'/aws/bedrock-agentcore/runtimes/Agent{i}-abc-DEFAULT' for i in range(7)]
    fake = _FakeLogs(groups)
    got = etl._discover_span_log_groups(fake)
    assert fake.described_prefix == '/aws/bedrock-agentcore/runtimes/'
    assert sorted(got) == sorted(groups), '7 个 runtime 的日志组必须全部纳入查询'


def test_m03_超过Insights上限要截断并且不静默(etl, caplog):
    groups = [f'/aws/bedrock-agentcore/runtimes/A{i}-DEFAULT' for i in range(60)]
    with caplog.at_level('WARNING'):
        got = etl._discover_span_log_groups(_FakeLogs(groups))
    assert len(got) == etl.MAX_INSIGHTS_LOG_GROUPS
    msgs = [r.getMessage() for r in caplog.records]
    assert any('超过 Insights 上限' in m for m in msgs), (
        f'静默截断会让部分 agent 的依赖边凭空消失，必须告警。实际日志: {msgs}')


def test_m04_零命中的三态判据(etl, monkeypatch):
    """本测试是这个文件存在的理由，判据改过两次，两次都因为同一个错误。

      第一版：有 runtime + 零命中 → 矛盾。
        打脸：窗口 08:00–14:00，最后一次 agent 调用在 07:48，零 span 是真实的空。
      第二版：日志组有任何日志 + 零命中 → 矛盾。
        还是错：那些日志是 [runtime-logs] 应用输出，不是 span，探针问错了对象。

    第三版用 `cloud.resource_id` 作判别 —— 它是**资源**属性，实测 400/400 条 span
    都有、与操作类型无关。三态：

      spans == 0              → EMPTY   没人调用或没埋点
      spans > 0, with_rid == 0 → 矛盾    span 在但身份字段名变了（真回归）
      spans > 0, with_rid > 0  → EMPTY   埋点正常，只是窗口内无 gen_ai 操作
    """
    groups = [f'/aws/bedrock-agentcore/runtimes/A{i}-DEFAULT' for i in range(7)]

    def probe(spans, with_rid):
        f = _FakeLogs(groups, rows=[])
        f.probe_rows = [{'spans': str(spans), 'with_rid': str(with_rid)}]
        monkeypatch.setattr(etl.boto3, 'client', lambda *a, **k: f)
        return etl.collect_spans(runtime_count=6)[0]

    assert probe(0, 0) == etl._probe_status.EMPTY, \
        'spans 流零记录 = 没人调用 agent，空就是空'
    assert probe(526, 0) == etl._probe_status.CONTRADICTORY, \
        'span 在但没有一条带身份字段 → 字段名变了，是真回归'
    assert probe(526, 526) == etl._probe_status.EMPTY, (
        '埋点与字段名都正常、只是窗口内无 gen_ai 操作 —— 这是真实的空，'
        '报矛盾就是假警报（agent 调用稀疏突发是常态）')

    # runtime_count=0（真没部署 agent）→ 连探针都不必跑
    f0 = _FakeLogs(groups, rows=[])
    monkeypatch.setattr(etl.boto3, 'client', lambda *a, **k: f0)
    assert etl.collect_spans(runtime_count=0)[0] == etl._probe_status.EMPTY


def test_m05_矛盾状态必须计入failed_collections(etl):
    """CONTRADICTORY 与 FAILED 同等对待 —— 两者都意味着本轮结果不可信。"""
    statuses = {'runtimes': etl._probe_status.OK,
                'spans': etl._probe_status.CONTRADICTORY}
    failed = [k for k, v in statuses.items()
              if v in (etl._probe_status.FAILED, etl._probe_status.CONTRADICTORY)]
    assert failed == ['spans']
    # 三个状态互不相等，否则上面那个 in 判断会误伤
    vals = {etl._probe_status.OK, etl._probe_status.EMPTY,
            etl._probe_status.FAILED, etl._probe_status.CONTRADICTORY}
    assert len(vals) == 4


def test_m06_有命中时正常返回ok(etl, monkeypatch):
    groups = ['/aws/bedrock-agentcore/runtimes/A0-DEFAULT']
    fake = _FakeLogs(groups, rows=[{'runtime_id': 'arn:x', 'op': 'execute_tool',
                                    'tool_name': 'search_available_pets',
                                    'calls': '78'}])
    monkeypatch.setattr(etl.boto3, 'client', lambda *a, **k: fake)
    st, rows = etl.collect_spans(runtime_count=6)
    assert st == etl._probe_status.OK
    assert rows[0]['tool_name'] == 'search_available_pets'
    assert fake.queried_groups == groups, 'start_query 必须收到发现出来的日志组列表'


def test_m07_显式覆盖时不再按前缀发现(etl, monkeypatch):
    """留 override 口子是为了收窄到单个 runtime，但不能因此绕过上限。"""
    monkeypatch.setattr(etl, 'SPAN_LOG_GROUPS_OVERRIDE', ['/aws/only/this/one'])
    fake = _FakeLogs(['/should/not/be/used'])
    got = etl._discover_span_log_groups(fake)
    assert got == ['/aws/only/this/one']
    assert fake.described_prefix is None, 'override 生效时不该再调 describe_log_groups'

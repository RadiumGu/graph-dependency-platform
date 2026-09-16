"""test_53_rca_page_evidence_chain.py — RCA 页证据链只许有一个定义处。

## 为什么

证据链有两个消费方：

    demo/pages/6_Root_Cause_Analysis.py   在线现查
    demo/fixtures/refresh_fixtures.py     生成离线快照

各抄一份必然漂移，而且**漂了不报错**：页面加了查询而生成器没加，
离线模式下对应小节就是空的，访客只会以为「那部分没数据」。
实测已经漂过一次 —— 两边都写了 9 条，但说明文案不同，
且 q1/q3 的方向标注都是旧的（那两个查询的遍历方向 2026-09-06 才修对，
见 tests/test_52_rca_query_direction.py）。

定义已收敛到 `demo/_common.RCA_EVIDENCE_CHAIN`。这一条钉住它不再被抄回去。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
PAGE = _DEMO / 'pages' / '6_Root_Cause_Analysis.py'
GEN = _DEMO / 'fixtures' / 'refresh_fixtures.py'
COMMON = _DEMO / '_common.py'


def _chain():
    import sys
    if str(_DEMO) not in sys.path:
        sys.path.insert(0, str(_DEMO))
    import _common as C
    return C.RCA_EVIDENCE_CHAIN, C.RCA_STAGE_TITLE


def test_m01_证据链只在common里定义():
    """页面与生成器都不得自带清单。"""
    for f in (PAGE, GEN):
        src = f.read_text(encoding='utf-8')
        # 允许 `EVIDENCE_CHAIN = C.RCA_EVIDENCE_CHAIN` 这种引用；
        # 不允许出现自己的字面量清单。
        for pat, why in (
            (r'^EVIDENCE_QUERIES\s*=\s*\[', '自带查询清单'),
            (r'^PARAM_ALIASES\s*=\s*\{', '自带参数映射'),
            (r'^EVIDENCE_CHAIN\s*=\s*\[', '自带证据链字面量'),
            (r'^STAGE_TITLE\s*=\s*\{', '自带阶段标题字面量'),
        ):
            assert not re.search(pat, src, re.M), (
                f'{f.name} 里有 {why} —— 证据链必须只在 demo/_common.py 定义。\n'
                '两处各维护一份会静默漂移：页面加查询而生成器没加，'
                '离线模式下那一节就是空的，且不报错。')


def test_m02_证据链每项结构完整并且查询都存在于目录():
    chain, stages = _chain()
    assert chain, 'RCA_EVIDENCE_CHAIN 为空'
    import sys
    sys.path.insert(0, str(Path(PROJECT_ROOT) / 'rca'))
    from neptune.query_catalog import QUERY_CATALOG

    for item in chain:
        assert len(item) == 4, f'每项应为 (查询名, 说明, 参数映射, 阶段)：{item}'
        qname, why, pmap, stage = item
        assert qname in QUERY_CATALOG, (
            f'`{qname}` 不在查询目录里 —— 页面会在采集时静默记一条 error。')
        assert isinstance(pmap, dict), f'{qname} 的参数映射应为 dict'
        assert stage in stages, (
            f'{qname} 的阶段 `{stage}` 不在 RCA_STAGE_TITLE 里，'
            f'侧栏与报告溯源表会显示空标题')
        # 必填参数必须给到
        required = set(QUERY_CATALOG[qname].get('required') or [])
        assert required <= set(pmap), (
            f'{qname} 缺必填参数 {sorted(required - set(pmap))}')


def test_m03_两个方向的查询都必须在链里且阶段标为direction():
    """这一页的立论就是「RCA 是图上两个相反的方向」。少一个方向，论点就没了。"""
    chain, _ = _chain()
    by = {q: (why, stage) for q, why, _, stage in chain}
    for q in ('q3_upstream_deps', 'q1_blast_radius'):
        assert q in by, f'证据链缺 `{q}` —— 页面的因果方向盘会缺一半'
        assert by[q][1] == 'direction', f'`{q}` 的阶段应为 direction'
    # 说明文案必须写明方向，否则观众无从分辨（上游/下游在两种读法下含义相反）
    assert '出边' in by['q3_upstream_deps'][0], (
        'q3 的说明必须写明它是出边（故障服务依赖谁）')
    assert '入边' in by['q1_blast_radius'][0], (
        'q1 的说明必须写明它是入边（谁依赖故障服务）')


def test_m04_验证类查询必须在链里():
    """依赖可信度是这一页区别于普通 RCA 面板的地方，四条都得在。"""
    chain, _ = _chain()
    names = {q for q, *_ in chain}
    for q in ('q22_edge_verification_verdicts', 'q23_verification_coverage',
              'q20_dependency_verification', 'q21_observation_source_coverage'):
        assert q in names, (
            f'证据链缺 `{q}` —— 没有它，页面就只能展示「有哪些依赖」，'
            f'展示不了「哪些依赖站得住脚」。')


def test_m05_页面不得直接把图谱行喂给st_dataframe():
    """`st.dataframe(C.df(...))` 会因一列混类型触发 pyarrow 崩溃，整页打挂。

    实测崩过两处：候选表的 `退化幅度` 列（缺失填 '—'、其余是数值），
    以及 `q12_service_dependency_tree` 的嵌套行。
    ⚠️ AppTest 抓不到 —— 它跑离线路径、喂的是干净桩数据。
    页面里统一走 `show_table()`，它会把混类型列整列转字符串。

    ⚠️ 本条第一版是**恒绿**的：排除条件写成 `'frame' not in ln`，
    本意是放过 `show_table` 内部那句 `st.dataframe(frame, ...)`，
    但 `st.dataframe` 这个名字本身就含 `frame` 子串 —— 于是它排除了所有命中行。
    改为精确匹配唯一允许的形态 `st.dataframe(frame`。
    （同类错误本会话已犯三次，见 todo/injection-found-defects #43。
    教训是：门禁必须做**反向验证** —— 注入一次回归，确认它真的会红。）
    """
    src = PAGE.read_text(encoding='utf-8')
    body = re.sub(r'"""..*?"""', '', src, flags=re.S)   # 去掉 docstring
    hits = []
    for ln in body.split('\n'):
        if 'st.dataframe(' not in ln:
            continue
        # 唯一允许：show_table() 内部那句，第一个实参就是已归一化的 frame
        if re.search(r'st\.dataframe\(\s*frame\b', ln):
            continue
        hits.append(ln.strip())
    assert not hits, (
        '这些地方直接调用了 st.dataframe，请改用 show_table()：\n  '
        + '\n  '.join(hits))

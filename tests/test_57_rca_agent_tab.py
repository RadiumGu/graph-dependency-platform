"""test_57_rca_agent_tab.py — 钉住 RCA 页「交给 Agent」这一屏的诚实性。

## 这一屏的论点

同一个问题问 AWS DevOps Agent 12 次（图谱以 MCP server 形式关联）：
**不提图谱时只有 25% 的调用会去查（2/8），点名要求就是 100%（4/4）。**
工具是好的、关联是好的、一调就准 —— 模型只是不主动去拿。

## 为什么需要门禁

这一屏是全站唯一一处**引用外部产品实测结果**的地方，所以它比别处更容易
出两种问题，而且两种都是**静默**的：

1. **数字写死。** 页面写「25%」，证据文件后来又加了采样 —— 页面开始说谎，
   而且没人会发现，因为它看起来一切正常。所以比例**必须从 fixture 算出来**。

2. **框架滑向「看它多蠢」。** 那样既不诚实（它擅长的部分做得确实好，
   而且接上图谱后表现很好），也会反过来伤本站可信度。
   所以「不是比谁聪明」这个框架必须留在页面上。

## 还有一条：判别方法必须写在页面上

如果不说清「怎么判断它到底查了没有」，那两个比例就只是自说自话。
我自己在这件事上错过两次（先数 `tool_use` 块 —— 恒为 0；
再只认 `exp-` 前缀 —— 漏掉把实验写成日期的两次），所以页面必须
公开判据，并把踩过的坑一起写上。
"""
import json
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
PAGE = _DEMO / 'pages' / '6_Root_Cause_Analysis.py'
FIXTURE = _DEMO / 'fixtures' / 'agent_unaided_answer.json'


def _tab_source() -> str:
    """只取 `with tab_agent:` 这一段，别的 Tab 不在本门禁管辖范围。"""
    src = PAGE.read_text(encoding='utf-8')
    m = re.search(r'^with tab_agent:', src, re.M)
    assert m, '没找到 with tab_agent: —— 第五个 Tab 不见了'
    nxt = re.search(r'^\S', src[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[m.start():end]


def _strip_comments(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_证据文件必须是真实调用记录():
    """带 executionId 才能被第三方核对；没有它这一屏就只是断言。"""
    assert FIXTURE.exists(), f'证据文件不存在：{FIXTURE}'
    d = json.loads(FIXTURE.read_text(encoding='utf-8'))
    runs = [r for r in (d.get('runs') or []) if 'error' not in r]
    assert len(runs) >= 8, f'样本太少（{len(runs)}），比例不足以支撑论断'
    for r in runs:
        assert r.get('execution_id'), '有样本缺 executionId —— 那就无法被核对'
        assert r.get('answer'), '有样本缺逐字回答原文'
        assert r.get('captured_at'), '有样本缺抓取时间'
    kinds = {r.get('kind') for r in runs}
    assert {'plain', 'explicit'} <= kinds, (
        f'缺少对照组。必须同时有 plain（不提图谱）与 explicit（点名要求查图谱）'
        f'两类样本，现在只有 {kinds} —— 没有对照就说明不了「取决于怎么问」。')
    assert d.get('question') and d.get('question_explicit'), '缺问题原文'
    assert d.get('detection_rule'), (
        '证据文件里没有 detection_rule。判别方法必须显式写下来，'
        '否则那两个比例只是自说自话。')


def test_m02_比例必须从证据算出不能写死():
    """写死的数字会在证据文件更新后开始说谎，而且是静默的。"""
    code = _strip_comments(_tab_source())
    assert re.search(r'len\(\s*plain\s*\)', code) and re.search(r'len\(\s*expl\s*\)', code), (
        '页面没有用 len(plain) / len(expl) 计算比例 —— 比例必须从 fixture 现算。')
    assert 'used_graph' in code, '页面没有读 used_graph，无法统计查了图谱的次数'

    # 只针对**采样比例**，不针对所有百分比。
    #
    # 判据必须对准会过时的东西：采样比例会随证据文件增补而变，
    # 而「业界所有依赖图这一列都是 100% untested」这种论断不会 ——
    # 它讲的是业界现状，不是本次采样的结果。
    # 第一版判据是「任何双位数百分比字面量」，它把后者也判成违规。
    # 本会话第六次因为「判据对准了字符串而不是现象」而误报。
    SAMPLING_WORDS = ('自发查询率', '会去查', '查了图谱', '的调用会', '采样')
    hard = [s for s in re.findall(r'"[^"]*?\d{1,3}\s*%[^"]*?"', code)
            if '{' not in s and any(w in s for w in SAMPLING_WORDS)]
    assert not hard, (
        f'采样比例被写成了字面量：{hard}\n'
        '证据文件加了采样之后这些数字就会开始说谎，而且没有任何报错。'
        '必须写成 f-string 从 len(plain) / len(expl) 现算。')


def test_m03_必须保留不是比谁聪明的框架():
    """滑向「看它多蠢」既不诚实，也会伤本站可信度。"""
    tab = _tab_source()
    assert '不是比谁聪明' in tab, (
        '页面丢了「不是比谁聪明」这个框架。'
        '任何 LLM 被问一个它没有事实依据的问题时都会给出答案 —— 包括本项目自己的引擎。'
        '这一屏证明的是「有没有可核对的数据」，不是「谁更聪明」。')
    assert '接上这张图之后表现很好' in tab or '接上图谱后表现很好' in tab, (
        '页面没有承认 agent 接上图谱后表现很好。'
        '实测里它引用真实实验 ID、退化幅度，并在证据不足时主动拒绝下结论 —— '
        '隐去这一点就是挑选证据。')


def test_m04_必须公开判别方法与踩过的坑():
    """不说清怎么判断「查了没有」，比例就没有说服力。"""
    tab = _tab_source()
    assert '退化幅度数字' in tab, '页面没有说明判据是「退化幅度数字」'
    assert 'injection_confirmed' in tab, '页面没有提 injection_confirmed 这个判据'
    assert 'tool_use' in tab, (
        '页面没有记录第一次判据错误（数 tool_use 块，该值恒为 0）。'
        '这一屏的可信度建立在把自己的错误也摆出来上。')
    assert 'exp-' in tab, '页面没有记录第二次判据错误（只认 exp- 前缀，漏掉两次）'


def test_m05_图谱一侧必须实时查不能读快照():
    """agent 那侧是记录、图谱这侧是实时 —— 这个不对称本身就是论据。"""
    code = _strip_comments(_tab_source())
    assert 'C.gquery' in code, (
        '图谱那一栏没有调用 C.gquery。它必须**实时查**：'
        'agent 的回答是抓取的记录（非确定性、单次几十秒），'
        '图谱是毫秒级且每次相同 —— 这个不对称是论点的一部分，'
        '改成读 fixture 就把它抹掉了。')
    assert 'verify_status' in code, (
        "实时查询没有取 verify_status。判定存在边的这个属性上"
        "（不是 verification_status —— 那个属性不存在）。")
    assert 'C.neptune_online' in code, '没有离线降级判断'


def test_m06_必须承认这个结果指向我们自己要修的东西():
    """25% 的自发查询率是我们的问题，不是 agent 的问题。"""
    tab = _tab_source()
    assert 'instructions' in tab, (
        '页面没有指出证据纪律写在 MCP initialize 的 instructions 字段里、'
        '而这批数据说明它没有可靠传达到模型。'
        '把 25% 说成对方的缺陷、不说成自己的待办，是不诚实的。')

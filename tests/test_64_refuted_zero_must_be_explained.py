"""test_64_refuted_zero_must_be_explained.py — 「0 条被证伪」必须给出实证解释。

## 为什么这是全站最需要门禁的一个数字

项目的核心主张是「一个能推翻自己的依赖图」。计分板上 `refuted = 0`
有三种完全不同的含义，而它们对这个主张意味着相反的东西：

    ① 图很干净        → 主张成立，只是没触发
    ② 判伪通道不可达  → **主张失去可证伪性**，等于一个永不说「不」的系统
    ③ 从未测过        → 主张未被检验，是覆盖缺口

原来页面只有一句 `st.info("当前没有被证伪的边。")`。**那句话是空洞的** ——
读者只能猜，而猜出 ② 的人会认为整个项目是自证的。这比不给数字更糟：
一个「0」加一句不解释的话，正是本项目批评别人做的事。

## 2026-09-09 逐条查过，答案是 ③

活图谱实测（115 条依赖边）：

    独立观测源 = 0 的边   62 条（54%）  ← 判伪通道对它们是可达的
    其中从未做过注入实验  57 条          ← 这才是 refuted=0 的原因
    需要的注入靶标        29 个

期间验证并**推翻了三个假设**，记录在案，因为它们都是合理但错误的猜测：

  假设一「独立证据门禁过严，判伪结构性不可达」
    错。门禁数的是**观测标记属性**（xray_* / nfm_* / calls|error_rate /
    image_ref），不是写入这条边的 source。所以 CFN 静态声明、从未被观测到的
    边 observing=0，可以判伪 —— 而且这样的边占 54%。

  假设二「靶标选择没优先测这些边」
    错。`_info_gain_key` 已按 (priority, confidence, observing_sources) 升序，
    从未验证的（conf=-1）最优先，同档内观测源少的先测。

  假设三「有些边看起来该 confirmed 却被压成了 inconclusive，说明门禁有问题」
    错。抽查两条高退化的：
      petsite→petlistadoptions 退化 24.66% → 退化全来自**吞吐塌陷**、
        成功率通道无信号；吞吐下降分不清「观测方自己失败」与「上游不再调它」，
        纯吞吐证据需 >= 60%。
      pethistory→数据库 退化 66.67% → **观测方流量不足**（基线 12 / 注入期 24，
        需 >= 20）；小样本上的 66% 是噪声。
    两条门禁都成立，而且理由都记录在边的 `verify_reason` 上。

**三个假设全错**说明这套判定比我预期的严谨。真正的问题是覆盖，不是设计。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

PAGE = Path(PROJECT_ROOT) / 'demo' / 'pages' / '1_Edge_Verification.py'


def _code_only(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_零证伪不得只给一句空话():
    """必须回答「是没测过，还是测不了」。"""
    src = PAGE.read_text(encoding='utf-8')
    assert '这个 0 需要解释' in src, (
        '「0 条被证伪」旁边没有解释。这个数字有三种相反的含义：'
        '图很干净 / 判伪通道不可达 / 从未测过。'
        '不解释的话，读者猜出「不可达」就会认为整个项目是自证的。')
    assert '判伪通道可达' in src, (
        '页面没有算「判伪通道对多少条边是可达的」。'
        '这是区分「没测过」与「测不了」的唯一办法。')


def test_m02_必须现算不得写死数字():
    """62/115 会随图谱变 —— 并发会话这两天已经改过三次边数。"""
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    assert 'C.gquery' in code, (
        '覆盖数字没有实时查询。图谱在变（本会话见过 119 → 113 → 97 → 115），'
        '写死的数字会静默变成谎话。')
    hard = [s for s in re.findall(r'"[^"]*?\d{2,3}\s*/\s*\d{2,3}[^"]*?"', code)
            if '{' not in s]
    assert not hard, f'页面里有写死的覆盖比例：{hard}'


def test_m03_判据必须与运行时的观测标记同源():
    """页面上的「零观测」定义若与判定逻辑不一致，整段分析就是错的。"""
    code = _code_only(PAGE.read_text(encoding='utf-8'))
    for marker in ('xray_call_count', 'nfm_flow_count', 'error_rate', 'image_ref'):
        assert marker in code, (
            f'页面的零观测判据缺少 `{marker}`。它必须与 '
            '`chaos/code/runner/edge_verification.py` 的 `_OBSERVER_MARKERS` '
            '同源 —— 少一个标记就会把有观测的边算成可判伪，'
            '于是待办队列里混进永远不会被判伪的边。')

    runner = (Path(PROJECT_ROOT) / 'chaos' / 'code' / 'runner'
              / 'edge_verification.py').read_text(encoding='utf-8')
    m = re.search(r'_OBSERVER_MARKERS\s*=\s*\{([\s\S]*?)\n\}', runner)
    assert m, '找不到 _OBSERVER_MARKERS'
    props = set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
    props -= {'xray', 'nfm', 'deepflow', 'k8s-image-spec'}   # 分组名不是属性名
    missing = [p for p in props if p not in code]
    assert not missing, (
        f'运行时的观测标记里有 {missing} 没被页面判据覆盖 —— '
        '上游加了新的观测源，这一段的覆盖数字会偏大。')


def test_m04_不得暗示这些边是假的():
    """「可判伪但未测」不等于「假边」。混淆两者会诬告真实依赖。"""
    src = PAGE.read_text(encoding='utf-8')
    assert '不是**说这些边是假的' in src or '不是说这些边是假的' in src, (
        '待办队列没有说清「这些边还没被检验」≠「这些边是假的」。'
        '一条边可以完全真实却零观测 —— 例如 DNS 解析派生的边：'
        '看到了解析，看不到流量。把待办当成假边清单会诬告真实依赖，'
        '而按 DoD-10 累计两次 refuted 就会删边。')


def test_m05_必须解释判伪的第三个前提():
    """「有独立观测源的边永不判 refuted」是这套设计最反直觉的一条。"""
    src = PAGE.read_text(encoding='utf-8')
    assert 'soft dependency' in src, (
        '页面没有解释为什么「被观测到过的边」不能判 refuted。'
        '理由是 soft dependency 与「边不存在」在干预数据上**完全同形** —— '
        '不说这一条，读者会觉得这道门禁是在护着数据。')

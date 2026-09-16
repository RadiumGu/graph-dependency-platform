"""test_58_metric_delta_not_annotation.py — 禁止把附注塞进 st.metric 的 delta 位。

## 现象

`st.metric(label, value, delta)` 的第三个参数渲染成**带箭头的涨跌**。
`delta_color="off"` 只去掉颜色，**箭头照样在**。于是：

    st.metric("AZ 分布", 1, "个可用区", delta_color="off")   → 「1　↑ 个可用区」
    st.metric("untested", 86, "76.1%", delta_color="off")   → 「86　↑ 76.1%」
    st.metric("不提图谱时会去查", "2/8", "25%", ...)          → 「2/8　↑ 25%」

三个都在制造一个**不存在的趋势**。第二个尤其糟：那是本站的核心记分牌
（每条依赖边的验证判定分布），「↑ 76.1%」会被读成「未验证边涨了 76%」。

## 为什么这值得一条门禁

这个站的整个主张是「摆出来的数字要能被核对」。在自己的记分牌上放一个
凭空捏造的箭头，比少显示一个占比糟得多 —— 而且它**看起来完全正常**，
不报错、不告警，正是本项目反复栽跟头的那类静默缺陷。

2026-09-07 部署后截图才发现，当时全站有 10 处。

## 正确写法

占比、单位、说明一律进 `label` 或 `help=`：

    st.metric("AZ 分布", 1, help="该服务的 Pod 分布在几个可用区。")
    st.metric("⬜ untested　76.1%", 86, help="113 条依赖边里 86 条未验证。")

delta 位**只放真正的变化量**（本期 vs 上期）。本站目前没有这种指标，
所以正确的状态是：**demo 下不出现任何 delta_color="off"**。
它的存在本身就说明有人在用 delta 位放附注 —— 真的涨跌不需要关掉颜色。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'


def _py_files() -> list:
    out = [_DEMO / '_common.py', _DEMO / 'app.py']
    out += sorted((_DEMO / 'pages').glob('*.py'))
    return [p for p in out if p.exists()]


def _code_only(text: str) -> str:
    """剥 docstring 与注释 —— 否则会匹配到本文件式的解释性文字。

    本会话为「判据匹配到自己写的说明文字」付过多次学费。
    """
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m01_不得用delta位放附注():
    """`delta_color="off"` 的存在即证据：真的涨跌不需要关掉颜色。"""
    bad = {}
    for p in _py_files():
        code = _code_only(p.read_text(encoding='utf-8'))
        hits = [i + 1 for i, ln in enumerate(code.split('\n'))
                if 'delta_color' in ln and 'off' in ln]
        if hits:
            bad[p.name] = hits
    assert not bad, (
        f'这些地方把附注放在了 st.metric 的 delta 位：{bad}\n\n'
        'delta 渲染成带箭头的涨跌，`delta_color="off"` 只去掉颜色、箭头照样在，'
        '于是「86　↑ 76.1%」会被读成「涨了 76%」——'
        '在一个主张「数字要能被核对」的站点上凭空造趋势。\n'
        '占比与说明请进 label 或 help=；delta 位只放真正的变化量。')


def test_m02_核心记分牌的占比必须在label或help里():
    """`status_chips` 是全站共用的验证判定记分牌，最不该出错的地方。"""
    src = (_DEMO / '_common.py').read_text(encoding='utf-8')
    m = re.search(r'def status_chips\([\s\S]*?(?=\ndef )', src)
    assert m, '没找到 status_chips'
    body = _code_only(m.group(0))
    assert 'delta_color' not in body, (
        'status_chips 仍在用 delta 位。这一行是本站核心记分牌，'
        '「untested 86 ↑ 76.1%」这种渲染直接损害可信度。')
    assert 'help=' in body, (
        'status_chips 没有 help 说明。占比与含义要能被读到，'
        '只是不能放在 delta 位。')


def test_m03_metric第三个位置参数不得是纯说明文本():
    """就算不写 delta_color，第三个位置参数仍是 delta，一样会出箭头。

    而且不写 delta_color 更糟：它会**带绿/红配色**渲染，
    「预置查询 24　↑ 确定性 Cypher」看着像一个真的增长。

    判据只看**字符串字面量**作为第三个位置参数的情形 —— 那必然是附注，
    因为真正的变化量是算出来的（变量或表达式），不会是写死的短语。

    ⚠️ 第一版正则把 value 写成 `[^,()]+`，于是
    `f"{gstats.get('node_total', 0):,}"` 这种带括号的 value 直接不匹配，
    漏掉了首页 6 处里的 4 处。value 必须允许括号与逗号 —— 用括号配平来断。
    """
    bad = {}
    # .metric( 之后逐字符扫，按顶层逗号切参数，只看第 3 个
    for p in _py_files():
        code = _code_only(p.read_text(encoding='utf-8'))
        hits = []
        for mm in re.finditer(r'\.metric\(', code):
            i = mm.end()
            depth, args, cur, in_s, q = 1, [], '', False, ''
            while i < len(code) and depth:
                c = code[i]
                if in_s:
                    if c == q and code[i - 1] != '\\':
                        in_s = False
                elif c in '"\'':
                    in_s, q = True, c
                elif c in '([{':
                    depth += 1
                elif c in ')]}':
                    depth -= 1
                    if not depth:
                        break
                elif c == ',' and depth == 1:
                    args.append(cur.strip()); cur = ''; i += 1; continue
                cur += c; i += 1
            args.append(cur.strip())
            if len(args) >= 3:
                third = args[2]
                if re.match(r'^f?["\']', third) and '=' not in third.split('"')[0]:
                    hits.append(third[:44])
        if hits:
            bad[p.name] = hits
    assert not bad, (
        f'这些 st.metric 把字符串字面量当成了 delta（第三个位置参数）：{bad}\n'
        '真正的变化量是算出来的，不会是写死的短语 —— 所以这些都是附注，'
        '会渲染成带箭头（且带配色）的涨跌。请改用 help= 或并进 label。')

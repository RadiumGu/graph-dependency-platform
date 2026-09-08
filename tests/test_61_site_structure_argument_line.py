"""test_61_site_structure_argument_line.py — 站点必须读得出一条论证线。

## 这条门禁挡的是什么

原来侧栏是十条**扁平清单**。十个页面各自都能用，但连起来读不出一条线 ——
访客看到的是「这个项目有十个功能」，而不是「这个项目在论证一件事」。

论证线是四段，顺序不能换：

    ① 这张图是什么      先让人看见对象，否则后面说什么都没落点
    ② 这张图是真的吗    **核心，市面产品没有这一段**
    ③ 它能帮你做什么    有了可信的图，下游才谈得上
    ④ 你可以自己问它    把验证权交给访客，而不是让他信我们

## 还挡「页面存在但没人能找到」

一个 `demo/pages/*.py` 文件不在导航里就等于不存在 —— Streamlit 的多页机制会
把它列进自动侧栏，但本项目用的是自定义导航，漏掉一页不会报任何错。
这与本会话反复出现的那类静默缺陷同型：**东西在那儿，只是没人看得见。**
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / 'demo'
COMMON = _DEMO / '_common.py'
APP = _DEMO / 'app.py'


def _nav_groups() -> list:
    """从 _common.py 里取 NAV_GROUPS，不导入模块（它会拉起 streamlit）。"""
    src = COMMON.read_text(encoding='utf-8')
    m = re.search(r'NAV_GROUPS\s*=\s*\[', src)
    assert m, '_common.py 里没有 NAV_GROUPS'
    i, depth = m.end() - 1, 0
    while i < len(src):
        if src[i] == '[':
            depth += 1
        elif src[i] == ']':
            depth -= 1
            if depth == 0:
                break
        i += 1
    import ast
    return ast.literal_eval(src[m.end() - 1:i + 1])


def test_m01_导航必须分成四段论证线():
    groups = _nav_groups()
    titles = [g for g, _ in groups]
    assert len(groups) == 4, (
        f'论证线应该是四段，现在有 {len(groups)} 段：{titles}\n'
        '① 这张图是什么 → ② 这张图是真的吗 → ③ 它能帮你做什么 → ④ 你可以自己问它')
    for i, mark in enumerate(('①', '②', '③', '④')):
        assert titles[i].startswith(mark), (
            f'第 {i + 1} 段标题不是以 {mark} 开头：{titles[i]}\n'
            '顺序不能换 —— 先让人看见对象，再谈它是不是真的。')
    assert '真的吗' in titles[1], (
        f'第 ② 段应该是「这张图是真的吗」，现在是：{titles[1]}\n'
        '这是市面产品没有的那一段，也是整个项目的论点，位置不能动。')
    assert '核心' in titles[1], (
        '第 ② 段没有标出「核心」。访客只看一页时应该看这一段。')


def test_m02_每个页面文件都必须在导航里():
    """页面不在导航里等于不存在 —— 而且不会报任何错。"""
    files = {f'pages/{p.name}' for p in (_DEMO / 'pages').glob('*.py')
             if not p.name.startswith('_')}
    listed = {p for _, entries in _nav_groups() for p, _ in entries}
    missing = files - listed
    assert not missing, (
        f'这些页面文件不在 NAV_GROUPS 里，访客找不到它们：{sorted(missing)}\n'
        '本项目用自定义导航，漏掉一页不会报错 —— 它就是静静地消失。')
    ghosts = {p for p in listed if p != 'app.py'} - files
    assert not ghosts, (
        f'导航里指向了不存在的页面：{sorted(ghosts)}\n'
        'st.page_link 指向缺失文件会在运行时抛异常。')


def test_m03_首页必须把论证线摆出来():
    """侧栏分组只是导航；首页得说清**为什么**是这四段。"""
    src = APP.read_text(encoding='utf-8')
    assert '怎么读这个站' in src, (
        '首页没有「怎么读这个站」这一节。原来读完首页不知道下一步去哪 —— '
        '十个页面在侧栏是一条扁平清单，看起来像十个并列的功能。')
    for mark in ('①', '②', '③', '④'):
        assert mark in src, f'首页论证线缺少第 {mark} 段'
    assert '核心' in src, '首页没有标出哪一段是核心'


def test_m04_首页主张不得被埋掉():
    """「我看到的是真的吗」必须在第一屏，不能被功能目录挤下去。"""
    src = APP.read_text(encoding='utf-8')
    claim = src.find('我看到的是真的吗')
    assert claim > 0, '首页丢了核心主张「我看到的是真的吗」'
    roadmap = src.find('怎么读这个站')
    assert claim < roadmap, (
        '主张排在论证线路线图之后。先立主张，再给路线图 —— '
        '否则访客不知道这些页面是为了论证什么。')
    scoreboard = src.find('依赖边验证计分板')
    assert claim < scoreboard < roadmap, (
        '顺序应该是：主张 → 计分板（第一手证据）→ 怎么读这个站（路线图）。'
        f'实际位置：主张 {claim} / 计分板 {scoreboard} / 路线图 {roadmap}')


def test_m05_NAV扁平清单必须与分组一致():
    """有些地方只需要「所有页面」，那份清单不能与分组脱钩。"""
    src = COMMON.read_text(encoding='utf-8')
    assert re.search(
        r'NAV\s*=\s*\[\s*entry\s+for\s+_,\s*entries\s+in\s+NAV_GROUPS', src), (
        'NAV 不是从 NAV_GROUPS 推导出来的。两份手写清单必然漂移 —— '
        '本会话的教训是：同一事实只能有一个来源。')

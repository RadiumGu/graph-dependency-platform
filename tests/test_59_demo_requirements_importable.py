"""test_59_demo_requirements_importable.py — 声明的依赖必须真的能导入。

## 这条门禁挡的是什么

2026-09-07：`demo/requirements.txt` 里写着 `st-link-analysis>=0.4.0`，
但它**从来没被装上**。后果不是报错，而是 `9_Interactive_Explorer`
走进 `except ImportError` 分支、显示一个错误框、然后 `st.stop()` ——
整页零控件、零表格。

这是最难发现的一类缺陷：

  · 页面**能打开**，HTTP 200
  · **不抛异常**，日志里没有 traceback
  · 错误框写得还挺得体（「缺少 st-link-analysis，安装：pip install ...」）
  · 所以它看起来就像「这页本来就长这样」

真正的缺陷不是「少装一个包」——那只是症状。缺陷是**没有任何东西检查
已声明的依赖是否装上了**。装漏一个包和写错一行代码同样能让功能消失，
但前者没有任何自动化能抓到。

## 顺带挡开区间

`>=` 会让某天的新版本静默改掉 API。`st-link-analysis` 0.4.0 已经在弃用
`labeled` 参数了 —— 下一版删掉它，页面就会在运行时炸，而 CI 全绿。
所以 demo 的依赖一律钉死（`==`）。
"""
import importlib
import re
from pathlib import Path

from paths import PROJECT_ROOT

REQ = Path(PROJECT_ROOT) / 'demo' / 'requirements.txt'

#: 包名 → 导入名（不一致的才需要列在这里）
_IMPORT_NAME = {
    'st-link-analysis': 'st_link_analysis',
    'pyyaml': 'yaml',
    'pillow': 'PIL',
    'beautifulsoup4': 'bs4',
    'python-dateutil': 'dateutil',
}


def _requirements() -> list:
    """返回 [(原始行, 包名, 版本约束符)]。"""
    out = []
    for raw in REQ.read_text(encoding='utf-8').split('\n'):
        ln = raw.split('#', 1)[0].strip()
        if not ln or ln.startswith('-'):
            continue
        m = re.match(r'^([A-Za-z0-9._-]+)\s*(==|>=|<=|~=|>|<)?', ln)
        if m:
            out.append((ln, m.group(1).lower(), m.group(2) or ''))
    return out


def test_m01_requirements文件存在且非空():
    assert REQ.exists(), f'缺少 {REQ}'
    reqs = _requirements()
    assert len(reqs) >= 5, f'依赖太少（{len(reqs)}），文件可能被截断'


def test_m02_每个声明的依赖都必须能导入():
    """装漏一个包和写错一行代码同样能让功能消失，但前者没人抓。"""
    missing = []
    for line, pkg, _ in _requirements():
        mod = _IMPORT_NAME.get(pkg, pkg.replace('-', '_'))
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            missing.append(f'{line}（import {mod} → {type(exc).__name__}）')
    assert not missing, (
        '这些依赖声明了但导入不了：\n  ' + '\n  '.join(missing) + '\n\n'
        '后果不是报错，而是对应页面走进 except ImportError 分支、'
        '显示一个得体的错误框、然后 st.stop() —— 整页零控件，'
        'HTTP 200，日志无 traceback，看起来就像「这页本来就长这样」。\n'
        '装上它们，或者从 requirements.txt 里删掉不再需要的那些。')


#: 必须钉死（`==`）的包 —— 只列**有实证理由**的。
#:
#: 判据不是「钉死更安全」，而是「这个包的 API 已经证明不稳」：
#:
#:   st-link-analysis  0.4.0 正在弃用 EdgeStyle 的 `labeled`；本页直接构造
#:                     EdgeStyle / NodeStyle 并依赖 `caption` 参数。
#:                     下一版删掉参数 → 运行时炸，静态检查全绿。
#:   pyvis             build_html 的 options / 坐标 API 在 0.3.x 内变过；
#:                     3_Graph_Explorer 自己算坐标、绕开 hierarchical，
#:                     强耦合到具体行为。
#:   streamlit         本仓库用到 width="stretch"、st.tabs、AppTest 等
#:                     版本敏感 API；线上与本地必须同版本才谈得上复现
#:                     （这也是 2026-09-06 把线上升到 1.62.0 的原因）。
#:
#: **刻意不钉 boto3**：它必须保持新，否则拿不到新服务。
#: 本会话用的 `aws devops-agent` 就需要较新的 botocore —— 钉死它是
#: 主动制造故障，不是防故障。requests / pandas / pyyaml / pydantic /
#: networkx 同理：向后兼容良好，给下限就够。
_MUST_PIN = {'st-link-analysis', 'pyvis', 'streamlit'}


def test_m03_API不稳的依赖必须钉死版本():
    """开区间会让新版本静默改掉 API，而所有静态检查都是绿的。

    只管 `_MUST_PIN` 里那几个 —— 全部钉死不是更安全，对 boto3 是更危险。
    """
    loose = [line for line, pkg, op in _requirements()
             if pkg in _MUST_PIN and op != '==']
    assert not loose, (
        '这些 API 已证明不稳的依赖没有钉死版本：\n  ' + '\n  '.join(loose) + '\n\n'
        '举例：st-link-analysis 0.4.0 已经在弃用 EdgeStyle 的 `labeled` 参数，'
        '下一版删掉它，页面会在**运行时**炸，而 CI 全绿。\n'
        '这几个包必须用 `==`。（boto3 等刻意不钉，理由见 _MUST_PIN 的注释。）')


def test_m04_其余依赖至少要有版本下限():
    """不钉死也不能完全不写 —— 裸包名会装到任意版本。"""
    naked = [line for line, pkg, op in _requirements()
             if pkg not in _MUST_PIN and not op]
    assert not naked, (
        f'这些依赖既没钉死也没下限：{naked}\n'
        '至少要给 `>=`，否则装到哪个版本完全取决于运行环境。')


def test_m05_交互探索页的关键符号必须存在():
    """光能 import 不够 —— 页面用的符号得都在，且签名没变。

    ## ⚠️ 2026-09-24：这条测试原来守的是一个已被删除的依赖

    原文 import `st_link_analysis`，检查 `EdgeStyle`/`NodeStyle` 的 `caption`
    参数,docstring 写着「9_Interactive_Explorer.py 直接 import 这四个符号」。

    那句话早就不成立了:`demo/requirements.txt` 记着 2026-09-13 把
    pyvis / networkx / st-link-analysis **三个都删了**,两个图谱页改用
    `demo/components/graph_svg`(dagre 布局 + 自绘 SVG)。
    现在 `9_Interactive_Explorer.py:41` 是 `from components import graph_svg`。

    它一直没被发现,是因为两层遮蔽叠在一起:
      ① 开发机上还残留着卸载前装的 st_link_analysis,所以本地一直绿;
      ② CI 里所有测试都在 setup 阶段因缺 Neptune 而 ERROR(见 conftest 的
         cleanup_test_data 说明),这条从来没真跑过。

    **教训**:一条 import 一个「项目已决定不用」的包的测试,本身就是可疑的 ——
    它守的契约已经不存在了,而它的绿色会被当成「这一页没问题」。
    """
    import inspect

    # 守真正的契约：页面调的是 graph_svg.render(nodes, edges, ..., key=...)。
    # ⚠️ 用文件里已有的 REQ 推出 demo 目录，**不要**另造一个名字：
    # 第一版我写了 `DEMO` 和 `sys.path`，而这个文件既没定义 DEMO 也没
    # import sys —— 又一次「按想象中的实现写代码」（本会话第五次）。
    import sys  # noqa: PLC0415

    demo_dir = REQ.parent
    sys.path.insert(0, str(demo_dir))
    try:
        mod = importlib.import_module("components.graph_svg")
    finally:
        sys.path.remove(str(demo_dir))

    assert hasattr(mod, "render"), (
        "components/graph_svg 少了 `render` —— "
        "3_Graph_Explorer.py 与 9_Interactive_Explorer.py 都调它。"
    )
    sig = inspect.signature(mod.render)
    # 这五个是两个页面实际传的（9_Interactive_Explorer.py:504 传了
    # height/anchor/key，3_Graph_Explorer.py 还传 positions/bands）。
    for param in ("nodes", "edges", "height", "anchor", "key"):
        assert param in sig.parameters, (
            f"graph_svg.render 少了 `{param}` 参数 —— 两个图谱页在传它。"
        )

    # 已删除的三个包不许悄悄回来：它们各自都有被删的具体理由
    # （st-link-analysis 在缩放低于 0.625 时节点标签整体消失；
    #   pyvis 是单向的，选中的节点拿不回 Python），
    # 见 demo/requirements.txt 的说明。
    reqs = REQ.read_text(encoding="utf-8")
    for dead in ("st-link-analysis", "pyvis", "networkx"):
        installed = [
            ln for ln in reqs.splitlines()
            if ln.strip().lower().startswith(dead)
        ]
        assert not installed, (
            f"{dead} 又出现在 demo/requirements.txt 的依赖行里。"
            "它是 2026-09-13 刻意删掉的，要装回来请先读那里记的理由。"
        )

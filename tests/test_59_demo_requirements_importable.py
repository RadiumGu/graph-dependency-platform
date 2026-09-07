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
    """光能 import 不够 —— 页面用的四个符号得都在，且签名没变。

    这一页真正依赖的是 `EdgeStyle(caption=...)` 与 `NodeStyle(caption=...)`。
    0.4.0 正在弃用 `labeled`，所以 `caption` 是必须存在的那个。
    """
    import inspect

    mod = importlib.import_module('st_link_analysis')
    for name in ('EdgeStyle', 'Event', 'NodeStyle', 'st_link_analysis'):
        assert hasattr(mod, name), (
            f'st_link_analysis 少了 `{name}` —— '
            '9_Interactive_Explorer.py 直接 import 这四个符号。')
    for cls_name in ('EdgeStyle', 'NodeStyle'):
        sig = inspect.signature(getattr(mod, cls_name).__init__)
        assert 'caption' in sig.parameters, (
            f'{cls_name} 没有 `caption` 参数。本页用 caption 显示关系名/节点名，'
            '不用已被弃用的 `labeled`。上游若改了参数名，这一页要跟着改。')

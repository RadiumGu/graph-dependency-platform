"""
test_136_fromasset_must_exclude_pycache.py

守「每一个 `Code.fromAsset` / `LayerVersion` 资产都必须排除 __pycache__」。

## 为什么是门禁而不是注释

`lambda.Code.fromAsset()` 打包目录的**当前内容**，并且**默认不排除**
`__pycache__` / `*.pyc`。而本仓的 pytest 会 import 这些目录里的模块，
于是跑一次测试就在资产目录里留下一堆 .pyc。

PR #62 给 Layer 加过 exclude，**但漏了 4 个函数资产**
（etl_deepflow / etl_aws / etl_cfn / etl_trigger）。
2026-10-07 做三方一致性核对时才发现，当时的残留量是
etl_deepflow 97 个 .pyc、etl_aws 107 个、etl_cfn 113 个。

实测后果三层，第一层最隐蔽：

1. **跑过 pytest 之后 `cdk diff` 永远不干净** —— 资产哈希随 .pyc 变动。
   这会训练人忽略 diff，而忽略 diff 正是配置漂移长期不被发现的起点。
   （本仓已经为此付过代价：3 个 ETL 曾 4 个在 v19 而 appsignals 在 v15，
     见 docs/lessons/tech-debt-etl-lambdas-outside-cfn.md。）
2. **.pyc 被发到生产**。python3.11 编出来的 .pyc 进 python3.12 的 Lambda
   是 magic number 不匹配的死重，纯占包体积。
3. **让「包条目数部署前后不变」这个防空壳判据失效** ——
   那是 docs/lessons/cdk-fromasset-packages-ungitted-deps.md 给出的
   最直接的判据，而哈希随「谁最后跑过测试」变动会把它淹掉。

所以这条不变量必须是**结构性**的（门禁）而不是**记忆性**的（注释）。
这也是 PR #62 自己的标题说的「治本而非靠『记得清理』」—— 那次只治了一半。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STACK_DIR = ROOT / 'infra' / 'lib'


def _stack_files() -> list[pathlib.Path]:
    return sorted(STACK_DIR.glob('*.ts'))


def test_t136_01_stack_files_exist():
    assert _stack_files(), f"{STACK_DIR} 下没有 .ts 栈文件"


def test_t136_02_every_fromasset_excludes_pycache():
    """每个 fromAsset 调用都必须带 __pycache__ 的 exclude。

    判据：从 `fromAsset(` 起向后取一段，必须出现 `__pycache__`。
    取 700 字符 —— 足以覆盖一个 fromAsset 的 options 对象连同注释，
    又不会误把下一个 fromAsset 的 exclude 算进来（实测各调用点间距远大于此）。
    """
    problems = []
    for path in _stack_files():
        src = path.read_text(encoding='utf-8')
        for m in re.finditer(r'\bfromAsset\s*\(', src):
            seg = src[m.start():m.start() + 700]
            if '__pycache__' in seg:
                continue
            line = src[:m.start()].count('\n') + 1
            # 把那一行的内容带出来，便于定位是哪个资产
            ctx = src.splitlines()[line - 1].strip()[:100]
            problems.append(f"  {path.name}:{line}  {ctx}")
    assert not problems, (
        "有 fromAsset 没有排除 __pycache__ / *.pyc。\n"
        + "\n".join(problems)
        + "\n\n`fromAsset` 默认不排除它们，而 pytest 会在这些目录里生成 .pyc。"
          "\n后果：cdk diff 永远不干净（训练人忽略 diff）、.pyc 被发到生产、"
          "\n并且让「包条目数部署前后不变」这个防空壳判据失效。"
          "\n加上 `exclude: ['**/__pycache__/**', '**/*.pyc']`。"
    )


def test_t136_03_exclude_covers_both_patterns():
    """既要排目录也要排文件 —— 只写一个会漏。

    `**/__pycache__/**` 排的是 __pycache__ 目录下的内容；
    `**/*.pyc` 兜住散落在别处的 .pyc（例如 Python 2 风格的同目录 .pyc，
    或被工具生成在包根下的）。两者不互相覆盖。
    """
    problems = []
    for path in _stack_files():
        src = path.read_text(encoding='utf-8')
        for m in re.finditer(r'\bfromAsset\s*\(', src):
            seg = src[m.start():m.start() + 700]
            if '__pycache__' not in seg:
                continue   # 由 test_02 负责报
            if '*.pyc' not in seg:
                line = src[:m.start()].count('\n') + 1
                problems.append(f"  {path.name}:{line} 只排了 __pycache__，没排 *.pyc")
    assert not problems, "\n".join(problems)


def test_t136_04_asset_dirs_are_gitignored_for_pycache():
    """.gitignore 必须把 __pycache__ 挡住，否则它们会被提交进仓库。

    与 fromAsset 的 exclude 是两道独立的防线：
    exclude 管「不要打进部署包」，gitignore 管「不要进版本库」。
    """
    gi = ROOT / '.gitignore'
    if not gi.is_file():
        pytest.skip('没有 .gitignore')
    txt = gi.read_text(encoding='utf-8')
    assert '__pycache__' in txt, ".gitignore 没有挡 __pycache__"


def test_t136_05_no_pyc_tracked_in_git():
    """仓库里不得有被跟踪的 .pyc。

    被跟踪的 .pyc 比未跟踪的更糟：`git clean -fdx` 清不掉它，
    于是那条「部署后清理工作树」的流程对它无效。
    """
    import subprocess
    r = subprocess.run(['git', 'ls-files', '*.pyc', '**/*.pyc'],
                       cwd=ROOT, capture_output=True, text=True)
    tracked = [l for l in r.stdout.splitlines() if l.strip()]
    assert not tracked, (
        "仓库里有被跟踪的 .pyc：\n  " + "\n  ".join(tracked[:20])
        + "\n被跟踪的 .pyc 连 `git clean -fdx` 都清不掉。"
    )

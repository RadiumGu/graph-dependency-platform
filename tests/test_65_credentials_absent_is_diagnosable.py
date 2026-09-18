"""tests/test_65_credentials_absent_is_diagnosable.py — 凭据缺失必须给出可诊断的错误

## 这组测试在补什么

2026-09-09 实测：在**没有 AWS 凭据**的环境跑 `pytest -m "not neptune"`，
**767 个用例全部以同一条错误失败**：

    AttributeError: 'NoneType' object has no attribute 'get_frozen_credentials'

根因只有一个 —— `boto3.Session().get_credentials()` 在解析不到凭据时
**返回 None 而不抛异常**，而代码直接对它调 `.get_frozen_credentials()`。
全仓库有 **9 处**同样未加保护的写法。

后果不是「跑不过」，而是**报错完全看不出是凭据问题**：
767 条 AttributeError 让人以为是 767 个各自的故障。
这也是「无法在 CI 里跑离线门禁子集」的直接障碍 ——
一个连失败原因都读不懂的套件，没人会去接 CI。

## 为什么这条门禁值得长期留着

`get_credentials()` 返回 None 是 botocore 的既定行为，不会变；
而「加一个 boto3 调用」是这个仓库里最常见的改动之一（9 个副本足以说明）。
没有门禁的话，下一个新增的签名路径会重新引入同一个坑。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)

#: 会对 SigV4 请求签名、因而需要冻结凭据的模块。
#
# 三对是 Lambda 打包用的副本（有 test_52::m05 之类的门禁强制内容一致），
# 逐一列出而不是 glob：漏掉一个就等于放弃对它的检查，而 glob 会在
# 目录结构变化时静默少扫。
CREDENTIAL_CALL_SITES = [
    'chaos/code/runner/neptune_client.py',
    'dr-plan-generator/graph/neptune_client.py',
    'infra/lambda/etl_aws/neptune_client_base.py',
    'infra/lambda/etl_aws/collectors/eks.py',
    'infra/lambda/rca_window_flush/neptune/neptune_client.py',
    'infra/lambda/rca_window_flush/collectors/eks_auth.py',
    'infra/lambda/shared/python/neptune_client_base.py',
    'rca/neptune/neptune_client.py',
    'rca/collectors/eks_auth.py',
]

#: 未加保护的写法：直接把 get_credentials() 的返回值当对象用。
#
# ⚠️ **必须用 AST 而不是正则**。这些文件的 docstring 里原文引用了旧写法
# （「原先每次调用都 `boto3.Session().get_credentials().get_frozen_credentials()`」），
# 正则会把那段**散文**当成代码命中 —— 我第一版就这么误报了 4 个文件。
# 注释里讲历史是好事，门禁不该因此逼人删注释。


def _unguarded_derefs(src: str) -> list:
    """用 AST 找「对 get_credentials() 的返回值直接取属性」的位置。

    匹配形状：`<any>.get_credentials().get_frozen_credentials()`
    即 Attribute(value=Call(func=Attribute(attr='get_credentials')))。
    """
    import ast

    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:                                  # pragma: no cover
        return [f'语法错误无法解析: {exc}']
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        inner = node.value
        if (isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == 'get_credentials'):
            hits.append(f'L{node.lineno}: .{node.attr} on get_credentials()')
    return hits


@pytest.mark.parametrize('rel', CREDENTIAL_CALL_SITES)
def test_t65_01_no_unguarded_credential_deref(rel):
    """任何签名路径都不得直接对 `get_credentials()` 的返回值取属性。

    `get_credentials()` **解析不到凭据时返回 None**（不抛异常），
    直连 `.get_frozen_credentials()` 会产出
    `AttributeError: 'NoneType' object has no attribute ...` ——
    一条完全看不出是凭据问题的报错。
    """
    p = ROOT / rel
    assert p.exists(), f'{rel} 不存在 —— 若文件已移动，请更新本清单而不是删掉这条检查'
    hits = _unguarded_derefs(p.read_text(encoding='utf-8'))
    assert not hits, (
        f'{rel} 里有 {len(hits)} 处直接对 get_credentials() 取属性:\n  '
        + '\n  '.join(hits)
        + '\n正确写法：\n'
        '    creds = session.get_credentials()\n'
        '    if creds is None:\n'
        '        raise RuntimeError("AWS 凭据未解析到…")\n'
        '    frozen = creds.get_frozen_credentials()\n'
        '理由：解析不到凭据时返回 None，直连取属性会把「凭据问题」\n'
        '伪装成一条 NoneType AttributeError。实测代价：767 个用例同错。'
    )


@pytest.mark.parametrize('rel', CREDENTIAL_CALL_SITES)
def test_t65_02_credential_error_is_actionable(rel):
    """凭据缺失的报错必须说清「是什么问题」和「怎么办」。

    只 `raise RuntimeError("no credentials")` 也能通过 t65_01，
    但对读日志的人几乎没有帮助。本条要求错误文本里同时出现
    「凭据」这个词和至少一条可执行的下一步。
    """
    src = (ROOT / rel).read_text(encoding='utf-8')
    assert '凭据未解析到' in src, (
        f'{rel} 缺少可诊断的凭据错误文本。'
        f'请复用其余 8 处相同的措辞，保持日志可 grep。'
    )
    # 至少给出一条可执行动作
    assert ('aws sso login' in src or 'AWS_PROFILE' in src), (
        f'{rel} 的凭据错误没有给出下一步动作（如 `aws sso login` / 设置 AWS_PROFILE）。'
        f'一条只说「失败了」的错误会让人去翻代码，而不是去修配置。'
    )


def test_t65_03_guard_actually_fires(monkeypatch):
    """行为验证：凭据解析返回 None 时必须抛出可诊断的 RuntimeError。

    只做静态文本检查不够 —— 保护写在了那里、但被写在了不会执行的分支上，
    静态检查一样通过。这条把它真正跑一遍。

    用 monkeypatch 把 `get_credentials()` 打成返回 None，
    **不动任何真实凭据、不读环境变量**。
    """
    import sys

    layer = str(ROOT / 'infra' / 'lambda' / 'shared' / 'python')
    if layer not in sys.path:
        sys.path.insert(0, layer)

    # conftest 会把 neptune_client_base 桩进 sys.modules（生产里它来自 Lambda 层），
    # 所以这里直接按路径加载真实实现，避开那个桩。
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        '_real_ncb', ROOT / 'infra' / 'lambda' / 'shared' / 'python' / 'neptune_client_base.py')
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                    # pragma: no cover
        pytest.skip(f'无法独立加载 neptune_client_base: {exc}')

    class _NoCredSession:
        def get_credentials(self):
            return None                                        # botocore 的真实行为

    monkeypatch.setattr(mod, '_boto_session', _NoCredSession(), raising=False)

    # 函数名按模块实际情况解析：shared 层里叫 `_get_creds`，
    # rca / dr-plan-generator 那几份叫 `_get_frozen_creds`。
    # 硬编码一个名字会在另一侧改名后静默 skip 掉这条行为验证。
    fn = getattr(mod, '_get_creds', None) or getattr(mod, '_get_frozen_creds', None)
    assert fn is not None, (
        'neptune_client_base 里找不到取冻结凭据的函数（_get_creds / _get_frozen_creds）。'
        '若已改名请更新本测试 —— 不要删掉它。'
    )

    with pytest.raises(RuntimeError) as ei:
        fn()

    msg = str(ei.value)
    assert '凭据未解析到' in msg, f'错误文本不可诊断: {msg!r}'
    assert 'aws sso login' in msg or 'AWS_PROFILE' in msg, (
        f'错误没有给出下一步动作: {msg!r}')

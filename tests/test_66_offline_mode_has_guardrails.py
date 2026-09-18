"""tests/test_66_offline_mode_has_guardrails.py — 离线模式必须带护栏

## 离线模式在解什么问题

2026-09-09 实测：无 AWS 凭据时跑 `pytest -m "not neptune"` → **767 个失败**。
所以本仓库的整套门禁（g18 / t59_06 / G2 / test_52::m04 …）
**只在有人记得手动跑 pytest 时才生效** —— `.github/workflows` 只在 main 上
触发、且不跑测试套件。**门禁本身没有自动执行，就等于门禁只是声明。**

`GDP_OFFLINE=1` 把「确认是云访问不可达」的失败转成 skip，让门禁子集能在
无云环境跑起来。

## 这组测试为什么存在

**离线模式本身是危险的**：全部 skip 的运行看起来是绿的。
如果只有 skip 而没有「通过数下限」校验，它就把「测试没跑」伪装成
「测试通过了」—— 那比没有 CI 更糟，因为它给出虚假的安全感。

所以本文件钉的不是「skip 能工作」，而是**护栏不能被摘掉**：
  - 签名必须收紧，不能把业务断言失败也当成云问题
  - 必须有通过数下限的机制，且不满足时要让运行以失败退出
  - 必须把跳过数打印出来（不打印 = 面板上与全绿无法区分）
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from paths import PROJECT_ROOT

CONFTEST = pathlib.Path(PROJECT_ROOT) / 'tests' / 'conftest.py'


@pytest.fixture(scope='module')
def conftest_src() -> str:
    return CONFTEST.read_text(encoding='utf-8')


def test_t66_01_offline_mode_is_opt_in(conftest_src):
    """离线模式必须显式开启，绝不能是默认行为。

    默认开启会让本地开发时的真实云故障被静默 skip ——
    那是最坏情况：问题存在，但没人看得见。
    """
    assert "os.environ.get('GDP_OFFLINE') == '1'" in conftest_src, (
        '离线模式必须由 GDP_OFFLINE=1 显式开启'
    )
    # 转换逻辑必须在 _OFFLINE 为假时直接返回
    m = re.search(r'def pytest_runtest_makereport.*?\n\n\n', conftest_src, re.S)
    assert m, '找不到 pytest_runtest_makereport'
    body = m.group(0)
    assert 'if not _OFFLINE' in body and 'return' in body, (
        '非离线模式下必须原样返回，不得改动任何 report'
    )


def test_t66_02_signature_is_narrow(conftest_src):
    """云不可达的签名必须收紧 —— 不得把业务断言失败当成云问题。

    这条防的是「为了让 CI 变绿，把 AssertionError 也加进签名」。
    那样做等于把整个套件变成一个永远绿的空壳。

    ⚠️ **用 AST 逐项取字面量，不做子串匹配。**
    第一版写的是 `'Exception' not in sigs`，被 `UnrecognizedClientException`
    误报 —— 它含 `Exception` 这个子串。本文件作者在同一天里第三次犯同类错误
    （前两次：正则匹配 docstring 里的散文、按单词 boto3 判断测试是否需要 AWS）。
    **判语义就要用语法树，不要用文本包含。**
    """
    import ast

    tree = ast.parse(conftest_src)
    entries = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, 'id', None) == '_CLOUD_UNREACHABLE'
                        for t in node.targets)):
            val = node.value
            assert isinstance(val, ast.Tuple), '_CLOUD_UNREACHABLE 应是元组'
            entries = [e.value for e in val.elts
                       if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            break
    assert entries, '找不到 _CLOUD_UNREACHABLE 的字符串条目'

    # 逐项比对：禁止把泛化的异常名单独作为一个条目
    FORBIDDEN_EXACT = {'AssertionError', 'Failed', 'Exception', 'BaseException',
                       'Error', 'RuntimeError'}
    bad = [e for e in entries if e.strip() in FORBIDDEN_EXACT]
    assert not bad, (
        f'签名里不得出现这些泛化条目: {bad} —— 它们会把真实的断言失败也 skip 掉，'
        f'让离线模式变成一个永远绿的空壳。'
    )
    # 每个条目都要足够 specific（挡住 'Error' 这类单词）。
    # ⚠️ 只对**纯 ASCII** 条目做长度判断：中文条目按字符数算天然很短
    # （'凭据未解析到' 只有 6 个字符），却是最 specific 的那一条 ——
    # 这是本文件作者当天第四次被「按文本长度/包含关系判语义」绊到。
    too_short = [e for e in entries
                 if e.strip().isascii() and len(e.strip()) < 12]
    assert not too_short, (
        f'这些 ASCII 条目过于宽泛，可能误命中无关失败: {too_short}'
    )
    # 必须依赖那条可识别的凭据错误文本（2026-09-09 给 9 处签名路径加的）
    assert any('凭据未解析到' in e for e in entries), (
        '签名应包含本仓库自己抛的凭据错误文本 —— 那是能精确判定'
        '「是凭据问题」而不是靠异常类型猜的唯一依据'
    )


def test_t66_03_min_passed_floor_actually_fails_the_run():
    """**行为验证**：通过数下限不满足时，整个运行必须以非 0 退出。

    ## 为什么必须是行为验证而不是静态检查

    第一版这条只断言源码里出现 `exitstatus = 1`。它通过了，
    而机制是坏的 —— 原实现写在 `pytest_terminal_summary` 里，
    那个 hook 在退出码定下来之后才跑，**只能打印、改不了结果**。
    实测退出码仍是 0：护栏「看起来有」而实际不生效。

    这是「静态检查 ≠ 行为验证」的又一个实例。
    只 skip 不真的让运行失败，等于把「测试没跑」伪装成「测试通过了」，
    比没有 CI 更糟 —— 它给出虚假的安全感。

    ## 为什么靶子必须是 `tests/` 里的真实节点

    第一版把一个 trivial 测试写进 `tmp_path` 再跑它 —— **conftest 没被加载**，
    因为 tmp_path 不在 `tests/` 的目录链上，于是 hook 根本没运行、
    退出码自然是 0，这条测试因为**错误的原因**失败。
    选一个 `tests/` 内必然通过、且自身不起子进程的节点作靶子。
    """
    import subprocess
    import sys

    target = ('tests/test_66_offline_mode_has_guardrails.py'
              '::test_t66_01_offline_mode_is_opt_in')
    env = {**os.environ, 'GDP_OFFLINE': '1', 'GDP_OFFLINE_MIN_PASSED': '99999'}
    r = subprocess.run(
        [sys.executable, '-m', 'pytest', target, '-q', '-p', 'no:cacheprovider'],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=300)

    assert r.returncode != 0, (
        '通过数低于下限时运行仍以 0 退出 —— 护栏无效。\n'
        'CI 只看退出码，只打印警告等于没有护栏。\n'
        f'stdout:\n{r.stdout[-1500:]}'
    )
    assert '未满足' in r.stdout, f'汇总里没有标出下限未满足:\n{r.stdout[-1200:]}'


def test_t66_03b_floor_satisfied_run_succeeds():
    """反向验证：下限满足时不得把正常运行判成失败。

    没有这一条，上一条可以用「永远返回非 0」作弊通过。
    """
    import subprocess
    import sys

    target = ('tests/test_66_offline_mode_has_guardrails.py'
              '::test_t66_01_offline_mode_is_opt_in')
    env = {**os.environ, 'GDP_OFFLINE': '1', 'GDP_OFFLINE_MIN_PASSED': '1'}
    r = subprocess.run(
        [sys.executable, '-m', 'pytest', target, '-q', '-p', 'no:cacheprovider'],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f'下限满足却判为失败:\n{r.stdout[-1500:]}'
    assert '满足' in r.stdout


def test_t66_03c_floor_check_is_in_sessionfinish(conftest_src):
    """下限判定必须写在 `pytest_sessionfinish` 里。

    静态补充上面两条行为验证：把它写回 `pytest_terminal_summary`
    会让退出码改不动（实测过），而行为测试可能因环境差异被 skip，
    所以这条守住实现位置。
    """
    m = re.search(r'def pytest_sessionfinish\(.*?\n\n\n', conftest_src, re.S)
    assert m, '缺少 pytest_sessionfinish —— 那是唯一能改 exitstatus 的位置'
    assert 'session.exitstatus = 1' in m.group(0), (
        'pytest_sessionfinish 里必须设置 session.exitstatus'
    )


def test_t66_04_skip_count_is_reported(conftest_src):
    """跳过数必须打印出来。

    不打印的话，「全部 skip」在 CI 面板上与「全部通过」长得一样 ——
    那是本机制最危险的失效方式。
    """
    m = re.search(r'def pytest_terminal_summary.*', conftest_src, re.S)
    body = m.group(0)
    assert '跳过' in body and '实际通过' in body, (
        '汇总必须同时打出「跳过数」与「实际通过数」，两个数缺一个都无法判断'
    )


def test_t66_05_skipped_report_says_it_is_not_a_pass(conftest_src):
    """转成 skip 的报告文本必须明说「这不是测试通过」。

    读 CI 日志的人看到 skip，默认会理解成「这条不重要」。
    对云不可达而言不是 —— 那条测试**没有被验证过**。
    """
    assert '这不是测试通过' in conftest_src, (
        'skip 的原因文本必须明说它不等于通过，否则读日志的人会误判覆盖度'
    )

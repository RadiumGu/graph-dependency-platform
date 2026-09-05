"""
tests/conftest.py — 全测试会话的 profile 注入

去掉「静默默认 profile」之后，任何读 profile 的代码路径都必须被显式告知用哪一份。
测试统一注入 ``fixtures/test_profile.yaml``（一个虚构的 acme-shop workload），
这同时是解耦的证明：测试不引用父仓库的 profiles/petsite.yaml。

需要验证「未配置就抛错」的用例请自行 ``set_active_profile(None)``，
并在 finally 里恢复——见 tests/test_dr_profile.py。
"""

import os
import sys

import pytest

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

TEST_PROFILE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "test_profile.yaml"
)


@pytest.fixture(autouse=True)
def _active_test_profile():
    """每个用例前把 active profile 设成测试 fixture，用例后清空。

    清空是刻意的：残留的 active profile 会让「未配置」类断言在整套测试里
    偶然通过，取决于用例执行顺序。
    """
    from dr_profile import set_active_profile

    set_active_profile(TEST_PROFILE_PATH)
    try:
        yield
    finally:
        set_active_profile(None)

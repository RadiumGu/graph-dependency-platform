"""
test_layer2_memory.py — Memory pressure test for Layer2 Probers.

Validates that Strands Orchestrator + tools stays under 2 GB memory.

Usage:
  cd rca && PYTHONPATH=.:.. pytest ../tests/test_layer2_memory.py -v -s
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_RCA = os.path.join(_PROJECT, "rca")
for p in [_PROJECT, _RCA]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _get_rss_mb() -> float:
    """Get current process RSS in MB."""
    import resource
    # getrusage returns KB on Linux
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_maxrss / 1024  # KB -> MB


@pytest.fixture(autouse=True)
def _restore_layer2_engine_env():
    """还原 LAYER2_ENGINE。

    2026-08-28:本文件两个测试都 `os.environ["LAYER2_ENGINE"] = ...` 且**从不还原**,
    泄漏到后续测试 —— 与本次修掉的 sys.modules 泄漏属同一类问题
    (测试改全局状态却不恢复,结果取决于执行顺序)。
    """
    saved = os.environ.get("LAYER2_ENGINE")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("LAYER2_ENGINE", None)
        else:
            os.environ["LAYER2_ENGINE"] = saved


def test_strands_memory_under_2gb():
    """Constructing Strands Layer2 engine should stay under 2 GB.

    2026-08-28:strands 包缺失时改为 **skip 而非 fail**。
    `make_layer2_engine` 在 strands 不可导入时按设计 warning + 回退 direct,
    于是 `assert engine.ENGINE_NAME == "strands"` 必然失败 ——
    这是环境缺件,不是代码缺陷。

    恒红项的危害不是它本身,而是它训练所有人忽略红色 —— 真缺陷会跟着被忽略。
    """
    pytest.importorskip(
        "strands",
        reason="未安装 strands 包，make_layer2_engine 会按设计回退 direct，"
               "本内存预算测试无从进行（安装见 requirements-dev.txt 可选段）",
    )

    import gc
    gc.collect()
    baseline_mb = _get_rss_mb()
    print(f"\nBaseline RSS: {baseline_mb:.0f} MB")

    os.environ["LAYER2_ENGINE"] = "strands"
    from engines.factory import make_layer2_engine

    engine = make_layer2_engine()
    assert engine.ENGINE_NAME == "strands", (
        "strands 包已安装但引擎仍回退到 direct —— "
        "检查 collectors.layer2_strands 的导入错误（factory 会吞成 warning）"
    )

    gc.collect()
    after_mb = _get_rss_mb()
    delta_mb = after_mb - baseline_mb
    print(f"After Strands construction: {after_mb:.0f} MB (delta: {delta_mb:.0f} MB)")

    # The engine + tools should add < 500 MB
    assert delta_mb < 500, f"Strands engine added {delta_mb:.0f} MB, exceeding 500 MB budget"
    assert after_mb < 2048, f"Total RSS {after_mb:.0f} MB exceeds 2 GB limit"


def test_direct_memory_baseline():
    """Direct engine should be very lightweight."""
    import gc
    gc.collect()
    baseline_mb = _get_rss_mb()

    os.environ["LAYER2_ENGINE"] = "direct"
    from engines.factory import make_layer2_engine

    engine = make_layer2_engine()
    assert engine.ENGINE_NAME == "direct"

    gc.collect()
    after_mb = _get_rss_mb()
    delta_mb = after_mb - baseline_mb
    print(f"\nDirect engine: {after_mb:.0f} MB (delta: {delta_mb:.0f} MB)")

    # Direct should add almost nothing
    assert delta_mb < 100, f"Direct engine added {delta_mb:.0f} MB, unexpected"

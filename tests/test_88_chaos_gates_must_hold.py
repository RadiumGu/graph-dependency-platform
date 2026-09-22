"""test_88 —— chaos 的两道闸门必须真的生效。

本文件守两个 2026-09-22 修掉的缺陷。它们的共同形状是**闸门给出了错误陈述
而不是沉默**：

  1. 时长闸门看到的秒数与真正注入的时长长期不是同一个数
  2. 删 CRD 失败照报 PASSED，而 tproxy 还挂在 Pod netns 上

两者都不会让任何测试变红，因为它们不产生异常 —— 只产生一个看起来正常的
错误结论。所以必须靠断言「闸门的输入值正确」和「失败被记录」来守。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "chaos" / "code", ROOT / "rca"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ─── 一、时长闸门 ──────────────────────────────────────────────────────────────

def test_t88_01_parse_duration_换算单位():
    """`rstrip('smh')` 只删尾字符、不换算，这是缺陷的根源。"""
    from runner.experiment import parse_duration

    assert parse_duration("30s") == 30
    assert parse_duration("10m") == 600, "分钟必须 ×60，不能只删掉 'm'"
    assert parse_duration("1h") == 3600, "小时必须 ×3600"
    assert parse_duration("30m") == 1800


def test_t88_02_闸门输入不得再用rstrip写法():
    """两处调用方都不能回退到 rstrip。

    旧写法在 runner.py:233 与 runner_strands.py:398，方向不同但后果相同：
      rstrip('smh')                  → '10m' 被当成 10 秒（低估 60 倍）
      getattr(exp, 'duration', 0)    → Experiment 无此字段，恒为 0（闸门全失效）
    """
    for rel in ("chaos/code/runner/runner.py",
                "chaos/code/runner/runner_strands.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "rstrip('smh')" not in src and 'rstrip("smh")' not in src, (
            f"{rel} 又出现 rstrip('smh') —— 它不换算单位，"
            f"'10m' 会被当成 10 秒。请用 duration_sec_for_policy()"
        )
        assert "getattr(experiment, 'duration'" not in src, (
            f"{rel} 又出现 getattr(experiment, 'duration') —— "
            f"Experiment 没有 duration 字段（时长在 fault.duration），恒返回 0"
        )


def test_t88_03_Experiment确实没有duration字段():
    """这条断言解释了为什么 getattr(experiment,'duration',0) 恒为 0。

    如果将来给 Experiment 加了 duration 字段，本测试会失败 —— 那时要回来
    确认闸门读的是哪一个，避免出现两个时长来源。
    """
    from runner.experiment import Experiment

    flds = getattr(Experiment, "__dataclass_fields__", {})
    assert "duration" not in flds, (
        "Experiment 新增了 duration 字段。请确认 duration_sec_for_policy() "
        "读的是哪个来源，不要让闸门和注入路径各读一个"
    )
    assert "fault" in flds, "时长应通过 fault.duration 取得"


@pytest.mark.parametrize("raw,expect", [
    ("30s", 30),
    ("10m", 600),
    ("1h", 3600),
])
def test_t88_04_闸门取值走正确换算(raw, expect):
    from runner.experiment import duration_sec_for_policy

    class _F:
        duration = raw

    class _E:
        fault = _F()

    assert duration_sec_for_policy(_E()) == expect


@pytest.mark.parametrize("bad", [None, "", "  ", "10minutes", "0.5m", 600])
def test_t88_05_无法解析时必须触发闸门而不是放行(bad):
    """fail-closed：不确定时长要让 PolicyGuard 拒绝，不能返回 0。

    返回 0 会让 rules.yaml 的 R008（max_duration_sec: 600, deny, high）
    判定「没超限」—— 用「我不知道」冒充「没问题」。
    """
    from runner.experiment import duration_sec_for_policy

    class _F:
        duration = bad

    class _E:
        fault = _F()

    got = duration_sec_for_policy(_E())
    assert got > 600, (
        f"duration={bad!r} 无法解析时返回了 {got}，不会触发 R008 的 600 秒闸门。"
        f"必须返回一个必然超阈值的值（fail-closed）"
    )


def test_t88_06_缺少fault属性也要fail_closed():
    from runner.experiment import duration_sec_for_policy

    class _NoFault:
        pass

    assert duration_sec_for_policy(_NoFault()) > 600


def test_t88_07_R008闸门规则仍在且阈值被本测试覆盖():
    """本文件的 >600 断言依赖 R008 的阈值。若规则被改，这里要同步。"""
    rules = yaml.safe_load(
        (ROOT / "chaos/code/policy/rules.yaml").read_text(encoding="utf-8"))

    def _walk(o):
        if isinstance(o, dict):
            if "max_duration_sec" in o:
                yield o["max_duration_sec"]
            for v in o.values():
                yield from _walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from _walk(v)

    thresholds = list(_walk(rules))
    assert thresholds, "rules.yaml 里找不到 max_duration_sec —— 时长闸门被删了？"
    assert max(thresholds) <= 600, (
        f"max_duration_sec 阈值升到了 {max(thresholds)}，"
        f"本文件 fail-closed 断言用的 >600 需要同步调大"
    )


# ─── 二、清理失败必须可见 ──────────────────────────────────────────────────────

def test_t88_08_结果对象有独立的清理失败字段():
    """清理失败与 status 是正交的两个维度，不能塞进 status。"""
    from runner.result import ExperimentResult

    flds = getattr(ExperimentResult, "__dataclass_fields__", {})
    assert "cleanup_failures" in flds, (
        "ExperimentResult 缺 cleanup_failures —— 删 CRD 失败会重新变成"
        "「只打一条日志，实验照报 PASSED」"
    )


def test_t88_09_删CRD失败必须写入结果而不只是打日志():
    """源码断言：except 分支里必须 append 到 cleanup_failures。"""
    src = (ROOT / "chaos/code/runner/runner.py").read_text(encoding="utf-8")
    assert "result.cleanup_failures.append" in src, (
        "runner.py 没有把清理失败写进 result.cleanup_failures"
    )


def test_t88_10_删CRD必须有自动重试():
    """硬约束：故障注入必须可自动恢复。

    此前删不掉就只打一条 error 走了，而 Phase 4 的 except 是局部的、不向上抛，
    _emergency_cleanup（只挂在异常路径）也不会被触发 —— 等于零次重试。

    注意定位方式：runner.py 里 FAULT_TO_DELETE_TYPE 出现三次（熔断路径 /
    Phase 4 / 紧急清理），必须锚到 Phase 4 那处，不能用第一个匹配。
    """
    src = (ROOT / "chaos/code/runner/runner.py").read_text(encoding="utf-8")
    idx = src.find('if exp.backend == "chaosmesh" and result.chaos_experiment_name:')
    assert idx > 0, "找不到 Phase 4 的 CRD 删除现场"
    window = src[idx:idx + 3000]
    assert "for attempt in range" in window, (
        "Phase 4 的 CRD 删除没有重试循环 —— 违反「故障注入必须可自动恢复」"
    )
    assert "_emergency_cleanup" in window, "重试全失败后没有兜底走紧急清理"


def test_t88_12_delete必须检查kubectl退出码():
    """`delete` 此前无条件 return True，使上层所有失败处理形同虚设。

    只有 subprocess 自己抛异常才返回 False；退出码非 0（权限不足、CRD 类型
    拼错）和 namespace 不符都被吞成成功，日志还打 ✅。
    """
    src = (ROOT / "chaos/code/runner/chaos_mcp.py").read_text(encoding="utf-8")
    idx = src.find("def delete(")
    assert idx > 0
    body = src[idx:idx + 3400]
    assert "returncode" in body, (
        "delete() 不检查 kubectl 退出码 —— 非 0 也会 return True，"
        "上层的重试与 cleanup_failures 记录都将永远拿不到失败"
    )


def test_t88_13_紧急清理必须接收namespace():
    """默认 namespace 是 'default'，而实验主要在 petadoptions。

    紧急清理当时不传 namespace，所以它打在 default 上，对绝大多数实验一个
    对象也删不到 —— 而这是最需要它生效的异常出口。
    """
    src = (ROOT / "chaos/code/runner/runner.py").read_text(encoding="utf-8")
    idx = src.find("def _emergency_cleanup(")
    assert idx > 0, "找不到 _emergency_cleanup"
    sig = src[idx:idx + 320]
    assert "namespace" in sig, (
        "_emergency_cleanup 签名里没有 namespace —— 会退回用 delete() 的默认值 "
        "'default'，而实验在 petadoptions"
    )

    # 所有调用点都必须显式传 namespace，否则改签名没有意义
    calls = [m for m in re.finditer(r"self\._emergency_cleanup\(", src)]
    assert calls, "找不到 _emergency_cleanup 的调用点"
    for m in calls:
        seg = src[m.start():m.start() + 400]
        seg = seg[:seg.find("\n\n")] if "\n\n" in seg else seg
        assert "namespace=" in seg, (
            f"第 {src[:m.start()].count(chr(10)) + 1} 行的 _emergency_cleanup "
            f"调用没有传 namespace"
        )


def test_t88_14_紧急清理失败要记入结果():
    src = (ROOT / "chaos/code/runner/runner.py").read_text(encoding="utf-8")
    idx = src.find("def _emergency_cleanup(")
    body = src[idx:idx + 2200]
    assert "cleanup_failures.append" in body, (
        "_emergency_cleanup 失败时没有记进 result.cleanup_failures"
    )


def test_t88_11_报告必须展示清理失败():
    """记了没人看等于没记。摘要表和 dict 导出都要有。"""
    src = (ROOT / "chaos/code/runner/report.py").read_text(encoding="utf-8")
    assert "cleanup_failures" in src, "report.py 不展示 cleanup_failures"
    assert src.count("cleanup_failures") >= 2, (
        "cleanup_failures 应同时出现在 markdown 摘要与 dict 导出里"
    )

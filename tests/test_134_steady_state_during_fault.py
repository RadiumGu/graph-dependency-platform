"""
test_134_steady_state_during_fault.py

守「注入期间必须求值稳态假设，且与熔断分层、不在无效样本上宣告健康」。

## 为什么需要这一层

本仓此前只有两个稳态时机：
  · `steady_state_before` —— 不满足抛 PreflightFailure，实验不跑（闸门）
  · `steady_state_after`  —— 逐项记 passed/failed（终局判定）

这等价于 Chaos Toolkit 的**默认**策略（method 前后各求值一次）。
缺的是它的 `continuously` 策略，而这是个真实缺口：

**只在首尾看两眼，会漏掉整个故障窗口内的瞬时违反。** 一次违反若在故障
结束前自行恢复，现状的结果是 before 通过、after 通过、实验判「无弱点」
—— 而系统其实失稳过。这正是本仓反复出现的那类缺陷：报告成功，
同时悄悄给出不完整的图景。

## 为什么不能跟 StopCondition 合并（本文件的核心不变量）

  · `StopCondition` 是**熔断**（guardrail）：阈值很低（如 success_rate < 40%），
    触发即 abort，职责是「别把生产打崩」。
  · `steady_state_during` 是**判定**（verdict）：阈值就是正常稳态要求
    （如 >= 99%），触发**只记录，不中断**。

合并会出两种坏结果：
  · 拿熔断阈值当稳态判定 → 判定失去意义；
  · 拿稳态阈值当熔断 → 实验几乎必然在 Phase 5 之前被 abort，
    边验证永远跑不到。**2026-08-31 的真实事故正是这个形状**
    （T+71s 成功率掉到 27.4% 触发 <40% 熔断，实验以 ERROR 收场、零判定）。

## 以及：不得在无效样本上宣告健康

`metrics.collect()` 在 ClickHouse 异常时 fallback 成
`success_rate=100.0 / total_requests=0`，所以在 `snap.ok=False` 的样本上
求值稳态会得到**假通过**。2026-08-31 的另一面就是这个 fallback 把
一条边误判成 `confirmed`（见 `MetricsSnapshot.ok` 上方注释）。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPERIMENT_PY = ROOT / 'chaos' / 'code' / 'runner' / 'experiment.py'
RUNNER_PY = ROOT / 'chaos' / 'code' / 'runner' / 'runner.py'
RESULT_PY = ROOT / 'chaos' / 'code' / 'runner' / 'result.py'


@pytest.fixture(scope='module')
def exp_src() -> str:
    return EXPERIMENT_PY.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def runner_src() -> str:
    return RUNNER_PY.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def result_src() -> str:
    return RESULT_PY.read_text(encoding='utf-8')


# ------------------------------------------------------------------ 结构在位

def test_t134_01_experiment_has_during_field(exp_src: str):
    assert 'steady_state_during' in exp_src, \
        "Experiment 缺 steady_state_during —— 注入期间无法求值稳态"


def test_t134_02_yaml_parses_during(exp_src: str):
    """两个 loader（普通 + composite）都要解析 steady_state.during。"""
    n = len(re.findall(r"steady_state_during=_parse_checks\(ss\.get\('during'", exp_src))
    assert n == 2, (
        f"只有 {n} 处解析 steady_state.during，应为 2 处"
        "（load_experiment 与 _load_composite_experiment）。"
        "漏掉 composite 的话，组合实验静默失去期间判定。"
    )


def test_t134_03_result_records_violations(result_src: str):
    for fld in ('steady_state_during_violations',
                'steady_state_during_samples',
                'steady_state_during_unavailable'):
        assert fld in result_src, f"ExperimentResult 缺 {fld}"


def test_t134_04_evaluated_in_observe_loop(runner_src: str):
    """必须在注入期间的观测循环里求值，而不是只定义了字段没人用。"""
    assert 'exp.steady_state_during' in runner_src, (
        "runner 里没有读 exp.steady_state_during —— 字段定义了但没接上，"
        "等于这层判定不存在。"
    )
    # 求值点必须在 Phase 3 的 while 循环内：用「它出现在 OBSERVE_INTERVAL
    # 那次 sleep 之前、且在 while time.time() < end_ts 之后」来定位。
    i_while = runner_src.find('while time.time() < end_ts')
    i_eval = runner_src.find('exp.steady_state_during')
    i_sleep = runner_src.find('time.sleep(self.OBSERVE_INTERVAL)')
    assert i_while != -1 and i_sleep != -1, "没找到 Phase 3 观测循环"
    assert i_while < i_eval < i_sleep, (
        "steady_state_during 的求值不在 Phase 3 观测循环内 "
        f"(while={i_while}, eval={i_eval}, sleep={i_sleep})"
    )


# ------------------------------------------- 核心不变量：判定不得兼任熔断

def test_t134_05_during_must_not_abort(runner_src: str):
    """期间稳态违反**绝不能**中断实验 —— 那是 StopCondition 的职责。

    本文件最重要的一条。拿稳态阈值去 abort，实验几乎必然在 Phase 5 之前
    被掐掉，边验证永远跑不到（2026-08-31 的真实事故）。
    """
    i = runner_src.find('exp.steady_state_during')
    assert i != -1
    # 取求值段到 Stop Conditions 检查之前
    j = runner_src.find('Stop Conditions 检查', i)
    seg = runner_src[i:j if j != -1 else i + 2500]
    for bad in ('AbortException', 'raise ', 'self.fis.stop', 'injector.delete'):
        assert bad not in seg, (
            f"期间稳态求值段里出现了 {bad!r} —— 判定不得兼任熔断。\n"
            "中断实验是 StopCondition 的职责；两者合并会让实验在 Phase 5 "
            "之前被掐掉，边验证永远跑不到。"
        )


def test_t134_06_layering_documented(exp_src: str):
    """分层理由必须写在代码里，不是只存在于 PR 描述。"""
    i = exp_src.find('steady_state_during')
    seg = exp_src[i:i + 2500]
    assert '熔断' in seg or 'guardrail' in seg, \
        "没写明 steady_state_during 与 StopCondition 的分层"
    assert '2026-08-31' in seg, (
        "没引用 2026-08-31 那次事故。这个分层不是审美偏好 —— "
        "它来自一次真实的「实验以 ERROR 收场、零判定」。"
    )


# --------------------------------- 核心不变量：不得在无效样本上宣告健康

def test_t134_07_skips_unavailable_samples(runner_src: str):
    """`snap.ok=False` 的采样点必须排除在判定之外。

    `metrics.collect()` 在 ClickHouse 异常时 fallback 成
    success_rate=100.0，所以在无效样本上求值会得到**假通过** ——
    在压根没拿到的数据上宣告系统健康。
    """
    i = runner_src.find('exp.steady_state_during')
    seg = runner_src[i:i + 2000]
    assert 'snap.ok' in seg, (
        "期间稳态求值没有检查 snap.ok。采集失败会 fallback 成 "
        "success_rate=100.0，于是「没采到」被判成「健康」。"
    )
    assert 'steady_state_during_unavailable' in seg, \
        "无效采样点没有单独计数 —— 「0 条违反」会分不清健康与没采到"


def test_t134_08_zero_violations_is_not_proof_without_samples(result_src: str):
    """样本数与不可用数都要记，否则「0 违反」不构成证据。"""
    i = result_src.find('steady_state_during_samples')
    seg = result_src[max(0, i - 900): i + 900]
    assert '压根没采到' in seg or '没采到' in seg, (
        "没写明为什么要记样本数 —— 「0 条违反」在零样本时毫无意义，"
        "这正是本仓反复出现的静默不完整。"
    )


def test_t134_09_during_not_defaulted_from_before(exp_src: str):
    """during 缺省必须为空，不得默认继承 before。

    during 的阈值常常要比 before 宽（故障期间允许一定降级）。
    默认继承会让所有存量实验突然开始记一堆本来预期之内的违反，
    把这层判定的信噪比一次性毁掉。
    """
    assert "ss.get('during', [])" in exp_src, "during 的缺省不是空列表"
    assert "ss.get('during', ss.get('before'" not in exp_src, \
        "during 默认继承了 before"


def test_t134_10_existing_two_phases_intact(exp_src: str, runner_src: str):
    """加 during 不得动摇原有的 before 闸门与 after 判定。"""
    assert 'steady_state_before' in exp_src and 'steady_state_after' in exp_src
    assert 'PrefightFailure' in runner_src or 'PreflightFailure' in runner_src, (
        "before 的闸门语义丢了 —— 注入前稳态不满足必须让实验不跑"
    )
    assert 'steady_state_after_checks' in runner_src, "after 的逐项判定丢了"

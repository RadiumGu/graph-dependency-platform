"""tests/test_73_scorecard_gates.py

韧性评分卡的判据门禁。

## 这条门禁在防什么

AWS 工作坊 Module 4 的评分卡判据是「每阶段耗时 + 是否恢复」——
本质是**流程跑通即通过**。这个前提在本项目已被证伪两次，
两次都往图谱写进了错误结论并撤回：

1. `took_effect` 拿不等长窗口比绝对计数（1800s/8470 vs 120s/548），
   `8470-548>0` 判「注入生效」，而速率 4.71 → 4.57 次/秒**根本没变**；
2. 复合实验删了 Pod，观测方吞吐必然塌陷，拿它当「依赖被切断」的证据。

实测印证：现有 8 条演练记录，按我们的门禁**全部**证据不可信。
若照抄工作坊判据，这 8 条会全部打勾，得出「恢复率 100%」。

**一份不校验注入生效性的评分卡，就是假证据的批量版本。**

## 三道门禁

1. 注入必须确实生效（`injection_confirmed is True`）
2. 观测方信号不得被混淆（复合手法删过 Pod 的只能记观察）
3. 基线窗与注入窗必须等长（否则计数不可比）

## 一个容易被改坏的设计：inconclusive 两边都不计

不满足门禁的记录**既不进分子也不进分母**。有人会想「进分母更保守吧」——
不对：那会把「实验没做成」算成「系统没恢复」，
而两者的下一步完全相反 —— 前者要修实验方法，后者要修系统。
"""
from __future__ import annotations

import pathlib
import sys

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'scripts',):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _rec(**over):
    r = {
        'edge': 'AccessesData a -> b',
        'injection_confirmed': True,
        'observer_window_seconds': 180,
        'verdict': 'confirmed',
        'observer_degradation_pct': 42.0,
    }
    r.update(over)
    return r


def test_t73_01_注入未生效必须记inconclusive():
    """没有打断就没有验证。"""
    import emit_resilience_scorecard as sc
    for eff in (False, None):
        cat, why = sc.classify_drill(_rec(injection_confirmed=eff))
        assert cat == 'inconclusive', (
            f'injection_confirmed={eff!r} 却被计入评分（{cat}）—— '
            f'注入没生效时任何恢复/未恢复的结论都是凭空的')
        assert '没有打断' in why or '生效' in why


def test_t73_02_复合手法删过Pod必须记inconclusive():
    """删 Pod 让观测方吞吐必然塌陷 —— 该信号没有判别力。"""
    import emit_resilience_scorecard as sc
    for over in ({'pods_deleted': True}, {'verdict': 'observation_only'}):
        cat, why = sc.classify_drill(_rec(**over))
        assert cat == 'inconclusive', f'{over} 却被计入评分（{cat}）'
        assert '混淆' in why or '塌陷' in why or '判别力' in why


def test_t73_03_窗口长度未记录必须记inconclusive():
    """不等长时计数不可比，而「没记录」无法证明等长。"""
    import emit_resilience_scorecard as sc
    r = _rec()
    r.pop('observer_window_seconds')
    cat, why = sc.classify_drill(r)
    assert cat == 'inconclusive'
    assert '等长' in why


def test_t73_04_证据齐全才进评分():
    import emit_resilience_scorecard as sc
    cat, _ = sc.classify_drill(_rec(verdict='confirmed'))
    assert cat == 'valid_not_recovered', (
        '注入生效 + 观测方退化 = 故障传导到调用方，应计入分母')


def test_t73_05_inconclusive两边都不计():
    """进分母会把「实验没做成」算成「系统没恢复」，下一步完全相反。"""
    import inspect

    import emit_resilience_scorecard as sc
    src = inspect.getsource(sc.scorecard)
    assert "buckets['valid_recovered']" in src
    assert "buckets['valid_not_recovered']" in src
    assert "buckets['inconclusive']" not in src.split('scored =')[1].split('\n')[0], (
        'inconclusive 被算进了 scored（分母）—— '
        '那会把「实验没做成」当成「系统没恢复」')


def test_t73_06_无可信记录时不得报0恢复率():
    """0% 表示试了都没恢复；无记录表示一次都没拿到可信证据。

    两者的下一步完全不同，报同一个数字会误导。
    """
    import emit_resilience_scorecard as sc
    import inspect
    src = inspect.getsource(sc.scorecard)
    assert 'if scored else None' in src, (
        'scored 为 0 时 recovery_rate_pct 必须是 None 而不是 0 —— '
        '0% 与「无可信证据」是两个不同的结论')


def test_t73_07_必须发证据质量指标():
    """证据质量掉下去比恢复率掉下去更值得警觉。

    它意味着实验方法本身在退化，产出的都是不可用的证据。
    """
    src = (ROOT / 'scripts' / 'emit_resilience_scorecard.py').read_text(
        encoding='utf-8')
    assert 'EvidenceQualityPct' in src, (
        '缺 EvidenceQualityPct 指标 —— '
        '只看恢复率看不出「我们的实验方法坏了」这种情况')


def test_t73_08_不得新建DynamoDB表作为第二真相来源():
    """工作坊用 DynamoDB 是因为它的闭环由 EventBridge 分阶段写表。

    我们的实验单进程跑完并落 JSON 留痕，再引一张表只会多一处
    需要同步的真相来源 —— 本仓库已因「同类清单各处一份」踩过坑。
    """
    src = (ROOT / 'scripts' / 'emit_resilience_scorecard.py').read_text(
        encoding='utf-8')
    lower = src.lower()
    assert 'dynamodb.table' not in lower and "resource('dynamodb')" not in lower, (
        '评分卡引入了 DynamoDB 表 —— 演练留痕已有 JSON 来源，'
        '多一处真相来源就多一处会漂移的地方')

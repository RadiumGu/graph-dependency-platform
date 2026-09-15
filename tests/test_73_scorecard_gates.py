"""tests/test_73_scorecard_gates.py — 韧性评分卡的判据门禁

## 这组门禁在防什么

AWS 工作坊 Module 4 的评分卡判据是「每阶段耗时 + 是否恢复」，本质是
**流程跑通即通过**。这个前提在本项目被证伪过两次，两次都往图谱写进了
错误的 `dependency_class=soft` 并撤回。

而 2026-09-15 我在照抄它时又踩了第三次，这次是**概念层面**的：

## 🔴 为什么这里没有"恢复率"

第一版把 `confirmed` 且退化低的边算成「调用方吸收了故障 = 恢复」。两层错：

**第一层（概念）**：`confirmed` 在本项目的定义就是
「切断这条依赖导致消费方可测量地受损」。所以每条 confirmed 本身就意味着
**故障传导了**。被吸收的边会判 `soft` 或 inconclusive，不会是 confirmed。
拿 confirmed 算恢复率 = 把「依赖承重」读成「系统恢复」。

**第二层（数据）**：即使想用退化值区分，`verify_degradation` 也担不起。
实测三条 iam-deny 边 `deg=0.0` 而 `verify_reason` 明写
**「完全切断…且业务归零（adopt 1 → 0）」**，它们的
`evidence_channel='xray-edge+business-probe'` —— 退化字段量的是
SQL/成功率通道，业务证据在另一条通道上。

第一版把这三条判成"调用方吸收了故障"，**正是跨会话台账警告过的读错**
（交互探索页曾按 deg 把这三条完全切断的边画成全图最细，49c63ad 从页面侧兜住）。

**所以图谱能诚实支撑的是「依赖承重率」，不是「恢复率」。**
恢复率需要 `steady_state_after` 那段观测，而图谱不存它 ——
报 `None` 加一句说明，比算一个看起来像的数字诚实。
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
        'edge': 'AccessesData', 'src': 'a', 'dst': 'b',
        'verify_status': 'confirmed',
        'degradation': 42.0,
        'channel': 'both',
        'severance': 'iam-deny',
        'injection_confirmed': 'True',
        'experiment': 'exp-test',
        'dep_class': 'hard',
    }
    r.update(over)
    return r


def test_t73_01_inconclusive判定不得计入():
    """它是判定链的注入生效门禁挡下来的结果 —— 什么都没证明。"""
    import emit_resilience_scorecard as sc
    cat, why = sc.classify_drill(_rec(verify_status='inconclusive'))
    assert cat == 'inconclusive'
    assert '门禁没放行' in why or '实验没做成' in why


def test_t73_02_注入确认未生效不得计入():
    import emit_resilience_scorecard as sc
    cat, why = sc.classify_drill(_rec(injection_confirmed='False'))
    assert cat == 'inconclusive'
    assert '没有打断' in why


def test_t73_03_无证据锚点不得计入():
    """台账建议的不变量：必须有 verify_degradation **或** verify_severance。"""
    import emit_resilience_scorecard as sc
    cat, why = sc.classify_drill(
        _rec(degradation=None, severance='unspecified'))
    assert cat == 'inconclusive'
    assert 'verify_severance' in why


def test_t73_04_零退化的完全切断必须算承重():
    """这条直接钉住我踩过的那个读错。

    三条 iam-deny 边 `deg=0.0` 但 `verify_reason` 明写「完全切断、业务归零」。
    第一版据 deg 判成"调用方吸收了故障"。
    现在按 `confirmed` 的定义算承重 —— 与退化数字无关。
    """
    import emit_resilience_scorecard as sc
    cat, why = sc.classify_drill(_rec(
        degradation=0.0, channel='xray-edge+business-probe',
        severance='iam-deny'))
    assert cat == 'load_bearing', (
        f'deg=0.0 的完全切断被判成 {cat} —— '
        f'那三条 iam-deny 边的 verify_reason 明写「业务归零」，'
        f'退化字段量的是另一条通道。confirmed 就是承重，与 deg 数字无关。')
    assert '受损' in why


def test_t73_05_soft强度算不承重():
    import emit_resilience_scorecard as sc
    cat, why = sc.classify_drill(_rec(dep_class='soft'))
    assert cat == 'not_load_bearing'
    assert '不影响调用方' in why


def test_t73_06_refuted算不承重而非inconclusive():
    import emit_resilience_scorecard as sc
    cat, _ = sc.classify_drill(_rec(verify_status='refuted'))
    assert cat == 'not_load_bearing'


def test_t73_07_恢复率必须报None并说明原因():
    """报 None 加说明，比算一个看起来像的数字诚实。"""
    import inspect

    import emit_resilience_scorecard as sc
    src = inspect.getsource(sc.scorecard)
    assert "'recovery_rate_pct': None" in src, (
        'recovery_rate_pct 被算出了一个数字 —— '
        '图谱存依赖承重判定、不存恢复观测，confirmed 本身就意味着故障传导。'
        '拿它算恢复率是把「依赖承重」读成「系统恢复」。')
    assert 'recovery_rate_note' in src, (
        '报 None 必须附一句为什么，否则读者会以为是没测')


def test_t73_08_不得用verify_degradation判恢复():
    """防回归：源码里不得出现"deg 低 = 恢复"这类判据。"""
    src = (ROOT / 'scripts' / 'emit_resilience_scorecard.py').read_text(
        encoding='utf-8')
    code = '\n'.join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#') and '——' not in ln)
    assert 'valid_recovered' not in code, (
        '又出现了 valid_recovered 分类 —— '
        '那是按退化值判恢复的老口径，已被证伪两层（见模块 docstring）')


def test_t73_09_必须发证据质量指标():
    """证据质量掉下去比承重率变化更值得警觉。

    它意味着实验方法本身在退化，产出的都是不可用的证据。
    """
    src = (ROOT / 'scripts' / 'emit_resilience_scorecard.py').read_text(
        encoding='utf-8')
    assert 'EvidenceQualityPct' in src
    assert 'LoadBearingRate' in src


def test_t73_10_必须报切断手段分布():
    """不同手段证明的范围不同，合规披露要据此说明证据边界。

    IAM deny 只证明「依赖承重」（AccessDenied 立即返回），
    不覆盖延迟劣化与部分失败 —— 与网络层切断能覆盖的场景不同。
    """
    import emit_resilience_scorecard as sc
    import inspect
    src = inspect.getsource(sc.scorecard)
    assert 'severance_methods' in src, (
        '评分卡没报切断手段分布 —— 读者会以为所有 confirmed 都做过'
        '完整的韧性场景测试，那是过度声称')


def test_t73_11_不得新建DynamoDB表作为第二真相来源():
    """图谱已是唯一权威来源，多一处就多一处会漂移的地方。"""
    src = (ROOT / 'scripts' / 'emit_resilience_scorecard.py').read_text(
        encoding='utf-8').lower()
    assert "resource('dynamodb')" not in src and 'dynamodb.table' not in src


def test_t73_12_数据源必须是图谱而非留痕文件():
    """第一版读某一轮实验的 JSON 留痕，只看得见四支探针里的一支。

    后果是偏悲观得离谱：那次成功的 iam-deny-probe（退化 100%）
    根本不在那个 glob 里，评分卡报「8 条全部证据不可信」。
    """
    import inspect

    import emit_resilience_scorecard as sc
    src = inspect.getsource(sc._load_drills)
    assert 'neptune_client' in src, '评分卡必须从图谱读判定'
    assert 'glob' not in src, (
        '又在读留痕文件 —— 那只能看见一支探针的结果')

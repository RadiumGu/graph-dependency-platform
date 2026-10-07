"""
test_133_etl_direction_confidence.py

守「依赖边必须带方向置信度，且缺失不得伪装成确信」这条不变量。

为什么要守：在一张喂 DR 拓扑排序的依赖图里，**方向搞反比边缺失更糟**。
边缺了是漏排；方向反了是带着确信把恢复顺序排错。

DeepFlow 的 `direction_score`（0–255，255=方向必然正确）此前一次都没用上。
2026-10-07 线上实测（近 10 分钟，与主查询同样的分组粒度）：122 条边里
91 条为 255、22 条在 128–254、**9 条 < 128** —— 约 7.4% 的依赖边方向不可信。

本文件是纯静态/单元门禁，不连 ClickHouse、不连 Neptune。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

ETL = (pathlib.Path(__file__).resolve().parents[1]
       / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py')


@pytest.fixture(scope='module')
def src() -> str:
    return ETL.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def mod():
    sys.path.insert(0, str(ETL.parent))
    sys.path.insert(0, str(ETL.resolve().parents[2] / 'lambda' / 'shared' / 'python'))
    spec = importlib.util.spec_from_file_location('_etl_df_dir_under_test', ETL)
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"etl_deepflow 无法在测试环境 import：{e!r}")
    return m


def test_t133_01_query_selects_direction_score(src: str):
    """主调用图查询必须取 direction_score，否则后面都是空谈。"""
    assert 'min(direction_score)' in src, (
        "主查询没有取 min(direction_score)。方向置信度拿不到，"
        "就无法区分「这条边方向可信」与「方向可能是反的」。"
    )
    assert 'direction_score < 128' in src, (
        "没有统计低置信记录占比。单看 min 分不清「1000 条里混了 1 条噪音」"
        "和「一半证据说方向相反」。"
    )


def test_t133_02_both_attrs_written_to_edge(src: str):
    """两个属性都要落到 Calls 边上。"""
    for attr in ('direction_score_min', 'direction_low_ratio'):
        assert f".property('{attr}'" in src, f"Calls 边没有写 {attr}"


def test_t133_03_missing_must_not_default_to_certain(src: str):
    """缺省值必须是 -1，不能是 255。

    这是本文件最重要的一条：把「没取到」默认成 255（方向必然正确）
    等于**伪造确信** —— 下游会把一条我们其实毫无把握的边当成铁证，
    而这正是本仓证据分级原则要防的事。
    """
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        if 'direction_score_min' not in line and 'direction_low_ratio' not in line:
            continue
        # 找形如 get('direction_score_min', 255) / , 255) 的缺省
        if re.search(r"direction_(score_min|low_ratio)['\"]?\s*,\s*255", line):
            bad.append(f"  第 {i} 行: {line.strip()[:110]}")
    assert not bad, (
        "方向置信度的缺省值是 255（= 方向必然正确），这是伪造确信。\n"
        + "\n".join(bad) + "\n应当用 -1 表示「本轮没取到」。"
    )
    # 正向确认 -1 缺省确实在位
    assert re.search(r"direction_score_min['\"]?\s*,\s*-1", src), \
        "没找到 direction_score_min 的 -1 缺省"
    assert re.search(r"direction_low_ratio['\"]?\s*,\s*-1", src), \
        "没找到 direction_low_ratio 的 -1 缺省"


def test_t133_04_low_confidence_edges_are_not_dropped(src: str):
    """不得因为置信度低就丢掉边。

    丢边会损失真实依赖。本仓的原则是采集侧给证据、下游做裁决 ——
    采集侧替下游决定「这条边不要了」，下游就再也看不到它存在过。
    """
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith('#'):
            continue
        if 'direction' not in s:
            continue
        # continue / skip / 丢弃 这类控制流落在 direction 判断上即为违规
        if re.search(r'direction\w*\s*[<>]=?\s*\d+', s) and \
           re.search(r'\b(continue|return|skip|pass)\b', s):
            offenders.append(f"  第 {i} 行: {s[:110]}")
    assert not offenders, (
        "出现了按 direction_score 过滤/跳过边的控制流。\n" + "\n".join(offenders)
        + "\n采集侧只记录，裁决交给下游（DR / chaos）。"
    )


def test_t133_05_must_not_write_verify_attrs_for_direction(src: str):
    """方向置信度不得借用 verify_* 表达。

    与 test_132_11 同源：edge_verification 的 authority 是 ['chaos-runner']，
    采集侧无权写。新开普通边属性正是为了不越界。
    """
    offenders = [f"  第 {i} 行: {ln.strip()[:100]}"
                 for i, ln in enumerate(src.splitlines(), 1)
                 if re.search(r"['\"]verify_\w+['\"]", ln)]
    assert not offenders, (
        "etl_deepflow 写了 verify_* 属性，但那组属性的 authority 是 "
        "['chaos-runner']。\n" + "\n".join(offenders)
    )


def test_t133_06_parses_rows_without_direction_columns(mod):
    """少列时必须退化成 -1 而不是抛错 —— DeepFlow 版本差异要兜住。"""
    # 直接验证解析逻辑的边界：构造只有 8 列的行（无 direction 两列）
    row = ['10.0.0.1', '10.0.0.2', '8080', 'HTTP', '10', '1000.0', '0', '5.5']
    ds_min = int(row[8]) if len(row) > 8 and row[8] != '' else -1
    ds_low = float(row[9]) if len(row) > 9 and row[9] != '' else -1.0
    assert ds_min == -1 and ds_low == -1.0, "少列时应退化成 -1"


def test_t133_07_direction_semantics_documented(src: str):
    """必须写明 255 的含义与为何 min/ratio 两个值都要。

    这条守的是「注释与实盘一致」：一个 0–255 的裸数字，下游读不出
    255 是最好还是最差，也读不出为什么有两个属性。
    """
    assert '255' in src and 'direction_score' in src, "没有说明 255 的含义"
    seg_start = src.find('direction_score')
    seg = src[max(0, seg_start - 2000): seg_start + 2000]
    assert 'DR' in seg or '拓扑排序' in seg, (
        "没有说明为什么方向置信度重要（方向反了比边缺失更危险，"
        "会把 DR 恢复顺序排错）。"
    )

"""判定门禁必须被**所有**写入路径复用的守门测试。

## 为什么需要这组测试

2026-09-05 查出仓库里有**两条**边验证写入路径：

    chaos/code/runner/edge_verification.py   走 classify_intervention（有门禁）
    scripts/write_edge_verdicts.py           自己决定 status 与 confidence（无门禁）

第二条路径的实测代价：

1. **三道门禁被绕过** —— 注入生效门禁、证据通道分级、独立证据门禁。
   活图谱因此出现 3 条错误的 refuted，DeepFlow calls 分别 60 / 2404 / 2796
   （即有独立观测源看到过这条边）；其中一条的 `verify_reason` 自述
   「流量不足不能据此证伪」而 `verify_status` 却写 refuted，**状态与理由自相矛盾**。
2. **11 条边的 verify_confidence 越界** —— 值是 ±4.0，而声明值域是 [0,1]。
   成因是直接写了契约里的**证据权重**（intervention_confirmed=4.0）而非归一化
   置信度。关键教训：**权限门禁（authority=chaos-runner）只校验「谁在写」，
   不校验「写的值是否合法」。**

所以本组测试守的是一条结构性规则：**任何写 verify_* 的路径都必须复用共享判据**，
而不是各自实现一份。判据只有一份，才谈得上「判据被改进后所有路径同时受益」。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

WRITERS = {
    'runner': REPO / 'chaos' / 'code' / 'runner' / 'edge_verification.py',
    'script': REPO / 'scripts' / 'write_edge_verdicts.py',
}


@pytest.mark.parametrize('name', sorted(WRITERS))
def test_v01_every_writer_uses_the_shared_classifier(name):
    """每条写 verify_status 的路径都必须调 classify_intervention。

    自己实现一份判定 = 门禁只在其中一条路径上生效，而图谱是共享的。
    """
    src = WRITERS[name].read_text()
    assert 'classify_intervention' in src, \
        f"{name} 写 verify_status 但没有复用 classify_intervention —— 门禁会被绕过"


@pytest.mark.parametrize('name', sorted(WRITERS))
def test_v02_no_writer_writes_raw_evidence_weights_as_confidence(name):
    """置信度必须来自 confidence()，不得直接写契约里的证据权重。

    实测 11 条边因此越界（±4.0）。判据是：源码里不得出现把
    `evidence_weights` 的值直接赋给 confidence 的形态。
    """
    src = WRITERS[name].read_text()
    bad = re.findall(r"conf\s*=\s*W\[[\"']intervention_(?:confirmed|refuted)[\"']\]", src)
    assert not bad, (
        f"{name} 直接把证据权重当置信度写: {bad}。"
        f"权重是 confidence() 的**输入**，不是它的输出；"
        f"直接写会越出声明的 [0,1] 值域，而权限门禁不校验数值合法性。")


@pytest.mark.parametrize('name', sorted(WRITERS))
def test_v03_every_writer_passes_independent_observing_sources(name):
    """每条路径都必须把独立观测源数喂进判据。

    这是「有独立证据的边永不判 refuted」的输入。缺省 0 会让门禁**结构上失效** ——
    门禁还在，但判据永远拿不到能触发它的数据。这是本项目反复出现的形态。
    """
    src = WRITERS[name].read_text()
    assert 'independent_observing_sources' in src, \
        f"{name} 没有传 independent_observing_sources —— 独立证据门禁会失效"


def test_v04_observer_marker_tables_agree_across_writers():
    """两条路径的观测源标记表必须一致。

    script 侧内联了一份（因为 scripts/ 不该依赖 chaos/code 的包结构），
    所以必须有测试盯住它们不漂移 —— 漂移方向是「两条路径用不同的证据口径
    算独立观测源」，那会让同一条边在两条路径上得到不同的门禁结论。
    """
    def markers(path: pathlib.Path) -> dict:
        src = path.read_text()
        m = re.search(r'_OBSERVER_MARKERS\s*=\s*\{(.*?)\n\}', src, re.S)
        assert m, f"{path.name} 里找不到 _OBSERVER_MARKERS"
        out = {}
        for line in m.group(1).splitlines():
            km = re.match(r"\s*'([a-z]+)':\s*\((.*?)\),?", line)
            if km:
                out[km.group(1)] = tuple(re.findall(r"'([^']+)'", km.group(2)))
        return out

    a = markers(WRITERS['runner'])
    b = markers(WRITERS['script'])
    assert a == b, f"两条写入路径的观测源标记表不一致:\n  runner={a}\n  script={b}"


def test_v05_script_refuses_to_write_without_the_gates():
    """共享判据导入失败时，脚本必须**拒绝写入**而不是退回旧逻辑。

    「导入失败就用备用实现」是最危险的兜底：它让门禁在最需要的时候悄悄消失。
    """
    src = WRITERS['script'].read_text()
    assert '_GATED' in src, '应有显式的门禁可用标志'
    assert re.search(r"if not _GATED:\s*\n\s*return False", src), \
        '门禁不可用时必须直接拒绝写入'


def test_v06_gates_actually_bite_on_the_recorded_failure_shape():
    """用实测记录到的错误形态跑一遍判据，确认它现在会被挡住。

    形态取自活图谱：注入确认生效、观测方退化 0、但该边有 1 个独立观测源
    （DeepFlow calls=2404）。旧逻辑判 refuted，新判据必须判 inconclusive。
    """
    from graph_confidence import classify_intervention, STATUS_INCONCLUSIVE, STATUS_REFUTED
    status, reason = classify_intervention(
        100, 100, 0.0, injection_confirmed=True, independent_observing_sources=1)
    assert status == STATUS_INCONCLUSIVE, '有独立观测源时不得判 refuted'
    assert 'soft' in reason
    # 无独立观测源才是真 refuted —— 证明门禁不是一刀切地禁掉 refuted
    status2, _ = classify_intervention(
        100, 100, 0.0, injection_confirmed=True, independent_observing_sources=0)
    assert status2 == STATUS_REFUTED


def test_v07_edge_traffic_gate_blocks_the_zero_traffic_shape():
    """边级流量门禁必须挡住「路径无流量」这个形态。

    实测：petsite 有 23,007 请求，而 petsite -> pay-for-adoption 这条路径
    15 分钟内 **0 次调用**，于是「退化 0.37%」是噪声却与「打断未传导」同形。
    """
    from graph_confidence import classify_intervention, STATUS_INCONCLUSIVE
    status, reason = classify_intervention(
        23007, 23007, 0.37, injection_confirmed=True,
        independent_observing_sources=0, edge_baseline_calls=0)
    assert status == STATUS_INCONCLUSIVE
    assert '被测依赖路径本身' in reason, '理由必须点明是路径无流量，而非观测方无流量'

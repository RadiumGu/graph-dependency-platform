"""test_138_rca_page_ab_and_no_live_filter.py — RCA 页的两条新口径必须被钉住。

## ① 证据链的 q1/q3 不得按 `kind='live'` 过滤

`live` = dynamic AND active=true，会把全部 static 声明边一起滤掉，而数据层依赖
（DynamoDB / RDS / S3）大多只有声明边。2026-10-15 实测：RDS 集群影响面 live=0、
不带 kind=11。MCP 工具描述与 Layer2 探针已改，demo 的证据链漏改了一个月 ——
它和那两处是**三个独立的调用方**，任何一处回退都不会让别处变红，所以单独钉。

## ② 「接图谱 vs 不接」A/B 一节必须从 fixture 现算、并保留读法警示

这一节是全站图谱价值的主证据。两种静默失效最容易出：
  · 数字写死 —— fixture 重抓后页面开始说谎；
  · 删掉「样本少 / 两臂同时带了 skill / 仍有泄露」这段警示 —— 那就是只报喜。
"""
import json
import re
from pathlib import Path

from paths import PROJECT_ROOT

_DEMO = Path(PROJECT_ROOT) / "demo"
PAGE = _DEMO / "pages" / "6_Root_Cause_Analysis.py"
FIXTURE = _DEMO / "fixtures" / "agent_ab_mcp.json"
SNAPSHOT = _DEMO / "fixtures" / "rca_evidence.json"


def _common():
    import sys
    sys.path.insert(0, str(_DEMO))
    import _common as C  # noqa: WPS433
    return C


def test_t138_01_证据链的根因与影响面不按live过滤():
    chain = {q: p for q, _, p, _ in _common().RCA_EVIDENCE_CHAIN}
    for q in ("q3_upstream_deps", "q1_blast_radius"):
        assert "kind" not in chain[q], (
            f"证据链的 {q} 又带上了 kind={chain[q].get('kind')!r}。"
            "live 会滤掉全部声明边，数据层根因（DynamoDB/RDS/S3）随之消失 —— "
            "实测 RDS 集群影响面 live=0、不带 kind=11。")


def test_t138_02_离线快照与证据链口径一致():
    """快照是用旧证据链抓的，离线模式就会继续展示 live 过滤后的结果。"""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["by_service"]
    for svc, ev in snap.items():
        for q in ("q3_upstream_deps", "q1_blast_radius"):
            params = (ev.get(q) or {}).get("params") or {}
            assert "kind" not in params, (
                f"离线快照 {svc}/{q} 仍带 kind={params.get('kind')!r} —— "
                "重跑 demo/fixtures/refresh_fixtures.py。")


def _ab_section() -> str:
    src = PAGE.read_text(encoding="utf-8")
    a = src.index('C.fixture("agent_ab_mcp")')
    b = src.index("#### B　")
    return src[a:b]


def test_t138_03_AB数据必须是可核对的真实记录():
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = d.get("cases") or []
    assert {c["round"] for c in cases} == {1, 2}, "两轮缺一，第一轮的混杂说明就无从对照"
    for c in cases:
        for arm in ("mcp", "base"):
            r = c[arm]
            assert r.get("task_id") and r.get("execution_id"), f"{c['label']}/{arm} 缺可核对的 ID"
            assert r.get("verdict") in d["verdict_scale"], f"{c['label']}/{arm} 判定不在刻度内"
            assert isinstance(r.get("minutes"), (int, float)) and isinstance(r.get("tools"), int)
        assert c.get("verdict_basis"), f"{c['label']} 没写判定依据 —— 人工判定必须可追问"
        # 两臂的唯一变量是图谱：接图谱的一臂必须真的调过图谱，另一臂必须一次没调
        assert c["mcp"]["graph_calls"] > 0, f"{c['label']} 接图谱一臂没调图谱，对照失效"
        assert c["base"]["graph_calls"] == 0, f"{c['label']} 不接图谱一臂调到了图谱，对照被污染"


def test_t138_04_AB数字从fixture现算不写死():
    sec = _ab_section()
    assert "_arm_stats(" in sec and "median" in sec, "A/B 指标没有从 fixture 现算"
    # 命中数、耗时、工具调用这三组对照不得出现成对的数字字面量
    hard = re.findall(r'"[^"{]*\b\d+(?:\.\d+)?\s*vs\s*\d+(?:\.\d+)?[^"]*"', sec)
    assert not hard, f"A/B 对照数字写成了字面量：{hard}"


def test_t138_05_必须保留读法警示():
    sec = _ab_section()
    for must in ("不够统计显著", "两者的贡献没有拆开", "known_leaks_round2", "rounds"):
        assert must in sec, (
            f"A/B 一节丢了「{must}」。样本量、MCP 与 skill 未拆分、第二轮残留的提示、"
            "第一轮准确率封顶 —— 少说任何一条都是只报喜。")

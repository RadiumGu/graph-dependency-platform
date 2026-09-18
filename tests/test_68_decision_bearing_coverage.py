"""tests/test_68_decision_bearing_coverage.py — 承重边覆盖目标的门禁。

## 这一段在防什么

「已验证覆盖率 14.5%」容易被读成「86% 的图没验过」。但那个分母里：

  - 一半的边**有直接观测证据**（X-Ray / NFM 看到过真实流量），
    对它们注入只是复核观测已经证明的事；
  - 更要紧的是**并非每条边错了都会改变一个决策**。

所以 `1_Edge_Verification.py` 加了一段把目标收敛到「错了会改变结论」的边。
判据是两道叠加：**位于割点关联路径上** 且 **零独立观测**。

## 为什么判据不能宽一点

试过一个更自然的定义并**实测否掉**：「能被决策查询触达的边」。
`q3_upstream_deps` 逐个依赖方跑下来，触达了**全部 110 条边，占 100%** ——
那个定义筛不掉任何东西。根因是 q3 只「列出依赖」，
而列错一项的代价远低于判错一个单点故障。

真正产出**结论**的是 `q_articulation_chokepoints`：它给出的 `blocked` 数
就是爆炸半径。割点关联边错了，那个数直接错。

## 判据刻意不用 q16

`q16_single_point_of_failure` 的判据是「只在单个 AZ 且被 ≥2 个服务依赖」，
实测返回的是 EC2 实例，与依赖边不直接相关
（背景见 `tests/test_59_chokepoint_spof.py`）。

## 本文件的断言只锁**机制**，不锁数字

覆盖率、割点数、承重边数都会随实验推进而变，锁死数字等于每跑一次实验就红一次。
所以断言的是：桥函数可用、判据是两道叠加、承重集合真的是全集的子集、
以及页面确实用了 `q_articulation_chokepoints` 而不是抄一份 cypher。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PAGE = _ROOT / "demo" / "pages" / "1_Edge_Verification.py"
_COMMON = _ROOT / "demo" / "_common.py"
for _p in (str(_ROOT), str(_ROOT / "rca"), str(_ROOT / "demo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _code_only(text: str) -> str:
    """剥掉注释与三引号块 —— 否则断言会匹配到本仓库详尽的说明文字。

    本会话统计过：判据错误里至少 4 次是「断言匹配到自己写的注释」。
    """
    text = re.sub(r'""".*?"""', "", text, flags=re.S)
    text = re.sub(r"'''.*?'''", "", text, flags=re.S)
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


# ── t68_01：页面必须复用 dr 的割点查询，不能抄一份 cypher ─────────────────────
def test_t68_01_page_reuses_chokepoint_query_not_a_copy():
    code = _code_only(_PAGE.read_text(encoding="utf-8"))

    assert "q_articulation_chokepoints" in code, (
        "边验证页没有引用 q_articulation_chokepoints。"
        " 承重边判据依赖割点分析 —— 没有它，这一段就退化成「零观测边」那个宽判据。")

    # 抄一份 cypher 的特征：页面里出现割点查询独有的结构。
    # 该查询算的是「阻断数」，抄过来必然带 blocked 这个别名。
    assert not re.search(r'AS\s+blocked', code), (
        "页面里出现了 `AS blocked` —— 像是把 q_articulation_chokepoints 的 cypher"
        " 抄了一份。本仓库栽过同形的两次（依赖边清单 aead7e1、scope 映射 93e3120），"
        " 抄一份之后两边会各自漂移。请走 C.dr_query_module()。")


# ── t68_02：桥函数必须存在且真的走 load_module ────────────────────────────────
#
# ⚠️ 这一条第一版**反向验证没变红**：我把 `return qc.load_module("dr")` 换成
#    `return None` 之后它仍是绿的 —— 因为断言查的是**原始源码**，而
#    `dr_query_module` 的 docstring 里就写着 "load_module"。
#    这正是本会话统计过至少 4 次的「断言匹配到自己写的说明文字」，
#    我在自己的门禁里又犯了第 5 次。所以这里必须先 `_code_only()`。
def test_t68_02_bridge_exists_and_explains_itself():
    raw = _COMMON.read_text(encoding="utf-8")
    code = _code_only(raw)

    assert "def dr_query_module" in code, (
        "demo/_common.py 缺 dr_query_module()。"
        " 页面需要它来跨到 dr-plan-generator（目录名带连字符，不能当包导入）。")
    assert "load_module" in code, (
        "dr_query_module 没走 query_catalog.load_module —— "
        "自己改 sys.path 会引入一个顶层 `graph` 包，在 Streamlit 进程里容易撞名。")
    # 说明必须留着：下一个人很容易「顺手」改成 sys.path.insert。
    # 这一条查的是**注释/文档**，所以刻意用 raw。
    assert "sys.path" in raw, (
        "dr_query_module 的说明里没提为什么不改 sys.path —— "
        "那是这个函数存在的唯一理由，删掉说明后下一个人会直接改回去。")


# ── t68_03：判据必须是两道叠加，缺一道就退化 ──────────────────────────────────
def test_t68_03_criterion_is_both_chokepoint_and_zero_observation():
    code = _code_only(_PAGE.read_text(encoding="utf-8"))
    seg = code.split("q_articulation_chokepoints", 1)[1] if \
        "q_articulation_chokepoints" in code else ""
    assert seg, "找不到割点那一段"
    # 第二道筛子：零观测。页面用 _no_obs 这个已有表达式（与其他段落同源）。
    assert "_no_obs" in seg, (
        "承重边判据只有割点这一道，缺「零观测」那一道。"
        " 少了它，有观测证据的边也会被算进待攻清单 —— 而对那些边注入"
        "只是复核观测已经证明的事。")
    assert "零观测" in seg, "承重集合没有按零观测过滤（缺少结果列/过滤）"


# ── t68_04：承重集合必须是全集的真子集（在线核对真实数据）────────────────────
@pytest.mark.neptune
def test_t68_04_core_set_is_a_strict_subset():
    if os.environ.get("GDP_OFFLINE"):
        pytest.skip("GDP_OFFLINE：本用例需要真实 Neptune")

    from neptune import neptune_client as neptune_rca
    from neptune.neptune_queries import _dependency_edge_labels
    from neptune import query_catalog as qc

    labels = ", ".join(f"'{x}'" for x in _dependency_edge_labels())
    dr = qc.load_module("dr")
    names = [c.get("chokepoint") for c in (dr.q_articulation_chokepoints() or [])
             if c.get("chokepoint")]
    assert names, "割点分析返回空 —— 判据失去输入，页面那一段会静默跳过"

    no_obs = ("e.xray_call_count IS NULL AND e.xray_last_seen IS NULL "
              "AND e.nfm_flow_count IS NULL AND e.nfm_last_seen IS NULL "
              "AND e.calls IS NULL AND e.error_rate IS NULL "
              "AND e.image_ref IS NULL")

    total = neptune_rca.results(
        f"MATCH ()-[e]->() WHERE type(e) IN [{labels}] RETURN count(*) AS c"
    )[0]["c"]
    incident = neptune_rca.results(
        f"MATCH (a)-[e]->(b) WHERE type(e) IN [{labels}] "
        f"AND (coalesce(a.name,a.arn) IN {names} "
        f"OR coalesce(b.name,b.arn) IN {names}) RETURN count(*) AS c"
    )[0]["c"]
    core = neptune_rca.results(
        f"MATCH (a)-[e]->(b) WHERE type(e) IN [{labels}] AND {no_obs} "
        f"AND (coalesce(a.name,a.arn) IN {names} "
        f"OR coalesce(b.name,b.arn) IN {names}) RETURN count(*) AS c"
    )[0]["c"]

    print(f"\n全部 {total} / 割点关联 {incident} / 承重且零观测 {core}")

    assert 0 < core <= incident <= total, (
        f"层级关系不成立：承重 {core} / 割点关联 {incident} / 全部 {total}。"
        " 三者必须逐层收窄，否则那段收敛叙事是假的。")
    assert core < total, (
        f"承重集合 {core} 等于全集 {total} —— 判据没有收窄任何东西。"
        " 这正是「能被决策查询触达」那个宽判据被否掉的原因"
        "（q3_upstream_deps 触达 100% 的边）。若这里也变成 100%，"
        " 说明割点判据失效了，别把断言放宽，去查割点分析。")

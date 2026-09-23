"""NL→Cypher 引擎的金标回归 —— `rca/neptune/nl_query_*.py`。

## 两层，只有第二层要钱

    test_grade_*        判据逻辑的纯单测，**不联网**，随套件每次跑
    test_golden_*       活体：真 Neptune + 真 Bedrock，需 RUN_GOLDEN=1

分层的理由是成本：活体一轮 6 题约 30 秒、每题一次 Bedrock 调用。让它进默认
套件会让每个人每次跑测试都付钱，然后它就会被 skip 掉，回归保护随之消失。
判据逻辑本身没有外部依赖，那部分必须无条件跑 —— 否则判据坏了没人知道，
而**判据坏了会伪造结论**（见下）。

## 为什么判据逻辑值得单测

2026-09-23 的 harness 对照实验里，测量代码出了两个缺陷，两个都会得出
**反向结论**：

  · 用 `str(AgentResult.message)` 取文本 —— message 是嵌套 dict，`str()` 出来
    的 repr 里换行是字面 `\\n`，要求真换行的正则匹配不上，六题全判
    `parse_fail`、报 0/6。而输出里明明有格式正确的 json 块。
  · 一个 agent 实例连着答 6 题 —— 每题背着前面所有问答，input token 单调
    累积（843→2015→3552→5543→8151→11273），算出 "+352%"。那是拿
    「6 题独立查询」比「一段 6 轮对话」。

两者都是**测量工具自己的偏差看起来像结论**。所以判据有单测，且用例里刻意
放了「应为空却返回了东西」这种反例。
"""
from __future__ import annotations

import os
import sys
import time

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_RCA = os.path.join(_PROJECT, "rca")
for _p in (_PROJECT, _RCA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_GOLDEN_DIR = os.path.join(_HERE, "golden", "nl_query")
with open(os.path.join(_GOLDEN_DIR, "cases.yaml"), encoding="utf-8") as _f:
    _CASES = yaml.safe_load(_f)["cases"]


# ── 判据 ────────────────────────────────────────────────────────────────

def flatten_scalars(rows) -> list:
    """把结果行压成可比较的标量列表（`count` / `empty` 判据用）。

    **不按列名比** —— 引擎给的列名不受控（可能是 name / d.name / service），
    那不是被测能力。
    """
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            out.extend(r.values())
        else:
            out.append(r)
    return out


def columns_of(rows) -> list[set]:
    """把结果**按列**拆成若干值集合。

    ## 为什么 `set` 判据不能用 flatten

    第一版 `set` 判据是「所有列摊平后与真值集合相等」。实测 q6 上炸了：
    引擎返回了多列（源节点 name + 它的标签 + 边类型），摊平后当然多出
    `['AccessesData','BelongsTo','Database','LambdaFunction']` —— 而失败信息
    里「缺 []」已经说明真值一个不少，多出来的只是描述性列。

    也就是说**引擎答对了，判据判错了**。这是本会话第三次「判据切了个语法
    片段而不是表达意图」（前两次：`str(AgentResult.message)` 取文本导致
    六题全 parse_fail；一个 agent 实例连答 6 题导致 token 虚高 352%）。

    意图是「**某一列**恰好是期望的集合」。所以按列比：任一列的值集合等于真值
    即算通过。这不可赖 —— 每列都要独立完全匹配，靠多返回几列蒙不中；
    同时保留了「不在乎列名」这个正确的放宽。
    """
    cols: dict = {}
    for i, r in enumerate(rows or []):
        if isinstance(r, dict):
            for k, v in r.items():
                cols.setdefault(k, set()).add(str(v))
        else:
            cols.setdefault("_scalar", set()).add(str(r))
    return list(cols.values())


def grade(case: dict, results, truth_rows) -> tuple[bool, str]:
    """返回 (是否正确, 人类可读的原因)。原因要能让人不看代码就知道为什么错。"""
    kind = case["check"]
    got = flatten_scalars(results)
    want = flatten_scalars(truth_rows)

    if kind == "empty":
        ok = len(got) == 0
        return ok, ("返回空，正确" if ok else
                    "应为空却返回了 %d 项 %r —— 这是**编答案**，比答错更严重"
                    % (len(got), got[:5]))
    if kind == "count":
        wnum = [v for v in want if isinstance(v, (int, float))]
        gnum = [v for v in got if isinstance(v, (int, float))]
        if not wnum:
            return False, "真值查询没取到数字 —— 金标集本身有问题，不是引擎的错"
        if not gnum:
            return False, "引擎没返回数字：%r" % (got[:5],)
        ok = int(gnum[0]) == int(wnum[0])
        return ok, ("%d == %d" % (int(gnum[0]), int(wnum[0])) if ok else
                    "引擎 %d ≠ 真值 %d" % (int(gnum[0]), int(wnum[0])))
    if kind == "set":
        # **列对列**比：每个真值列都要能在结果列里找到一个完全相等的。
        #
        # 第二版是「结果的某一列 == 真值所有列摊平后的并集」。单列答案没问题，
        # 但真值是多列时永远匹配不上：2026-09-23 的多步实验里 M1 的真值是
        # (prop, count) 两列，三个引擎都把名字与计数分两列正确返回，判据却报
        # 「最接近的一列缺 ['1','11','15','20']」—— 那几个正是计数值。
        # **三个引擎都答对了，判据把它们全判错。**
        #
        # 这是同一族缺陷的第四次。写判据时要问的不是「怎么比」，
        # 而是「我到底想断言什么」——这里想断言的是「期望的每一列都在」。
        want_cols = columns_of(truth_rows)
        got_cols = columns_of(results)
        if not want_cols:
            return (not got_cols), ("真值与结果都为空" if not got_cols else
                                    "真值为空但引擎返回了 %d 列" % len(got_cols))
        unmatched = [w for w in want_cols if not any(g == w for g in got_cols)]
        if not unmatched:
            return True, "%d 列全部匹配（各 %s 项）" % (
                len(want_cols), "/".join(str(len(w)) for w in want_cols))
        if not got_cols:
            return False, "引擎没返回任何行（真值 %d 列）" % len(want_cols)
        w0 = unmatched[0]
        best = max(got_cols, key=lambda s: len(s & w0))
        return False, ("%d/%d 列没匹配上。其中一列缺 %r 多 %r"
                       % (len(unmatched), len(want_cols),
                          sorted(w0 - best)[:4], sorted(best - w0)[:4]))
    return False, "未知判据 %r —— 金标集写错了" % kind


# ── 第一层：判据逻辑单测（无条件跑，不联网）────────────────────────────

def test_grade_count_相等判对_不等判错():
    c = {"check": "count"}
    assert grade(c, [{"n": 8}], [{"c": 8}])[0] is True
    ok, why = grade(c, [{"n": 7}], [{"c": 8}])
    assert ok is False and "7" in why and "8" in why


def test_grade_set_不按列名比():
    """引擎用别的列名不算错 —— 那不是被测能力。"""
    c = {"check": "set"}
    assert grade(c, [{"svc": "a"}, {"svc": "b"}], [{"name": "a"}, {"name": "b"}])[0]


def test_grade_set_缺项与多项都判错且说清是哪些():
    c = {"check": "set"}
    ok, why = grade(c, [{"n": "a"}], [{"n": "a"}, {"n": "b"}])
    assert ok is False and "b" in why


def test_grade_empty_编答案必须判错():
    """最有判别力的一条：应为空却返回内容，是编答案。"""
    c = {"check": "empty"}
    assert grade(c, [], [])[0] is True
    ok, why = grade(c, [{"name": "invented-svc"}], [])
    assert ok is False and "编答案" in why


def test_grade_真值缺失时归咎金标集而不是引擎():
    """真值查不出数字时必须说是金标集的问题 —— 否则会把锅甩给引擎。"""
    ok, why = grade({"check": "count"}, [{"n": 1}], [])
    assert ok is False and "金标集" in why


def test_金标集自身完整性():
    """每题必须有 question / truth_q / check，且 check 是已实现的三种之一。"""
    assert _CASES, "金标集是空的"
    seen = set()
    for c in _CASES:
        for k in ("id", "question", "truth_q", "check"):
            assert c.get(k), "用例 %r 缺 %s" % (c.get("id"), k)
        assert c["check"] in ("count", "set", "empty"), (
            "用例 %s 的 check=%r 未实现" % (c["id"], c["check"]))
        assert c["id"] not in seen, "用例 id 重复: %s" % c["id"]
        seen.add(c["id"])
    # `empty` 那题不许被删掉 —— 它是唯一测「不编答案」的
    assert any(c["check"] == "empty" for c in _CASES), (
        "金标集里没有 empty 判据的用例 —— 那就没有任何一题在测"
        "「问不存在的东西时是否会编答案」")



def test_grade_set_多余的描述列不算错():
    """引擎多返回几列描述信息不算答错 —— 只要**某一列**恰好是期望集合。

    实测 q6：引擎返回了源节点 name + 它的标签 + 边类型三类值，摊平比会多出
    ['AccessesData','BelongsTo','Database','LambdaFunction'] 而判错，
    但失败信息里「缺 []」已说明真值一个不少。那是判据的错，不是引擎的错。
    """
    c = {"check": "set"}
    rows = [{"name": "a", "label": "Microservice", "rel": "AccessesData"},
            {"name": "b", "label": "LambdaFunction", "rel": "BelongsTo"}]
    assert grade(c, rows, [{"n": "a"}, {"n": "b"}])[0] is True


def test_grade_set_多返回几列蒙不中():
    """按列比不可赖：每列必须独立完全匹配。"""
    c = {"check": "set"}
    # 每列都只含真值的一部分，没有任何一列等于 {a,b}
    rows = [{"c1": "a", "c2": "x"}, {"c1": "zzz", "c2": "b"}]
    ok, why = grade(c, rows, [{"n": "a"}, {"n": "b"}])
    assert ok is False and "没匹配上" in why


def test_grade_set_多列真值必须列对列比():
    """真值两列时，引擎分两列正确返回必须判对。

    2026-09-23 多步实验 M1：真值是 (prop, count) 两列，三个引擎都正确地把
    名字与计数分列返回，而当时的判据拿「结果单列 vs 真值所有列并集」比，
    把三个引擎全判错。判据的错看起来像三个引擎一起失败。
    """
    c = {"check": "set"}
    truth = [{"prop": "verify_status", "count": 30}, {"prop": "verify_at", "count": 1}]
    got = [{"name": "verify_status", "n": 30}, {"name": "verify_at", "n": 1}]
    ok, why = grade(c, got, truth)
    assert ok is True, why


def test_grade_set_少一列要判错():
    """只答对一半（只给名字不给计数）不能算过。"""
    c = {"check": "set"}
    truth = [{"prop": "a", "count": 1}, {"prop": "b", "count": 2}]
    got = [{"name": "a"}, {"name": "b"}]          # 缺计数那一列
    ok, why = grade(c, got, truth)
    assert ok is False and "没匹配上" in why

# ── 第二层：活体金标（真 Neptune + 真 Bedrock，需 RUN_GOLDEN=1）────────

@pytest.fixture(scope="module")
def engine():
    from neptune.nl_query_strands import StrandsNLQueryEngine
    return StrandsNLQueryEngine()


@pytest.fixture(scope="module")
def truth(neptune_rca):
    """每题的期望值 —— **测试自己的 Cypher 现查**，不写死、不用引擎的 Cypher。"""
    return {c["id"]: neptune_rca.results(c["truth_q"]) for c in _CASES}


@pytest.mark.neptune
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_golden_nl_query(case, engine, truth):
    """自然语言问题 -> 引擎 -> 结果必须等于独立算出的真值。需 RUN_GOLDEN=1。"""
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")

    t0 = time.time()
    r = engine.query(case["question"])
    elapsed = time.time() - t0

    assert not r.get("error"), "引擎报错: %s" % r.get("error")
    ok, why = grade(case, r.get("results"), truth[case["id"]])

    tu = r.get("token_usage") or {}
    print("\n  %s  %s  %.1fs" % (case["id"], "✓" if ok else "✗", elapsed))
    print("    cypher: %s" % str(r.get("cypher"))[:160])
    print("    tokens: in=%s out=%s cache_read=%s cache_write=%s"
          % (tu.get("input"), tu.get("output"),
             tu.get("cache_read"), tu.get("cache_write")))
    assert ok, "%s: %s" % (case["id"], why)

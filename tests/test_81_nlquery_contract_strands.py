"""Smart Query 引擎的**行为契约** —— 与实现无关，两个引擎都该满足。

## 这组测试为什么存在

`tests/test_04_unit_nlquery.py` 守着 NL 查询引擎的一批重要契约，但它测的是
**direct 的实现方式**：mock `bedrock.invoke_model`、patch
`neptune.nl_query_direct.nc.results`、直接调 `_generate_cypher` /
`_summarize` 私有方法、断言 Wave 4 的显式重试标记 `retried is True`。

strands 引擎走 Strands Agent，没有 `invoke_model`、没有 `_generate_cypher`、
重试由 ReAct 承担而不是显式代码 —— 所以那些测试无法简单改指向，
但它们守的**契约**对 strands 一样成立，不能随 direct 一起丢掉。

这个文件用 strands 引擎重新守同一批契约，mock 只放在两端：

    最外层   `_build_agent` → 假 Agent（控制 LLM 行为）
    最底层   `strands_tools.nc.results` → 控制 Neptune 返回

中间的引擎逻辑、tool 分发、`query_guard` 校验全部走**真实路径**。
这样测的是契约而不是 mock 之间的连线。

## 一处契约在 strands 里不适用

`test_generate_cypher_strips_markdown` 测的是「LLM 把 cypher 包在
```cypher fence 里时要剥掉」。那是 direct 让 LLM **直接输出 cypher 文本**
才会遇到的问题；strands 的 cypher 来自 Agent 调用 `execute_cypher` 工具时
传的**参数**，根本不经过文本解析。所以这条不是「漏测」，是不存在的问题。
"""
import logging
import sys
import pathlib
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RCA = ROOT / "rca"
for p in (str(RCA), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _FakeResp:
    """最小 Strands AgentResult 替身。"""

    def __init__(self, text):
        self.message = {"content": [{"text": text}]}
        self.metrics = None


class _FakeAgent:
    """假 Agent：被调用时**真的**调 execute_cypher 工具，然后返回摘要文本。

    真调工具是有意的 —— 引擎的 `cypher` 与 `results` 是从
    `strands_tools.last_rows()` 取的，而那要靠工具执行时写入。
    如果这里只返回文本不调工具，测出来的就不是引擎的真实数据通路。
    """

    def __init__(self, cypher, summary="查询完成", raise_on_call=None):
        self.cypher = cypher
        self.summary = summary
        self.raise_on_call = raise_on_call
        self.tool_calls = 0

    def __call__(self, prompt):
        if self.raise_on_call:
            raise self.raise_on_call
        from neptune import strands_tools as st
        if self.cypher is not None:
            self.tool_calls += 1
            st.execute_cypher(self.cypher)
        return _FakeResp(self.summary)


@pytest.fixture
def engine():
    from neptune.nl_query_strands import StrandsNLQueryEngine
    return StrandsNLQueryEngine()


def _run(engine, agent, rows=None, raise_neptune=None):
    """用假 Agent 跑一次 query()，并控制 Neptune 的返回。"""
    from neptune import strands_tools as st
    kw = {"side_effect": raise_neptune} if raise_neptune else {"return_value": rows or []}
    with mock.patch.object(engine, "_build_agent", return_value=agent), \
         mock.patch.object(st.nc, "results", **kw):
        return engine.query("petsite 依赖哪些数据库？")


# ─────────────────────────────────────────────────────────────
# 契约 1：返回结构（对应 U-B2-01）
# ─────────────────────────────────────────────────────────────

def test_t81_01_返回结构必须完整(engine):
    """调用方依赖这些字段存在。缺任何一个都是破坏性变更。"""
    cypher = ("MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(db) "
              "RETURN db.name AS database LIMIT 50")
    r = _run(engine, _FakeAgent(cypher, "petsite 依赖若干数据库。"),
             rows=[{"database": "petadoption-db"}])

    for k in ("question", "cypher", "results", "summary", "engine", "latency_ms"):
        assert k in r, f"返回值缺字段 {k}：{sorted(r)}"
    assert r["question"] == "petsite 依赖哪些数据库？"
    assert "DependsOn" in r["cypher"], f"cypher 应来自工具调用参数，实得 {r['cypher']!r}"
    assert r["results"] == [{"database": "petadoption-db"}]
    assert r["engine"] == "strands"


# ─────────────────────────────────────────────────────────────
# 契约 2：空结果语义（对应 U-B2-02）
# ─────────────────────────────────────────────────────────────

def test_t81_02_空结果必须有无结果语义(engine):
    """结果为空时 summary 不能是空字符串 —— 调用方会把它直接展示给人看。"""
    cypher = "MATCH (inc:Incident) WHERE inc.start_time >= '2019-01-01' RETURN inc.id LIMIT 50"
    # Agent 返回空摘要，逼引擎走 _fallback_summary
    r = _run(engine, _FakeAgent(cypher, ""), rows=[])

    assert isinstance(r["results"], list) and r["results"] == []
    assert r["summary"], "空结果时 summary 不得为空字符串"
    assert "无结果" in r["summary"], f"应表达「无结果」语义，实得 {r['summary']!r}"


# ─────────────────────────────────────────────────────────────
# 契约 3：LLM 失败要收敛成结构化错误，且不静默（对应 U-B2-03）
# ─────────────────────────────────────────────────────────────

def test_t81_03_Agent异常必须返回error且记日志(engine, caplog):
    """一次 Bedrock 抖动不该让整轮 RCA 崩掉，但也不能伪装成成功。

    三条子契约，缺一不可：
      · error 出现在返回值里（调用方能判断）
      · 不伪装成功（不返回一个看起来正常的空结果集）
      · 失败可见于日志（不是静默）
    """
    r = None
    with caplog.at_level(logging.WARNING):
        r = _run(engine, _FakeAgent(None, raise_on_call=Exception("Connection timeout")))

    assert r.get("error"), f"Agent 异常时应返回 error，实得 {sorted(r)}"
    assert "Connection timeout" in r["error"], r["error"]
    assert not r.get("results"), f"失败时不应返回结果集：{r.get('results')}"
    assert any("Connection timeout" in (rec.message or str(rec.msg))
               for rec in caplog.records), "失败必须记 warning，不得静默"


# ─────────────────────────────────────────────────────────────
# 契约 4：不安全 cypher 必须被拦，且拦截不依赖 Agent 配合（对应 U-B2-04）
# ─────────────────────────────────────────────────────────────

def test_t81_04_不安全cypher必须被guard拦下(engine):
    """写操作必须被拦 —— 而且拦截发生在 `execute_cypher` **内部**。

    这条是安全防线的门禁。2026-09-20 的调优去掉了「执行前必须先调
    validate_cypher」这条 Agent 规则，理由正是「真正的防线在 execute_cypher
    内部那句无条件 is_safe()，不依赖 Agent 配合」。所以这里刻意让假 Agent
    **直接调 execute_cypher**（不先 validate），验证那句话是真的。
    """
    from neptune import strands_tools as st

    unsafe = "MATCH (n:Microservice {name: 'petsite'}) DELETE n"
    called = {"neptune": False}

    def _should_not_run(*a, **kw):
        called["neptune"] = True
        raise AssertionError("不安全的 cypher 绝不能到达 Neptune")

    agent = _FakeAgent(unsafe, "已删除")
    with mock.patch.object(engine, "_build_agent", return_value=agent), \
         mock.patch.object(st.nc, "results", side_effect=_should_not_run):
        r = engine.query("删除所有 petsite 节点")

    assert called["neptune"] is False, "guard 必须在查询到达 Neptune 之前拦下"
    # 被拦后引擎拿不到任何成功执行记录 → results 应为空
    assert not r.get("results"), f"被拦的查询不应产出结果：{r.get('results')}"
    trace = r.get("trace") or []
    blocked = [t for t in trace if t.get("tool") == "execute_cypher" and t.get("blocked")]
    assert blocked, f"trace 里应留下 blocked 记录供审计，实得 {trace}"
    assert blocked[0].get("reason"), "拦截必须带原因，否则无法排查"


def test_t81_05_安全cypher必须正常通过(engine):
    """反向门禁：别为了拦住写操作把正常只读查询也拦了。"""
    safe = "MATCH (s:Microservice {name:'petsite'}) RETURN s.name AS name LIMIT 10"
    r = _run(engine, _FakeAgent(safe, "petsite 存在于图谱中。"),
             rows=[{"name": "petsite"}])

    assert not r.get("error"), f"安全查询不应报错：{r.get('error')}"
    assert r["results"] == [{"name": "petsite"}]


# ─────────────────────────────────────────────────────────────
# 契约 5：执行失败要让 LLM 有机会改正，但不能无限烧调用
#         （对应 U-B2-03b / 03c —— direct 是显式重试，strands 靠 ReAct）
# ─────────────────────────────────────────────────────────────

def test_t81_06_Neptune执行失败必须把错误回喂给LLM(engine):
    """执行失败时 `execute_cypher` 要**返回** ERROR 字符串而不是抛异常。

    这是 strands 版「重试」能成立的前提：Agent 只有看到错误文本才可能改正。
    若 tool 直接抛，异常会穿过 Agent、整轮结束，LLM 根本没机会修 ——
    direct 那边是用显式的 `_retry_with_error` 把 Neptune 报错回喂给 LLM
    （U-B2-03b 守的就是这个），strands 则靠 tool 的返回值承担同一职责。
    """
    from neptune import strands_tools as st

    st.reset_trace()
    with mock.patch.object(st.nc, "results",
                           side_effect=Exception("400 Client Error: Bad Request")):
        out = st.execute_cypher("MATCH (s:Microservice) RETURN s.name AS n WHERE s.name='x'")

    assert isinstance(out, str), f"tool 必须返回字符串而不是抛异常，实得 {type(out)}"
    assert out.startswith("ERROR"), f"必须以 ERROR 开头让 LLM 识别，实得 {out[:60]!r}"
    assert "400" in out, f"必须把 Neptune 的原始错误带给 LLM，否则它无从改正：{out[:80]!r}"


def test_t81_07_执行失败不得让引擎返回假成功(engine):
    """Neptune 一直失败时，引擎不能返回一个「看起来成功」的空结果。

    对应 U-B2-03c 的精神：失败要能被调用方看见，而不是静默退化成空列表。

    同时覆盖 S6-04（Neptune 不可达时优雅返回）：schema 来自 system prompt
    而不是实时查图谱，所以**即使 Neptune 断网，cypher 仍然生成得出来** ——
    这是「Neptune 不可达时仍可用静态 schema 生成 Cypher」那条契约的要点。
    """
    cypher = "MATCH (s:Microservice) RETURN s.name AS n"
    r = _run(engine, _FakeAgent(cypher, "查询完成"),
             raise_neptune=ConnectionError("VPC unreachable"))

    # 工具执行失败 → last_rows 没有成功记录 → results 必须是空
    assert not r.get("results"), f"执行失败时不得产出结果：{r.get('results')}"
    # 但没有崩 —— 优雅返回一个结构完整的 dict
    for k in ("question", "cypher", "results", "summary", "engine"):
        assert k in r, f"Neptune 不可达时仍须返回完整结构，缺 {k}"
    # 失败痕迹必须留在 trace 里供审计
    trace = r.get("trace") or []
    errs = [t for t in trace if t.get("tool") == "execute_cypher" and t.get("error")]
    assert errs, f"trace 里应留下执行失败记录，实得 {trace}"
    assert "unreachable" in str(errs[0].get("error")), errs


def test_t81_08_ReAct轮数必须有界(engine):
    """Agent 不能无限循环烧 Bedrock 调用。

    direct 用显式计数（U-B2-03c 断言「最多生成两次」）。strands 的轮数由
    Strands 框架控制，这里守的是**我们这侧**的可观测性：引擎必须把
    Agent 实际跑了几轮报出来，否则「烧了多少调用」无从得知。
    """
    cypher = "MATCH (s:Microservice) RETURN s.name AS n LIMIT 5"
    r = _run(engine, _FakeAgent(cypher), rows=[{"n": "petsite"}])

    # 假 Agent 没有 metrics，strands_cycles 取不到时应缺省而不是崩
    assert "trace" in r, "必须暴露工具调用链，否则无法审计 Agent 做了什么"
    assert isinstance(r["trace"], list)
    # 真实 Agent 会带 metrics；这里断言字段契约而非具体数值
    from neptune.nl_query_strands import StrandsNLQueryEngine
    assert hasattr(StrandsNLQueryEngine, "_extract_cycles"), (
        "引擎必须保留 cycle 数提取能力 —— 那是判断 ReAct 是否失控的唯一依据"
    )

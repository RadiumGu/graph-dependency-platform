"""从 LLM 输出提 JSON 数组：单一实现 + 失败必须可归因。

## 守的是什么（2026-10-11 golden 失败查出来的）

`learning` golden 套件的 `l004` 失败，信息只有一句：

    l004: got 0 recs, expected >= 1

而「0 条」覆盖三种完全不同的原因，处置方式也完全不同：

    ① LLM 调用失败（节流 / 超时）  → 重试或降并发，不是模型退化
    ② 输出了但解析不出来           → 解析缺陷，不是模型退化
    ③ LLM 真的认为没有建议         → **这才是模型行为退化**

golden 套件的全部价值在于发现 ③。cron 通知写着「这类失败意味着 LLM 行为
可能已退化 —— 别当成 flaky 直接重跑了事」，而当三种原因长得一样时，
这句提示反而会把人导向错误结论。

实测当时：整套跑两次都 0 条，单跑和按序跑前四个都正常产出 5 条、
内容质量正常（与 `observed_rec_count_range: [5, 5]` 一致）。
**因为原始输出被丢弃，无法判断到底是哪一种。** 一次要 6.7 分钟复现、
复现了还查不出真因的失败，等于没有判据。

## 修掉的脆弱点

原退化路径 `text.find("[") .. text.rfind("]")`，在 fence 丢失且文本里有
别的 `[` 时会截出垃圾：

    输入   覆盖域 [network] 与 [data] 缺失。
           [{"priority":1,"category":"coverage"}]
    返回   []        ← 当成「LLM 没给建议」

这不是假想输入：learning agent 实跑产出的建议描述里充满 `[compute]` /
`[network]` / `[data]` 这类方括号引用。

## 单一实现

`learning_strands.py` 与 `hypothesis_strands.py` 原本各有一份**逐字相同**
的实现，两份一样脆弱。本仓对这类重复已有定论 ——
`tests/test_97_cfn_loader_single_source.py` 的报错说：

    6 份重复里曾有 3 份是错的，重复本身就是那些错误能存在的原因。
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "chaos" / "code" / "agents"
SHARED = AGENTS / "llm_json.py"


def _load_shared():
    """直接按文件加载 —— `agents/__init__.py` 会 import 整条 agent 链，
    而那需要 profile / engines 等运行时依赖，离线 CI 里拿不到。"""
    spec = importlib.util.spec_from_file_location("_llm_json_under_test", SHARED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ex():
    return _load_shared().extract_json_array


class TestExtractionHandlesRealWorldShapes:
    """九种实测形态。第 ⑤ 种是原实现会返回 0 条的那个。"""

    @pytest.mark.parametrize("name,text", [
        ("正常 fence", '```json\n[{"priority":1}]\n```'),
        ("fence 后无换行", '```json[{"priority":1}]```'),
        ("大写 JSON 标记", '```JSON\n[{"priority":1}]\n```'),
        ("描述含方括号 + fence 完好",
         '分析显示 [compute] 域未覆盖。\n```json\n[{"priority":1}]\n```'),
        ("fence 丢失 + 描述含方括号",
         '覆盖域 [network] 与 [data] 缺失。\n[{"priority":1}]'),
        ("无 fence 纯数组", '[{"priority":1}]'),
        ("方括号在数组之后",
         '```json\n[{"priority":1}]\n```\n备注：参见 [runbook]。'),
        ("元素字符串含方括号",
         '[{"title":"补全 [compute] 域覆盖","priority":1}]'),
    ])
    def test_parses(self, ex, name: str, text: str):
        data, diag = ex(text)
        assert diag is None, f"{name}: 解析失败 —— {diag}"
        assert len(data) == 1, f"{name}: 得到 {len(data)} 条"

    def test_fence_missing_with_brackets_in_prose(self, ex):
        """单独钉住第 ⑤ 种 —— 它是这次修复的直接动因。

        原实现：find("[") 命中 [network] 的 `[`，rfind("]") 命中数组尾，
        拼出的片段 json.loads 失败，返回 []。
        """
        data, diag = ex(
            '覆盖域 [network] 与 [data] 缺失。\n'
            '[{"priority":1,"category":"coverage"}]'
        )
        assert diag is None
        assert data == [{"priority": 1, "category": "coverage"}]

    def test_genuinely_empty_array_is_success_not_failure(self, ex):
        """LLM 说「没有建议」是一个**结论**，不是解析失败。

        把它判成失败会让 golden 永远无法区分「模型认为无需改进」和
        「解析挂了」—— 而那正是这次要修的东西。
        """
        data, diag = ex('```json\n[]\n```')
        assert diag is None, "空数组被误判成解析失败"
        assert data == []


class TestFailureMustCarryDiagnostic:
    """解析失败必须带回原始文本，否则下次失败还是查不出真因。"""

    def test_empty_text_says_so(self, ex):
        data, diag = ex("")
        assert data == []
        assert diag and ("空" in diag), f"诊断没说清是空响应: {diag}"

    def test_unparseable_text_is_echoed(self, ex):
        prose = "我认为当前覆盖已经足够，无需补充实验。"
        data, diag = ex(prose)
        assert data == []
        assert diag, "解析失败却没有诊断 —— 这正是原实现的盲区"
        assert prose[:10] in diag, (
            f"诊断里没有原始文本，无法判断 LLM 到底输出了什么: {diag}"
        )

    def test_diagnostic_reports_text_length(self, ex):
        _data, diag = ex("没有数组的一段话" * 20)
        assert diag and "字符" in diag, f"诊断没报全文长度: {diag}"

    def test_diagnostic_is_bounded(self, ex):
        """诊断不能把整个响应倒进日志 —— LLM 输出可以上万字符。"""
        _data, diag = ex("x" * 50000)
        assert diag is not None
        assert len(diag) < 1500, f"诊断长度 {len(diag)}，会把日志刷爆"


class TestSingleImplementation:
    """两处调用方不得各自再维护一份。"""

    def test_shared_module_exists(self):
        assert SHARED.is_file(), f"{SHARED.relative_to(ROOT)} 不存在"

    @pytest.mark.parametrize("rel", [
        "learning_strands.py",
        "hypothesis_strands.py",
    ])
    def test_no_local_json_block_regex(self, rel: str):
        """不得再出现本地的 ```json 正则。

        2026-10-11 之前这两个文件各有一份逐字相同的
        `_JSON_BLOCK_RE` + `_extract_json_array`，两份一样脆弱。
        """
        src = (AGENTS / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_JSON_BLOCK_RE" for t in node.targets
            )):
                continue
            pytest.fail(
                f"{rel} 又自己定义了 _JSON_BLOCK_RE —— "
                f"改用 `from agents.llm_json import extract_json_array`。"
                f"重复实现是那些错误能长期存在的原因（见 test_97 的同类判据）。"
            )

    @pytest.mark.parametrize("rel", [
        "learning_strands.py",
        "hypothesis_strands.py",
    ])
    def test_imports_shared(self, rel: str):
        src = (AGENTS / rel).read_text(encoding="utf-8")
        assert "llm_json" in src, f"{rel} 没有引用共享实现"


class TestCallerPropagatesDiagnostic:
    """learning agent 必须把诊断带进返回值，golden 测试必须把它报出来。

    这一组守的是「算了但没人读」—— 本仓反复出现的形态：
    诊断信息算出来了却没进返回值，等于没算。
    """

    def test_generate_recommendations_returns_parse_error(self):
        src = (AGENTS / "learning_strands.py").read_text(encoding="utf-8")
        assert '"parse_error": parse_diag' in src, (
            "generate_recommendations 没把 parse_error 放进返回值 —— "
            "解析失败会继续伪装成「LLM 没给建议」"
        )
        assert '"raw_text_len"' in src, (
            "没有带回原始文本长度 —— 0 字符和 8000 字符是两种完全不同的失败"
        )

    def test_golden_test_distinguishes_three_causes(self):
        src = (ROOT / "tests" / "test_learning_golden.py").read_text(encoding="utf-8")
        assert "parse_error" in src, (
            "golden 测试没读 parse_error，失败信息仍是一句 got 0 recs"
        )
        assert "模型行为本身的变化" in src, (
            "没有把「真退化」与另两种原因区分开 —— "
            "golden 套件的全部价值在于发现真退化"
        )

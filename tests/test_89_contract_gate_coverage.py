"""test_89 —— 契约门禁的覆盖面本身必须被守住。

契约文件开头写着「合法的 source 取值词表，**由 graph_contract.assert_source()
在写入时强制**」。这句话此前对 `etl_deepflow` 不成立：它是代码量最大、写边最多
的写入方，而 `assert_*` 调用数为 0 —— 只 lazy import 了 `dependency_edge_labels`
（drift 对账用的边类型清单，不是门禁）。

## 为什么需要这个文件

「哪些 ETL 有门禁」此前只存在于**代码注释**里，而两处注释互相矛盾且都过期：

    etl_agentcore 说「etl_xray 与共享层完全没接门禁」
        → etl_xray 已于 2026-09-04 接上，那半是历史状态
    etl_xray 说「etl_deepflow（1）都接了门禁」
        → 实测 assert_* 调用 0 次，它把 dependency_edge_labels 当成了门禁

判断覆盖面只能**数 assert_* 的调用**，不能读注释。本文件把这件事变成断言，
这样下一个新增的写入方 ETL 忘了接门禁会当场变红，而不是等某次审计翻出来。
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED = ROOT / "infra" / "lambda" / "shared" / "python"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

LAMBDA_DIR = ROOT / "infra" / "lambda"

# 写图的 ETL → 入口文件。etl_trigger 刻意不在此列：它只把 EventBridge 事件
# 转发到 SQS 再触发 etl_aws，自己不写任何节点或边，没有可校验的标签。
WRITER_ETLS = {
    "etl_agentcore": ["etl_agentcore/neptune_etl_agentcore.py"],
    "etl_deepflow": ["etl_deepflow/neptune_etl_deepflow.py"],
    "etl_xray": ["etl_xray/neptune_etl_xray.py"],
    "etl_cfn": ["etl_cfn/neptune_etl_cfn.py"],
    "etl_appsignals": ["etl_appsignals/neptune_etl_appsignals.py"],
    "etl_aws": ["etl_aws/handler.py", "etl_aws/neptune_client.py"],
}

_GATE_CALL = re.compile(
    r"^\s*(assert_node_type|assert_edge_type|assert_source)\(", re.M)


def _gate_calls(rel_paths) -> int:
    """数实际的门禁调用，排除注释与 import 行。"""
    total = 0
    for rel in rel_paths:
        p = LAMBDA_DIR / rel
        if p.exists():
            total += len(_GATE_CALL.findall(p.read_text(encoding="utf-8")))
    return total


@pytest.mark.parametrize("etl", sorted(WRITER_ETLS))
def test_t89_01_每个写图的ETL都必须有门禁调用(etl):
    n = _gate_calls(WRITER_ETLS[etl])
    assert n > 0, (
        f"{etl} 的 assert_node_type / assert_edge_type / assert_source 调用数为 0。"
        f"契约声称「写入时强制」，对这条路径就不成立。"
        f"注意：import dependency_edge_labels 不是门禁"
    )


def test_t89_02_etl_deepflow的门禁覆盖全部写入点():
    """它有 3 类节点写入点 + 4 处边写入点，每处都要有对应的门禁。"""
    src = (LAMBDA_DIR / "etl_deepflow/neptune_etl_deepflow.py").read_text(encoding="utf-8")

    # 边：addE 的每个标签都要出现在 assert_edge_type 里
    added_edges = set(re.findall(r"addE\('([A-Za-z]+)'", src))
    gated_edges = set(re.findall(r"assert_edge_type\('([A-Za-z]+)'", src))
    assert added_edges, "找不到 addE —— 文件结构变了，请更新本测试"
    missing = added_edges - gated_edges
    assert not missing, f"这些边类型有写入但没有 assert_edge_type: {sorted(missing)}"

    # 节点：mergeV 的每个标签都要出现在 assert_node_type 里
    merged_nodes = set(re.findall(r"mergeV\(\[\(T\.label\)\s*:\s*'([A-Za-z]+)'", src))
    gated_nodes = set(re.findall(r"assert_node_type\('([A-Za-z]+)'", src))
    assert merged_nodes, "找不到 mergeV —— 文件结构变了，请更新本测试"
    missing_n = merged_nodes - gated_nodes
    assert not missing_n, f"这些节点类型有写入但没有 assert_node_type: {sorted(missing_n)}"


def test_t89_03_etl_deepflow写入的标签与source全部已声明():
    """静态核对：它写的东西必须都在契约里，否则 enforce 模式一上线就拦生产写入。

    这是把门禁从 warn 直接开成 enforce 的前提 —— 先离线确认现有写入全合规。
    """
    from graph_contract_data import EDGE_TYPES, NODE_TYPES, SOURCES

    src = (LAMBDA_DIR / "etl_deepflow/neptune_etl_deepflow.py").read_text(encoding="utf-8")

    for lb in sorted(set(re.findall(r"addE\('([A-Za-z]+)'", src))):
        assert lb in EDGE_TYPES, f"写入未声明的边类型 {lb!r}"
    for lb in sorted(set(re.findall(r"mergeV\(\[\(T\.label\)\s*:\s*'([A-Za-z]+)'", src))):
        assert lb in NODE_TYPES, f"写入未声明的节点类型 {lb!r}"

    # source 的静态枚举有个固有限制：`__.constant('...')` 既用于 source 也用于
    # dependency_kind 等其它属性，正则分不开（第一版就把 'dynamic' 当成了 source）。
    # 所以这里只核对**字面量 source**，加上实测确认过的动态取值清单。
    # 真正的保证是运行时门禁（t89_04）—— 任何未声明的 source 会在写入时被拦，
    # 静态测试只是让问题更早暴露，不承担完备性。
    literals = set(re.findall(r"\.property\('source'\s*,\s*'([a-z0-9-]+)'", src))
    # 动态取值：edge_source = 'deepflow-dns' if has_dns else 'xray'（已实测）
    dynamic_known = {"deepflow-dns", "xray"}
    for s in sorted(literals | dynamic_known | {"deepflow-etl", "deepflow-l4"}):
        assert s in SOURCES, (
            f"source={s!r} 未在契约 sources 词表内。"
            f"历史上出现过 'deepflow' 这个同义漂移值（活图谱实测 8 个节点）"
        )


def test_t89_04_门禁在enforce模式下真的会拦():
    """门禁存在不等于门禁生效。默认模式必须是 enforce，且违约要抛。"""
    from graph_contract import (
        GraphContractError,
        assert_edge_type,
        assert_node_type,
        assert_source,
    )

    prev = os.environ.get("GRAPH_CONTRACT_MODE")
    os.environ.pop("GRAPH_CONTRACT_MODE", None)   # 走默认值
    try:
        with pytest.raises(GraphContractError):
            assert_node_type("NotARealNodeType")
        with pytest.raises(GraphContractError):
            assert_source("deepflow", "旧的同义漂移值必须被拦")
        with pytest.raises(GraphContractError):
            # Calls 的 pairs 只声明 Microservice→Microservice
            assert_edge_type("Calls", "Microservice", "ECRRepository")

        # 合法的必须放行，否则门禁会拦掉生产写入
        assert_node_type("Microservice")
        assert_node_type("TopologyChange")
        assert_source("deepflow-l4", "ok")
        assert_edge_type("Calls", "Microservice", "Microservice")
    finally:
        if prev is None:
            os.environ.pop("GRAPH_CONTRACT_MODE", None)
        else:
            os.environ["GRAPH_CONTRACT_MODE"] = prev


def test_t89_05_共享层不加门禁是设计而非缺口():
    """澄清一个我自己判断错过的点。

    `neptune_client_base.neptune_query(g)` 收的是**已经拼好的 Gremlin 字符串**，
    它无法知道这次写的是哪个 label、哪个 source —— 在这一层加门禁需要解析
    Gremlin，那是把校验点放错位置。

    强制点应该在**构造 Gremlin 的地方**（各 ETL），那里才知道语义。所以共享层
    `assert_* == 0` 是合理的，不是覆盖缺口。本测试固化这个判断，免得下次又有人
    （包括我）把它当成待修项。
    """
    base = SHARED / "neptune_client_base.py"
    assert base.exists()
    src = base.read_text(encoding="utf-8")
    assert len(_GATE_CALL.findall(src)) == 0, (
        "共享层出现了门禁调用。若确实要在传输层校验，请先说明如何从 Gremlin "
        "字符串可靠地还原 label 与 source —— 否则校验点应留在各 ETL"
    )
    assert "def neptune_query" in src, "共享层结构变了，请复核本测试的前提"

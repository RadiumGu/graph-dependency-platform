"""tests/test_75_no_phantom_gateway_targets.py — 网关 target 不得被建成 AgentTool。

## 防的是什么

Gateway 的 target 分两类，**目标节点类型不同**：

    targetType = AGENTCORE_RUNTIME          → 另一端是已建模的 AgentRuntime
    mcp.lambda / openApiSchema / smithyModel → 那是真的 tool 端点

`etl_agentcore` 曾一律建 `AgentTool` + `AgentGateway -[RoutesTo]-> AgentTool`，
于是图里多出 5 个不存在的「工具」（orchestrator / concierge / nutrition /
ordering / adoption）—— 它们其实是已经建模过的 AgentRuntime。
代码在 `2be2048` 改成建 `RoutesToRuntime -> AgentRuntime`。

## 为什么这条门禁必须存在

那份代码改动**提交后 8 天都没有真正生效**，而且**没有任何测试变红**：

部署侧的 botocore 太旧，不认识 `bedrock-agentcore-control` 的
`TargetConfiguration` tagged union 的 `http` 成员，**把它从解析结果里静默剥掉**
（日志只有一条 INFO：`Received a tagged union response with member unknown to
client: http`）。于是 `_runtime_arn_from_target` 拿不到 arn、返回 None，
一直走老分支建 AgentTool。`GetGatewayTarget` 调用本身是成功的，
所以连 `_paged_targets` 里那条降级警告都没触发。

**这是一个「代码对、依赖环境不对、且失败静默」的组合。** 单元测试测不到它 ——
只有对活图谱断言才能抓到。2026-09-15 加挂 `botocore-current:1` 层后修复生效，
残留由 `scripts/purge_phantom_gateway_targets.py` 清除。

## 判据刻意用 tool_key 而不是 name

`AgentTool` 的身份键是 `tool_key`，形如 `{owner_arn}#{tool_name}`：

    幽灵（网关 target）   ...:gateway/waggleaigateway-th4m2rp46p#adoption
    真工具（runtime 注册）...:runtime/WaggleAIOrchestrator-K85tG867Xt#adoption

**两者 name 相同**（都叫 `adoption`）。按 name 判会把真工具一起判进去 ——
而真工具上挂着 `WaggleAIOrchestrator -[InvokesTool]-> adoption`。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _skip_if_offline():
    if os.environ.get("GDP_OFFLINE"):
        pytest.skip("GDP_OFFLINE：本用例需要真实 Neptune")


# ── t75_01：不得存在 AgentGateway -[RoutesTo]-> AgentTool ─────────────────────
@pytest.mark.neptune
def test_t75_01_no_gateway_routes_to_agenttool():
    _skip_if_offline()
    from neptune import neptune_client as neptune_rca

    rows = neptune_rca.results(
        "MATCH (g:AgentGateway)-[e:RoutesTo]->(t:AgentTool) "
        "RETURN t.name AS name, t.tool_key AS tool_key, e.source AS source")
    assert not rows, (
        "网关 target 又被建成 AgentTool 了：\n  "
        + "\n  ".join(f"{r.get('name')}  key={r.get('tool_key')}  "
                      f"src={r.get('source')}" for r in rows)
        + "\n\n这些 target 的 targetType 是 AGENTCORE_RUNTIME，另一端是已建模的"
          " AgentRuntime，正确的边是 RoutesToRuntime。"
          "\n最可能的原因：部署侧 botocore 太旧，把 targetConfiguration 这个"
          " tagged union 的 `http` 成员静默剥掉了 —— 查 ETL 日志里有没有"
          " 'Received a tagged union response with member unknown to client: http'。"
          "\n若有，检查 neptune-etl-from-agentcore 是否仍挂着 botocore-current 层。"
          "\n**不要**为了让本条变绿而去删这条断言：它抓的是一个静默失败。")


# ── t75_02：RoutesToRuntime 必须覆盖控制面声明的全部 runtime target ────────────
#
# 这一条是 t75_01 的正面对应。只断言「没有幽灵」不够 —— botocore 再退回旧版时，
# 幽灵不会立刻重现（老边已被清），但 RoutesToRuntime 会**停止刷新**。
@pytest.mark.neptune
def test_t75_02_routes_to_runtime_covers_gateway_targets():
    _skip_if_offline()
    from neptune import neptune_client as neptune_rca

    rows = neptune_rca.results(
        "MATCH (g:AgentGateway)-[e:RoutesToRuntime]->(rt:AgentRuntime) "
        "RETURN rt.name AS runtime, e.target_name AS target_name, "
        "e.target_type AS target_type, e.dependency_kind AS kind")
    assert rows, (
        "一条 RoutesToRuntime 都没有。它由控制面派生"
        "（ListGatewayTargets + GetGatewayTarget），**与流量和采样无关**，"
        "所以「没有流量」不是它缺失的正当理由。"
        "\n最可能的原因同 t75_01：botocore 剥掉了 targetConfiguration.http。")

    bad_type = [r for r in rows
                if (r.get("target_type") or "").upper() != "AGENTCORE_RUNTIME"]
    assert not bad_type, (
        f"这些 RoutesToRuntime 的 target_type 不是 AGENTCORE_RUNTIME: {bad_type}。"
        " 该边只应用于指向 runtime 的 target；其他类型的 target 是真 tool 端点。")

    # dependency_kind 必须是 static：控制面声明，与流量无关。
    # 若写成 dynamic，它会落入 deactivate_stale_dynamic_edges 的失效管辖，
    # 于是「6 小时没流量」会把一条**配置事实**误判为失效。
    bad_kind = [r for r in rows if r.get("kind") != "static"]
    assert not bad_kind, (
        f"这些 RoutesToRuntime 的 dependency_kind 不是 static: {bad_kind}。"
        " 它是控制面声明的路由，与流量无关；写成 dynamic 会让「6 小时没流量」"
        "把一条配置事实误判为失效。")


# ── t75_03：清理脚本必须还在，且判据没被放宽 ──────────────────────────────────
def test_t75_03_purge_script_intact():
    p = _ROOT / "scripts" / "purge_phantom_gateway_targets.py"
    assert p.is_file(), f"找不到清理脚本 {p}"
    src = p.read_text(encoding="utf-8")

    # 判据必须是完整的 ARN 片段，不能退化成 'gateway' 一个词 ——
    # 后者会匹配到任何名字里带 gateway 的真工具。
    assert "':gateway/'" in src or '":gateway/"' in src, (
        "清理脚本的 tool_key 判据不再是 ':gateway/'。"
        " 退化成 'gateway' 一个词会误伤名字里带 gateway 的真工具。")

    # 必须保留 dry-run 与备份 —— 这是个删数据的脚本。
    for need, why in (("--apply", "缺 --apply 开关：删数据的脚本不能默认就删"),
                      ("backup", "缺备份：删之前必须把属性落盘")):
        assert need in src, f"{why}（{p.name}）"

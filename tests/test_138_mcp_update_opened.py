"""test_138 — temporal-mcp 放开 update_workflow（2026-10-20，负责人决定）后，其余写工具仍禁用。

放开的前提是 Update 都有 validator（不合规当场拒绝、不进 history）。signal 没有 validator，
cancel / terminate 会绕过「被取消也导出记录」以外的一切审计，所以这些必须仍在拒绝列表里。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cfn_yaml import load_cfn  # noqa: E402

RT = ROOT / "infra" / "dr-korea" / "06-agentcore-runtime.yaml"
STILL_DENIED = {"start_workflow", "signal_workflow", "signal_with_start_workflow", "cancel_workflow",
                "terminate_workflow", "pause_workflow", "unpause_workflow", "create_schedule", "delete_schedule"}


def _deny() -> set[str]:
    return set(load_cfn(RT)["Parameters"]["TemporalDenyTools"]["Default"].split(","))


def test_update_is_allowed():
    assert "update_workflow" not in _deny()


def test_everything_else_stays_denied():
    assert STILL_DENIED <= _deny(), STILL_DENIED - _deny()


def test_the_tradeoff_is_written_next_to_the_switch():
    src = RT.read_text(encoding="utf-8")
    assert "署名是调用方自报的" in src and "回滚" in src


def test_every_update_handler_has_a_validator():
    """放开 update 的前提：每个 @workflow.update 都有对应的 validator。"""
    import re
    for f in ("plan_workflow.py", "runbook_workflow.py"):
        src = (ROOT / "dr-plan-generator" / "worker" / f).read_text(encoding="utf-8")
        handlers = re.findall(r"@workflow\.update\(name=\"(\w+)\"\)\s+def (\w+)", src)
        for _, fn in handlers:
            assert f"@{fn}.validator" in src, f"{f}:{fn} 没有 validator —— agent 经 MCP 可以不受校验地推进"

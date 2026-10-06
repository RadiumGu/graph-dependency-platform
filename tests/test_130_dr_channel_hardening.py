"""test_130_dr_channel_hardening.py — agent 通道只读、live 走评审链、CI 不许静默 skip、退役模块清理。

纯文本/AST 断言，离线 CI 里跑。行为由 test_129 在真实 Temporal 上守。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cfn_yaml import load_cfn  # noqa: E402
from zh_text import assert_contains  # noqa: E402

RT = ROOT / "infra" / "dr-korea" / "06-agentcore-runtime.yaml"
RW = ROOT / "dr-plan-generator" / "worker" / "runbook_workflow.py"
PROV = ROOT / "dr-plan-generator" / "worker" / "provision-worker.sh"
DEPLOY = ROOT / "infra" / "dr-korea" / "temporal-1.32" / "deploy-worker.sh"
CI = ROOT / ".github" / "workflows" / "migration-checks.yml"
REQ = ROOT / "requirements-dev.txt"
T129 = ROOT / "tests" / "test_129_dr_runbook_workflow.py"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"

#: agent 通道上必须禁掉的：能推进、打断或伪造人工动作的全部写工具。
MUST_DENY = {
    "start_workflow", "signal_workflow", "signal_with_start_workflow", "update_workflow",
    "cancel_workflow", "terminate_workflow", "pause_workflow", "unpause_workflow",
    "create_schedule", "delete_schedule",
}


@pytest.fixture(scope="module")
def section() -> str:
    t = RECORD.read_text(encoding="utf-8")
    i = t.index("### 4.54")
    return t[i : t.index("## 六、待记录", i)]


class TestMcpChannelIsReadOnly:
    def test_deny_list_covers_every_mutating_tool(self):
        d = load_cfn(RT)
        denied = set(d["Parameters"]["TemporalDenyTools"]["Default"].split(","))
        assert MUST_DENY <= denied, f"漏禁：{sorted(MUST_DENY - denied)}"

    def test_deny_list_has_no_whitespace(self):
        v = load_cfn(RT)["Parameters"]["TemporalDenyTools"]["Default"]
        assert " " not in v and "\n" not in v

    def test_deny_list_is_wired_into_the_runtime(self):
        env = load_cfn(RT)["Resources"]["TemporalMcpRuntime"]["Properties"]["EnvironmentVariables"]
        assert "TEMPORAL_DENY_TOOLS" in env

    def test_template_warns_env_alone_is_a_fake_gate(self):
        assert "只配环境变量就是假闸门" in RT.read_text(encoding="utf-8")

    def test_why_update_is_denied_too(self, section: str):
        assert_contains(section, "agent 可以替「alice」confirm_step")


class TestLiveRequiresTheReviewChain:
    def test_parent_check_exists(self):
        src = RW.read_text(encoding="utf-8")
        # 锚定**代码语句**而不是名字 —— 名字在注释里也出现，只删代码时
        # 按名字断言仍会通过（2026-10-06 反向验证时实测未挂靶）。
        assert "            parent = workflow.info().parent\n" in src
        assert "if parent is None or" in src
        assert "只能由 DrPlanWorkflow" in src

    def test_parent_is_server_recorded_not_self_reported(self):
        assert "调用方伪造不了" in RW.read_text(encoding="utf-8")

    def test_behaviour_test_exists_and_cannot_hang(self):
        src = T129.read_text(encoding="utf-8")
        i = src.index("async def test_live_without_parent_is_refused")
        seg = src[i : i + 1600]
        assert "human_wait_hours=0.001" in seg, "校验失效时用例必须快速失败，不能卡 24 小时"


class TestCiCannotSilentlySkipTheBehaviourTest:
    def test_ci_requires_temporal(self):
        assert 'DR_REQUIRE_TEMPORAL: "1"' in CI.read_text(encoding="utf-8")

    def test_test_file_honours_the_flag(self):
        src = T129.read_text(encoding="utf-8")
        assert 'os.environ.get("DR_REQUIRE_TEMPORAL") == "1"' in src

    def test_sdk_pinned_to_the_worker_version(self):
        dev = REQ.read_text(encoding="utf-8")
        worker = (ROOT / "dr-plan-generator" / "worker" / "requirements.txt").read_text(encoding="utf-8")
        assert "temporalio==1.33.0" in dev and "temporalio==1.33.0" in worker

    def test_floor_is_not_left_far_behind(self):
        import re
        m = re.search(r'GDP_OFFLINE_MIN_PASSED: "(\d+)"', CI.read_text(encoding="utf-8"))
        assert m and int(m.group(1)) >= 1800, "下限落后实际通过数太多就挡不住大面积 skip"

    def test_parent_workflow_lives_in_a_sandbox_safe_module(self):
        assert (ROOT / "tests" / "_dr_live_parent.py").exists()
        assert "class TestLiveParent" not in T129.read_text(encoding="utf-8")


class TestRetiredModulesArePruned:
    def test_publish_writes_a_manifest(self):
        assert '> "$STAGE/MANIFEST"' in DEPLOY.read_text(encoding="utf-8")

    def test_publish_still_does_not_use_delete(self):
        """worker/ 前缀下有不归本流程管的对象，--delete 会把它们一起删掉。"""
        src = DEPLOY.read_text(encoding="utf-8")
        sync_lines = [l for l in src.splitlines() if l.strip().startswith("aws s3 sync")]
        assert sync_lines and not any("--delete" in l for l in sync_lines)

    def test_provision_prunes_only_with_a_manifest(self):
        src = PROV.read_text(encoding="utf-8")
        assert 'if [ -s "$MANIFEST" ]; then' in src
        assert "宁可留死文件也不猜" in src

    def test_prune_happens_before_the_fingerprint(self):
        src = PROV.read_text(encoding="utf-8")
        assert src.index('MANIFEST="$APP/MANIFEST"') < src.index("CODE_FP_AFTER=")


class TestRecordedFindings:
    def test_cancel_came_from_the_ui(self, section: str):
        assert_contains(section, "来自 **Temporal Web UI**")

    def test_mcp_deny_would_not_have_stopped_it(self, section: str):
        assert_contains(section, "**挡不住这一次**")

    def test_ui_switch_verified_server_side(self, section: str):
        assert_contains(section, "405 Method Not Allowed")

    def test_ui_decision_left_to_human(self, section: str):
        assert_contains(section, "**这个决定留给人**")

    def test_stale_package_root_cause(self, section: str):
        assert_contains(section, "**两天前没重新打包的旧 zip**")

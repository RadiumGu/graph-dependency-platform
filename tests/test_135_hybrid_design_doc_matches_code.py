"""test_135 — 混合方案设计文档里的关键结论必须与模板、代码一致。

设计文档最容易出的问题是「代码改了，文档还写着旧结论」。这里把文档里**可核验**的
断言逐条对到它们的事实来源上；文档或代码任何一边改了，这里就会变红，逼着两边一起改。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "dr-plan-generator" / "worker"))
from cfn_yaml import load_cfn  # noqa: E402

DOC = ROOT / "docs" / "runbooks" / "dr-arc-temporal-hybrid-design.md"
PLAN_T = ROOT / "infra" / "dr-korea" / "20-arc-region-switch-poc.yaml"
WORKER_T = ROOT / "infra" / "dr-korea" / "07-worker-permissions.yaml"
BRIDGE = ROOT / "dr-plan-generator" / "worker" / "arc_bridge.py"


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plan() -> dict:
    return load_cfn(PLAN_T)


def test_respects_the_owners_constraints(doc):
    assert "**CloudFront**" in doc and "**DNS 托管不变**" in doc
    assert "东京始终是主站点" in doc


def test_first_step_is_approval_as_documented(doc, plan):
    assert "每个工作流的第一步都是 ManualApproval" in doc
    for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]:
        assert wf["Steps"][0]["ExecutionBlockType"] == "ManualApproval"


def test_mfa_claim_matches_the_trust_policy(doc, plan):
    assert "`aws:MultiFactorAuthPresent=true`" in doc
    st = plan["Resources"]["ApproverRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert st["Condition"] == {"Bool": {"aws:MultiFactorAuthPresent": "true"}}


def test_named_role_and_plan_match_the_template(doc, plan):
    name = plan["Parameters"]["PlanName"]["Default"]
    assert f"`{name}`" in doc and f"`{name}-approver`" in doc


def test_worker_cannot_decide_as_documented(doc):
    assert "不能 Approve / Cancel / UpdatePlanExecution(Step)" in doc
    src = WORKER_T.read_text(encoding="utf-8")
    for forbidden in ("ApprovePlanExecutionStep", "CancelPlanExecution", "UpdatePlanExecutionStep"):
        assert f"arc-region-switch:{forbidden}\n" not in src.split("ArcStartAndWatchPocPlan", 1)[1].split("Sid:", 1)[0]


def test_ungraceful_skip_matches(doc, plan):
    assert "依赖停用区域的步骤在 ungraceful 模式下跳过" in doc
    for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]:
        for s in wf["Steps"]:
            cfg = s["ExecutionBlockConfiguration"].get("CustomActionLambdaConfig") or {}
            if cfg.get("RegionToRun") == "deactivatingRegion":
                assert cfg.get("Ungraceful") == {"Behavior": "skip"}


def test_verdicts_named_in_the_doc_exist_in_code(doc):
    src = BRIDGE.read_text(encoding="utf-8")
    for v in re.findall(r"`(arc-[a-z-]+)`", doc):
        if v.startswith(("arc-petsite", "arc-region", "arc-approve", "arc-poc")):
            continue
        assert f'"{v}"' in src, f"文档提到的结论 {v} 在 arc_bridge.py 里不存在"


def test_probe_count_matches_the_catalog(doc):
    from probe_catalog import PROBES
    assert f"{len(PROBES)} 类只读探针" in doc


def test_every_repo_path_in_the_doc_exists(doc):
    paths = set(re.findall(r"`((?:infra|dr-plan-generator|docs|tests)/[^`\s]+)`", doc))
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert not missing, missing


def test_known_gaps_are_not_silently_dropped(doc):
    """缺口没修之前，文档里不能把它删掉。"""
    for gap in ("**Temporal UI 入口**", "没有定期复检", "「agent 批不了」", "`RequireMfa`"):
        assert gap in doc, gap

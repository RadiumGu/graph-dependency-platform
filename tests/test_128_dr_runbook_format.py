"""test_128_dr_runbook_format.py — 步骤格式、探针目录、以及「Temporal 不执行任何变更」。

纯 Python，不需要 temporalio —— 在离线 CI 里也会跑。

## 守什么

1. **格式的每一条规则都会拒绝它该拒绝的东西。** 尤其是：没有后置探针的人工操作、
   不可逆却没写理由、可逆却没写回滚、拼错的参数名（会被静默忽略，探针就测了别的）。
2. **生成器不编事实。** 前门怎么切仓库里推导不出来，初稿必须标 needs_human_input，
   而带这个标记的计划不能被批准。
3. **探针目录与实现一一对应**，每个探针是一个独立的 activity 类型。
4. **整个 worker 目录里没有一个写类 AWS 调用**（只有写计划/记录的 S3 PutObject）。
   这取代了原来的 test_86「按步骤放行」—— 那道闸门守的是「演练不该有能力切换
   生产数据库」，现在这件事在结构上就不可能：代码里根本没有那条调用。
5. `reversible: false` 序列化后必须还在 —— 第一版把它当空值丢掉了。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
W = ROOT / "dr-plan-generator" / "worker"
if str(W) not in sys.path:
    sys.path.insert(0, str(W))

from probe_catalog import PROBES, check_params  # noqa: E402
from runbook import (  # noqa: E402
    FORMAT,
    RunbookError,
    approval_problems,
    extract_runbook,
    parse_runbook,
    render_block,
)
from runbook_draft import DraftFacts, draft_tokyo_to_seoul, render_body  # noqa: E402


def _minimal() -> dict:
    return {
        "format": FORMAT,
        "source_region": "ap-northeast-1",
        "target_region": "ap-northeast-2",
        "steps": [
            {"id": "pre", "kind": "check", "title": "前置",
             "probes": [{"probe": "aurora_global_membership"}]},
            {"id": "act", "kind": "manual", "title": "动作", "command": "echo hi",
             "reversible": True, "rollback": "echo undo",
             "post": [{"probe": "eks_ready_nodes", "params": {"min_ready": 2}}]},
        ],
    }


def _problems(raw: dict) -> list[str]:
    with pytest.raises(RunbookError) as e:
        parse_runbook(raw)
    return e.value.problems


def _facts(**kw) -> DraftFacts:
    base = dict(
        secondary_cluster_arn="arn:aws:rds:ap-northeast-2:1:cluster:sec",
        dynamodb_table="t",
        images_by_digest=[("petsite-hotfix", "sha256:" + "a" * 64)],
        images_by_tag=[("pet-adoptions-history", "latest")],
        lambdas=["dr-korea-petstatusupdater"],
        target_groups=["dr-korea-petsite-tg"],
        health_urls=["http://internal-x/health/status"],
        workload_manifests=["infra/dr-korea/petsite-korea-drill.yaml"],
    )
    base.update(kw)
    return DraftFacts(**base)


class TestFormatAcceptsTheMinimal:
    def test_minimal_parses(self):
        rb = parse_runbook(_minimal())
        assert [s.id for s in rb.steps] == ["pre", "act"]

    def test_round_trip_through_block(self):
        rb = parse_runbook(_minimal())
        again = extract_runbook("前言\n\n" + render_block(rb) + "\n")
        assert again is not None and [s.id for s in again.steps] == ["pre", "act"]


class TestFormatRejects:
    def test_manual_without_post_probe(self):
        r = _minimal(); del r["steps"][1]["post"]
        assert any("后置探针" in p for p in _problems(r))

    def test_manual_without_reversibility(self):
        r = _minimal(); del r["steps"][1]["reversible"]
        assert any("reversible" in p for p in _problems(r))

    def test_reversible_without_rollback(self):
        r = _minimal(); del r["steps"][1]["rollback"]
        assert any("rollback" in p for p in _problems(r))

    def test_irreversible_without_note(self):
        r = _minimal(); r["steps"][1]["reversible"] = False; del r["steps"][1]["rollback"]
        assert any("irreversible_note" in p for p in _problems(r))

    def test_irreversible_with_rollback(self):
        """不可逆却写了回滚 —— 那会让人以为能撤回。"""
        r = _minimal(); r["steps"][1]["reversible"] = False
        r["steps"][1]["irreversible_note"] = "因为…"
        assert any("不能写 rollback" in p for p in _problems(r))

    def test_unknown_probe(self):
        r = _minimal(); r["steps"][0]["probes"] = [{"probe": "aurora_magic"}]
        assert any("未知探针" in p for p in _problems(r))

    def test_misspelled_param_is_not_silently_ignored(self):
        r = _minimal(); r["steps"][1]["post"][0]["params"] = {"min_ready": 2, "min_raedy": 3}
        assert any("不接受参数 'min_raedy'" in p for p in _problems(r))

    def test_missing_required_param(self):
        r = _minimal(); r["steps"][1]["post"][0]["params"] = {}
        assert any("缺少必填参数 'min_ready'" in p for p in _problems(r))

    def test_bool_is_not_an_int(self):
        r = _minimal(); r["steps"][1]["post"][0]["params"] = {"min_ready": True}
        assert any("应为 int" in p for p in _problems(r))

    def test_misspelled_step_field(self):
        """拼错的 rolback 会被静默丢掉 —— 那个回滚说明就没了。"""
        r = _minimal(); r["steps"][1]["rolback"] = r["steps"][1].pop("rollback")
        ps = _problems(r)
        assert any("未知字段" in p and "rolback" in p for p in ps)

    def test_duplicate_ids(self):
        r = _minimal(); r["steps"][1]["id"] = "pre"
        assert any("重复" in p for p in _problems(r))

    def test_decision_needs_two_distinct_options(self):
        r = _minimal()
        r["steps"].append({"id": "d", "kind": "decision", "title": "选", "options": ["a", "a"]})
        assert any("两个互不相同" in p for p in _problems(r))

    def test_check_cannot_carry_a_command(self):
        r = _minimal(); r["steps"][0]["command"] = "rm -rf"
        assert any("check 步骤只能有 probes" in p for p in _problems(r))

    def test_two_blocks_are_ambiguous(self):
        rb = parse_runbook(_minimal())
        body = render_block(rb) + "\n\n" + render_block(rb)
        with pytest.raises(RunbookError) as e:
            extract_runbook(body)
        assert "只允许一个" in str(e.value)

    def test_all_problems_reported_at_once(self):
        """人改一次就能改完 —— 不是每次只报第一个。"""
        r = _minimal(); del r["steps"][1]["post"]; del r["steps"][1]["rollback"]
        assert len(_problems(r)) >= 2


class TestFalseSurvivesSerialization:
    def test_reversible_false_is_kept(self):
        """第一版把 `reversible: false` 当空值丢掉，不可逆步骤因此被拒。"""
        r = _minimal(); r["steps"][1]["reversible"] = False
        r["steps"][1]["irreversible_note"] = "因为…"; del r["steps"][1]["rollback"]
        block = render_block(parse_runbook(r))
        raw = json.loads(block.split("\n", 1)[1].rsplit("\n```", 1)[0])
        assert raw["steps"][1]["reversible"] is False


class TestGeneratorDoesNotInvent:
    def test_draft_parses(self):
        rb = draft_tokyo_to_seoul(_facts())
        assert len(rb.steps) >= 6

    def test_front_door_is_marked_for_human(self):
        rb = draft_tokyo_to_seoul(_facts())
        assert "switch-front-door" in rb.unresolved()

    def test_missing_fact_becomes_placeholder_not_guess(self):
        rb = draft_tokyo_to_seoul(_facts(secondary_cluster_arn=""))
        assert "promote-aurora" in rb.unresolved()

    def test_unresolved_plan_cannot_be_approved(self):
        f = _facts()
        body = render_body(f, draft_tokyo_to_seoul(f), generated_at="t", generator="g")
        ps = approval_problems(body)
        assert ps and "needs_human_input" in ps[0]

    def test_filled_plan_can_be_approved(self):
        f = _facts()
        rb = draft_tokyo_to_seoul(f)
        raw = rb.to_dict()
        for s in raw["steps"]:
            if s.get("needs_human_input"):
                s.pop("needs_human_input")
                s["command"] = "aws route53 change-resource-record-sets …"
                s["rollback"] = "aws route53 change-resource-record-sets …（指回东京）"
                s["post"] = [{"probe": "http_get", "params": {"url": "https://example.test/health"}}]
        assert approval_problems(render_block(parse_runbook(raw))) == []

    def test_no_dynamodb_promotion_step(self):
        """全局表是双活的，没有提升这一步 —— 不为了对称造一个。"""
        rb = draft_tokyo_to_seoul(_facts())
        assert not any("dynamo" in s.id for s in rb.steps if s.kind == "manual")

    def test_irreversible_step_comes_after_compute_is_proven(self):
        rb = draft_tokyo_to_seoul(_facts())
        ids = [s.id for s in rb.steps]
        assert ids.index("apply-workloads") < ids.index("promote-aurora")
        assert ids.index("aurora-mode") < ids.index("promote-aurora")

    def test_aurora_decision_has_no_default_and_offers_abort(self):
        rb = draft_tokyo_to_seoul(_facts())
        d = next(s for s in rb.steps if s.id == "aurora-mode")
        assert d.kind == "decision" and "abort" in d.options

    def test_expected_digest_comes_from_manifest_not_from_seoul(self):
        src = (ROOT / "dr-plan-generator" / "draft_runbook.py").read_text(encoding="utf-8")
        assert "循环论证" in src
        assert "images_from_manifests" in src


class TestCatalogMatchesImplementation:
    def _registered(self) -> set[str]:
        src = (W / "probes.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_IMPL":
                return {k.value for k in node.value.keys}
        pytest.fail("找不到 probes._IMPL")

    def test_one_to_one(self):
        assert self._registered() == set(PROBES)

    def test_each_probe_is_its_own_activity_type(self):
        src = (W / "probes.py").read_text(encoding="utf-8")
        assert "@activity.defn(name=activity_name(name))" in src
        assert "dispatcher" in src  # 文件里写明了为什么不做成一个分发器

    def test_every_spec_declares_means_and_question(self):
        for name, s in PROBES.items():
            assert s.question and s.means, name

    def test_param_types_are_the_two_supported(self):
        for name, s in PROBES.items():
            for k, p in s.params.items():
                assert p.type in ("str", "int"), f"{name}.{k}"

    def test_check_params_on_valid_minimal_calls(self):
        for name, s in PROBES.items():
            params = {k: ("x" if p.type == "str" else 1) for k, p in s.params.items() if p.required}
            assert check_params(name, params) == [], name


#: 写类 boto3 方法名的前缀。出现在 worker 目录里任何一处都算违规。
_MUTATING_PREFIXES = (
    "update_", "modify_", "delete_", "create_", "put_", "failover_", "switchover_",
    "terminate_", "start_", "stop_", "reboot_", "promote_", "register_", "deregister_",
    "attach_", "detach_", "change_", "set_", "add_", "remove_", "tag_", "untag_",
)
#: 唯一被允许的写：计划版本与执行记录写到自己的 S3 前缀。
_ALLOWED = {"put_object"}
#: 按文件放行的写（2026-10-07）。只有 arc_bridge.py 能起 ARC 执行 ——
#: 起执行本身不做变更，因为计划的第一步是要求 MFA 的人工审批。这个前提由
#: test_131 在计划模板上守着；两条门禁必须一起改。
_ALLOWED_IN_FILE = {("arc_bridge.py", "start_plan_execution")}
#: 不是 AWS 调用但名字碰巧以这些前缀开头的。
_NOT_AWS = {
    "start_activity", "start_workflow", "start_time_skipping", "start_local",
    "upsert_search_attributes", "add_search_attributes", "add_subparsers", "add_parser",
    "add_argument", "set_defaults", "setLevel", "setdefault", "start_to_close_timeout",
    "add_signal_handler", "set_result",
    "create_default_context",  # ssl —— count_ready_nodes 用集群 CA 校验 TLS
    "failover_task_queue",     # plan_workflow 的字段名（子执行用哪个队列），不是 AWS 调用
}


class TestTemporalNeverMutates:
    def _calls(self) -> list[tuple[str, str]]:
        out = []
        for f in sorted(W.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # ⚠️ 2026-10-07：原来只看 `x.method(...)` 形式的调用。
                #    `asyncio.to_thread(client.start_plan_execution, ...)` 把方法当参数传，
                #    不是 Call 的 func —— 原扫描**看不见它**（写 arc_bridge.py 时实测）。
                #    改为扫所有属性访问：凡是引用了写类方法名，都算。
                if isinstance(node, ast.Attribute):
                    out.append((f.name, node.attr))
        return out

    def test_no_mutating_aws_call_anywhere_in_worker(self):
        bad = [
            (f, m) for f, m in self._calls()
            if m.startswith(_MUTATING_PREFIXES) and m not in _ALLOWED and m not in _NOT_AWS
            and (f, m) not in _ALLOWED_IN_FILE
        ]
        assert not bad, (
            f"worker 里出现了写类调用 {bad}。执行模型是「人执行、Temporal 核实」——"
            "Temporal 一旦能自己改生产，「已审核、已演练」的结论就失去了意义"
        )

    def test_put_object_only_in_activities(self):
        files = {f for f, m in self._calls() if m == "put_object"}
        # snapshot_workflow.py 把图快照写到 snapshots/ 前缀 —— 一直都有，但它用
        # to_thread(s3.put_object, ...) 传方法引用，旧扫描看不见（2026-10-07 扩大扫描后发现）。
        # 那是自己的快照桶，不是生产资源。
        assert files <= {"activities.py", "snapshot_workflow.py"}, files

    def test_retired_modules_and_names_are_gone(self):
        assert not (W / "workflows.py").exists()
        src = "".join(p.read_text(encoding="utf-8") for p in W.glob("*.py"))
        for name in ("ScaleNodegroupWorkflow", "PromoteDatabaseWorkflow",
                     "async def scale_up_nodegroup", "async def promote_database"):
            assert name not in src, name

    def test_worker_registers_the_new_workflow_and_probes(self):
        src = (W / "worker.py").read_text(encoding="utf-8")
        assert "DrRunbookWorkflow" in src and "*ALL_PROBES" in src
        assert "DrFailoverWorkflow,\n" not in src

    def test_retirement_was_checked_against_running_executions(self):
        """删一个还有在途执行的 workflow 类型，那些执行会在回放时永久卡住。"""
        assert "没有任何 RUNNING 的执行" in (W / "worker.py").read_text(encoding="utf-8")


class TestRetentionCommentIsCurrent:
    def test_no_stale_one_day_claim(self):
        for p in [*W.glob("*.py"), ROOT / "dr-plan-generator" / "executor_temporal.py"]:
            t = p.read_text(encoding="utf-8")
            assert "保留期只有 1 天" not in t and "保留期只有 86400" not in t, p.name

    def test_measured_value_recorded(self):
        t = (ROOT / "dr-plan-generator" / "executor_temporal.py").read_text(encoding="utf-8")
        assert "2592000s" in t and "720h" in t


def test_deep_copy_does_not_alias(tmp_path):
    """防一个低级错误：_minimal() 每次返回新对象，测试之间不串。"""
    a, b = _minimal(), _minimal()
    a["steps"][0]["id"] = "x"
    assert b["steps"][0]["id"] == "pre"
    assert copy.deepcopy(a) == a

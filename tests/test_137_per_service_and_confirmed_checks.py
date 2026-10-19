"""test_137 — 工作负载按服务拆分、只读核查也逐条由人确认（2026-10-18，负责人要求）。

1. 拆分文件是 15-korea-workloads.yaml 的**逐字**派生物，不许漂移
2. 切换计划里每个服务一步，共享对象最先；每个服务核实它自己的 Deployment
3. Deployment 探针：目录与实现对应、RBAC 只在 petadoptions 读 deployments
4. 执行模式下只读核查要人确认（patched 保证旧执行回放不变），UI 上写出结论与确认命令
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
W = ROOT / "dr-plan-generator" / "worker"
for p in (str(W), str(ROOT / "dr-plan-generator"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

SRC = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
OUT = ROOT / "infra" / "dr-korea" / "workloads"


class TestSplitDoesNotDrift:
    def test_check_mode_passes(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "split_korea_workloads.py"), "--check"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_every_document_lands_in_exactly_one_file(self):
        from split_korea_workloads import split_raw
        _, src = split_raw(SRC.read_text(encoding="utf-8"))
        got = [d for p in sorted(OUT.glob("*.yaml")) for d in split_raw(p.read_text(encoding="utf-8"))[1]]
        assert sorted(src) == sorted(got)

    def test_one_deployment_per_service_file(self):
        for p in OUT.glob("*.yaml"):
            kinds = [d["kind"] for d in yaml.safe_load_all(p.read_text(encoding="utf-8")) if d]
            assert kinds.count("Deployment") == (0 if p.name == "00-shared.yaml" else 1), p.name

    def test_generator_resplits_after_writing(self):
        assert "split_korea_workloads.py" in (ROOT / "scripts" / "gen_korea_workloads.py").read_text(encoding="utf-8")


def _draft_module():
    """按文件路径加载 —— 全量跑时 sys.path 上可能先有别处的 dr-plan-generator（实测拿到了主干旧版）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_dr137", ROOT / "dr-plan-generator" / "draft_runbook.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestPlanHasOneStepPerService:
    def test_manifest_order_shared_first(self):
        dr = _draft_module()
        assert dr.MANIFESTS[0].endswith("workloads/00-shared.yaml")
        assert "infra/dr-korea/15-korea-workloads.yaml" not in dr.MANIFESTS, "不能再一条命令起全部服务"

    def test_every_service_has_its_own_step_and_deployment_probe(self):
        dr = _draft_module()
        wl = dr.workloads_from_manifests([])
        deps = {d for _, _, _, ds in wl for d in ds}
        want = {d["metadata"]["name"] for d in yaml.safe_load_all(SRC.read_text(encoding="utf-8"))
                if d and d.get("kind") == "Deployment"} | {"petsite-deployment"}
        assert deps == want
        assert all(len(ds) <= 1 for _, _, _, ds in wl), "一个清单只能对应一个服务"


class TestDeploymentProbe:
    def test_catalog_and_impl(self):
        from probe_catalog import PROBES
        assert "k8s_deployment_available" in PROBES
        src = (W / "probes.py").read_text(encoding="utf-8")
        assert '"k8s_deployment_available": _k8s_deployment_available' in src
        assert "    if gen is not None and (seen is None or seen < gen):\n" in src, (
            "要核对控制器已处理最新版本，否则 available 说的是旧版本")

    def test_missing_deployment_is_fail_not_unknown(self):
        src = (W / "probes.py").read_text(encoding="utf-8")
        i = src.index("def _k8s_deployment_available")
        assert 'return FAIL, {"exists": False}' in src[i: i + 1500]

    def test_rbac_is_namespaced_read_only(self):
        docs = [d for d in yaml.safe_load_all((W / "k8s-node-reader-rbac.yaml").read_text(encoding="utf-8")) if d]
        role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "dr-workload-reader")
        assert role["metadata"]["namespace"] == "petadoptions"
        assert role["rules"] == [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]}]
        assert not any(d["kind"] == "ClusterRole" and "deployments" in str(d.get("rules")) for d in docs)


class TestChecksNeedConfirmation:
    def test_patched_guard(self):
        src = (W / "runbook_workflow.py").read_text(encoding="utf-8")
        assert "if workflow.patched(PATCH_CHECK_CONFIRM):" in src
        i = src.index("if workflow.patched(PATCH_CHECK_CONFIRM):")
        assert '"check": True' in src[i: i + 700]

    def test_ui_shows_results_and_how_to_confirm(self):
        from runbook import FORMAT, parse_runbook
        import ui_text as u
        rb = parse_runbook({"format": FORMAT, "source_region": "a-b-1", "target_region": "a-b-2", "steps": [
            {"id": "c", "kind": "check", "title": "写节点在东京",
             "probes": [{"probe": "aurora_writer_region", "params": {"expect_region": "a-b-1"}}]}]})
        aw = {"type": "confirm", "step_id": "c", "check": True,
              "results": [{"probe": "aurora_writer_region", "verdict": "PASS",
                           "observed": {"writer_region": "a-b-1"}, "params": {"expect_region": "a-b-1"}}]}
        md = u.runbook_details(wid="w", mode="live", plan_id="p", version=1, rb=rb, status="running",
                               cursor=0, awaiting=aw, done_ids=set(), findings=[])
        assert "确认核查结果" in md and "✅ PASS" in md and "writer_region=a-b-1" in md and "confirm_step" in md
        assert "等人确认核查结果" in u.human_wait_summary(rb, aw, 24)

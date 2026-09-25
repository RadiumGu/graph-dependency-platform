"""
test_83_worker_permission_boundary.py — DR worker 权限边界的门禁。

本项目的权限策略是「按步骤逐个放开」,所以门禁守的是**还没放的不许提前放**,
以及**放了的要卡到最细**。

为什么值得有这道门禁:切换步骤的权限一旦给宽,就再没有人会回来收窄 ——
而 `eks:UpdateNodegroupVersion`(滚动替换节点)、`DeleteNodegroup`、
`rds:FailoverGlobalCluster` 这几个的后果都不可逆。

这里读模板文本而不连 AWS:判据是「模板里写了什么」,CI 里也能跑。
实际生效与否另有非侵入的核实手段 —— `aws iam simulate-principal-policy`,
它不改任何东西就能证明边界(见 deployment-record 4.10)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

TPL = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "dr-korea"
    / "07-worker-permissions.yaml"
)


@pytest.fixture(scope="module")
def doc() -> dict:
    assert TPL.exists(), f"{TPL} 不存在"
    # CFN 的短标签（!Ref / !Sub）不是标准 YAML，用最宽松的方式读：
    # 只关心 Action / Resource 的文本，所以把短标签当普通标量。
    class Loose(yaml.SafeLoader):
        pass

    def _any_tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    Loose.add_multi_constructor("!", _any_tag)
    return yaml.load(TPL.read_text(encoding="utf-8"), Loose)


def _statements(doc: dict) -> list[dict]:
    res = doc["Resources"]
    pol = next(v for v in res.values() if v["Type"] == "AWS::IAM::ManagedPolicy")
    return pol["Properties"]["PolicyDocument"]["Statement"]


def _all_actions(doc: dict) -> set[str]:
    out: set[str] = set()
    for st in _statements(doc):
        a = st.get("Action")
        out.update([a] if isinstance(a, str) else (a or []))
    return out


class TestNotYetGranted:
    """还没到那一步的写权限，不许提前出现在策略里。"""

    # 每一条都写清「为什么留到后面」，否则下一个人只看到一串禁令。
    #
    # ── 已毕业的一条 ──────────────────────────────────────────────────
    # `rds:FailoverGlobalCluster` 2026-09-25 从这个清单里移出。
    #
    # 当时留下的理由是「权限要等那条链路演练过再给」。链路已经演练过了
    # （4.19 节）：四种裁决各跑一遍 —— ordered 走 --switchover、
    # allow_data_loss 走 --allow-data-loss、abort 中止、非法裁决被忽略且
    # workflow 不崩，四种都是 0 个失败事件。
    #
    # 现在它由 test_95 接管，守的判据变成「只有那一个 action」+
    # 「Resource 是三个具体 ARN 而不是 *」。
    # **把它留在这个清单里会让 test_83 恒红** —— 而恒红项的危害不是它本身，
    # 是它训练所有人忽略红色。
    FORBIDDEN = {
        "eks:UpdateNodegroupVersion": "会滚动替换节点，不是切换需要的动作",
        "eks:DeleteNodegroup": "不可逆",
        "eks:CreateNodegroup": "切换不需要建新节点组",
        "route53:ChangeResourceRecordSets": "DNS 切换影响所有流量，最后才给",
    }

    @pytest.mark.parametrize("action", sorted(FORBIDDEN))
    def test_action_absent(self, doc: dict, action: str):
        acts = _all_actions(doc)
        assert action not in acts, (
            f"{action} 提前出现在 worker 策略里。留到后面的理由："
            f"{self.FORBIDDEN[action]}"
        )

    def test_no_wildcard_service_actions(self, doc: dict):
        # elasticloadbalancing:* 之类会改变暴露面，要先问用户。
        bad = [a for a in _all_actions(doc) if a.endswith(":*") or a == "*"]
        assert not bad, f"出现了通配动作 {bad} —— 每条写权限都要能单独演练并回滚"


class TestGrantedOnesAreNarrow:
    """已放开的权限要卡到最细。"""

    def test_nodegroup_scaling_is_resource_scoped(self, doc: dict):
        st = next(
            s for s in _statements(doc) if s.get("Sid") == "ScaleUpPilotLightNodegroup"
        )
        acts = st["Action"]
        acts = [acts] if isinstance(acts, str) else acts
        assert acts == ["eks:UpdateNodegroupConfig"], (
            f"这条只该有 UpdateNodegroupConfig，实际 {acts}"
        )
        res = st["Resource"]
        assert res not in ("*", ["*"]), (
            "节点组权限不许用 *。写成 * 意味着以后任何新节点组都自动落进"
            "这个角色的能力范围 —— 那是在没有需求时先开口子。"
        )

    def test_s3_read_is_prefix_scoped(self, doc: dict):
        st = next(s for s in _statements(doc) if s.get("Sid") == "ReadDrPlanBodies")
        res = st["Resource"]
        res = [res] if isinstance(res, str) else res
        joined = " ".join(str(r) for r in res)
        assert "plans/" in joined, "计划读权限必须限定到 plans/ 前缀"
        # 同桶里另一个前缀放的是 temporal-mcp 的可执行代码包。
        assert "temporal-mcp" not in joined

    def test_s3_is_read_only(self, doc: dict):
        acts = _all_actions(doc)
        writes = [a for a in acts if a.startswith("s3:") and a not in
                  ("s3:GetObject", "s3:ListBucket")]
        assert not writes, f"worker 不该有 S3 写权限，出现了 {writes}"

    def test_list_bucket_reason_is_recorded(self):
        # 这条权限的理由不是「方便」：没有它，HeadObject 对不存在的对象回 403
        # 而不是 404，「计划不存在」与「没权限」在返回码上就无法区分。
        text = TPL.read_text(encoding="utf-8")
        assert "404" in text and "403" in text, "ListBucket 的理由要留在模板里"


class TestNodegroupArnIsPinned:
    def test_arn_includes_uuid_segment(self, doc: dict):
        default = doc["Parameters"]["NodegroupArn"]["Default"]
        # EKS 的节点组 ARN 末段是它自己生成的 UUID；缺了它就等于 ARN 写错。
        assert re.search(r"/[0-9a-f]{8}-[0-9a-f]{4}-", default), (
            "节点组 ARN 少了 UUID 段。这个值只能从 describe-nodegroup 取，不能拼。"
        )

    def test_rebuild_hazard_is_documented(self, doc: dict):
        desc = doc["Parameters"]["NodegroupArn"]["Description"]
        # 节点组重建后 UUID 会变，权限静默失效且表现为 AccessDenied。
        assert "AccessDenied" in desc or "重建" in desc

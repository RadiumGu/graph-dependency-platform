"""test_113_waggle_korea.py — WaggleAI 在首尔重建的记录契约。

## 为什么这个文件守的是记录而不是资源

四个栈（dr-korea-waggle-iam / -bedrock / -routes / -runtimes）本身是声明式的，
模板就是它们的真相来源。仓库里需要守的是**判断与边界不被改写**：

1. **「角色是账号级所以可以复用」这个错误推论的订正**不许被删 ——
   它的失效形态是 runtime 根本起不来（信任策略先拒），排查会指向别处。
2. **数据驻留的丧失**必须留在记录里 —— 这是唯一一条技术上无解、
   只能由人决定是否接受的取舍。
3. **「没有做切换」的核实证据**不许被弱化成一句「没切换」。
4. 三处「判据/引用取错东西」的教训 —— 它们都属于同一族：
   报错指向的位置不是真因所在。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
INFRA = ROOT / "infra" / "dr-korea"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.38")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheRoleReuseFallacy:
    def test_permission_wildcard_finding_is_recorded(self, section: str):
        """我曾警告的静默失败点实测不存在 —— 这条订正不许被删。"""
        assert_contains(section, "**换模型不需要动权限。**")

    def test_trust_policy_is_the_real_blocker(self, section: str):
        """只看权限策略会漏掉信任策略 —— 后者先拒，表现是 runtime 起不来。"""
        assert_contains(section, "首尔的 AgentCore 服务主体**连 AssumeRole 都过不去**")

    def test_global_resource_vs_global_authorization(self, section: str):
        """区分「资源是全局的」与「授权范围是全局的」。"""
        assert_contains(section, "对**授权范围**不成立")


class TestImageReplication:
    def test_digest_is_the_criterion(self, section: str):
        """判据是 digest 相同，不是「首尔有这个仓库」。"""
        assert_contains(section, "**判据是 digest 相同，不是「仓库存在」：**")

    def test_why_replicate_not_rebuild(self, section: str):
        """重建出来的镜像不是演练过的那个。"""
        assert_contains(section, "同一个 digest 才是同一个东西")

    def test_superseded_comment_is_annotated_not_deleted(self, section: str):
        """「范围变了」与「当初错了」是两件事，原句要留着加订正。"""
        assert_contains(section, "**留着原句并加订正**")

    def test_replication_filter_is_declared_in_template(self):
        """CLI 改过的复制配置必须收回声明式，否则重新部署会丢掉它。"""
        tpl = (INFRA / "09-ecr-replication.yaml").read_text(encoding="utf-8")
        assert "WaggleRepoPrefix" in tpl
        assert "waggle-ai-" in tpl


class TestDataResidencyIsNotHidden:
    def test_residency_loss_is_recorded(self, section: str):
        """首尔只有 global.（全球路由）—— 区域驻留属性丢了，不许省略。"""
        assert_contains(section, "**所以首尔这套失去了区域驻留属性。**")

    def test_the_substitution_was_measured_with_a_counter_control(self, section: str):
        """替换表每项都实测，且做了反证（拿 jp. 打首尔）。"""
        assert_contains(section, "反证：拿东京现用的 jp. 直接打首尔 → ValidationException")

    def test_vendor_alignment_reason_for_concierge(self, section: str):
        """concierge 换同厂商而不是换 Claude，理由要留着。"""
        assert_contains(section, "**保持同一厂商比换框架风险小**")


class TestNoSwitchoverWasDone:
    def test_tokyo_param_unchanged_with_evidence(self, section: str):
        """「没切换」必须带证据（东京参数的最后修改时间早于本轮）。"""
        assert_contains(section, "2026-09-04T15:28:19Z")
        assert_contains(section, "本轮之前，没被动过")

    def test_seoul_param_is_self_consistency_not_switchover(self, section: str):
        """首尔参数指首尔 runtime 是让首尔自洽，不是切换。"""
        assert_contains(section, "那是让**首尔站点自洽**")


class TestWrongThingPointedAtByTheError:
    def test_ref_returns_arn_not_name(self, section: str):
        """报错指向长度上限，真因是 !Ref 取到的是 ARN。"""
        assert_contains(section, "**报错指向长度，真因是引用取到的东西不是名字**")

    def test_petfood_port_correction(self, section: str):
        """订正我自己的旧归因：端口本身就是 8080。"""
        assert_contains(section, "这次证实**端口本身就是 8080**")

    def test_dead_config_would_fail_at_ingestion(self, section: str):
        """照死配置建资源，失败点出现在 ingestion 而不是配置处。"""
        assert_contains(section, "**失败点会出现在 ingestion，指不到这里**")


class TestDeliberateDeviationsAreJustified:
    def test_managed_policy_instead_of_five_copies(self, section: str):
        """5 份策略 md5 相同 —— 用托管策略表达实测事实。"""
        assert_contains(section, "**md5 完全相同**")
        assert_contains(section, "**仍保留 5 个独立角色**")

    def test_dangling_param_deliberately_not_copied(self, section: str):
        """不照抄一个已知坏掉的值。"""
        assert_contains(section, "**照抄一个已知坏掉的值只会把悬空引用复制一份。**")

    def test_end_to_end_evidence_not_resource_status(self, section: str):
        """验证判据是真调用 + 委派发生，不是资源 READY。"""
        assert_contains(section, "→ 流式返回，且**委派给了 Nutrition agent**")


class TestTemplatesExist:
    @pytest.mark.parametrize(
        "name",
        [
            "20-waggle-iam.yaml",
            "21-waggle-bedrock.yaml",
            "22-waggle-backend-routes.yaml",
            "23-waggle-runtimes.yaml",
        ],
    )
    def test_template_present(self, name: str):
        """四个栈必须有对应模板 —— CLI 建的东西留不下重建路径。"""
        assert (INFRA / name).is_file(), f"缺模板 {name}"

    def test_petfood_target_port_is_8080(self):
        """按服务名统一填 80 会得到永远 unhealthy 的目标组。"""
        tpl = (INFRA / "22-waggle-backend-routes.yaml").read_text(encoding="utf-8")
        i = tpl.index("PetFoodTg:")
        block = tpl[i : i + 600]
        assert "Port: 8080" in block, "petfood 目标组端口必须是 8080"

    def test_alb_ingress_lives_in_the_owning_stack(self):
        """安全组规则由拥有它的栈声明，否则给那个栈造漂移。"""
        owner = (INFRA / "12-korea-alb.yaml").read_text(encoding="utf-8")
        assert "AllowWaggleRuntimeToAlb" in owner
        other = (INFRA / "22-waggle-backend-routes.yaml").read_text(encoding="utf-8")
        assert "AllowWaggleRuntimeToAlb" not in other

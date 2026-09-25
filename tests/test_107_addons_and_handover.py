"""
test_107_addons_and_handover.py — addon 的「按需」判断 + 交接文档的契约。

## 守的第一件事:两个「不补」的**前提**

`aws-ebs-csi-driver` 与 `eks-pod-identity-agent` 不补，依据是两条实测:

    清单里只有 configMap 卷、无 PVC        → 不需要 EBS CSI driver
    Pod Identity 只有 1 个关联且不属于 petsite → 不需要 pod-identity-agent

**这两条是会过期的结论。** 哪天有人给 petsite 加了 PVC、或把某个 SA
从 IRSA 改成 Pod Identity，「不补」就错了，而**错的表现是 pod 卡在
ContainerCreating 或者拿不到凭据** —— 都指不到 addon 缺失这个原因。

所以这里直接扫清单:出现 PVC 就挂，提醒结论已过期。

## 守的第二件事:交接文档里那几段「不知道就一定会栽」的东西

四种「全绿但不能服务」的形态、零节点拒绝建 Service、PDB 死锁、
切换的三个 CLI 细节。这些删掉之后文档看起来更清爽，而接手人会栽。

## 守的第三件事:版本与授权不许漂

addon 版本刻意与东京一致；节点角色刻意只补一条。
两者都是「灾备站点要跑与生产同一个东西」的体现。
"""
from __future__ import annotations

import collections
from pathlib import Path

import pytest
import yaml

from cfn_yaml import load_cfn, param_default
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ROOT / "infra" / "dr-korea" / "18-korea-addons.yaml"
EKS = ROOT / "infra" / "dr-korea" / "03-eks.yaml"
WORKLOADS = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
HANDOVER = ROOT / "docs" / "runbooks" / "dr-korea-handover.md"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def handover() -> str:
    return HANDOVER.read_text(encoding="utf-8")


class TestTheTwoSkipDecisionsStillHold:
    def test_no_pvc_in_workloads(self):
        """出现 PVC 就说明「不补 EBS CSI driver」这个结论过期了。

        失效形态是 pod 永远 ContainerCreating —— 指不到 addon 缺失。
        """
        docs = [
            d
            for d in yaml.safe_load_all(WORKLOADS.read_text(encoding="utf-8"))
            if d
        ]
        vol_kinds: collections.Counter = collections.Counter()
        for d in docs:
            if d.get("kind") != "Deployment":
                continue
            for v in d["spec"]["template"]["spec"].get("volumes") or []:
                vol_kinds.update(k for k in v if k != "name")
        assert "persistentVolumeClaim" not in vol_kinds, (
            "清单里出现了 PVC —— 「不补 aws-ebs-csi-driver」的依据已经不成立，"
            "要么补上 addon，要么改这里的结论"
        )

    def test_skip_reasons_are_recorded_in_template(self):
        """理由必须留在模板里，否则下一个人只看到「少了 4 个 addon」。"""
        src = ADDONS.read_text(encoding="utf-8")
        assert_contains(src, "**「按需」的意思是先测再补，不是照抄 5 个。**")
        assert_contains(src, "无 PVC")
        assert_contains(src, "list-pod-identity-associations")
        assert_contains(src, "三个「不补」都是**测出来的，不是估的**")


class TestAddonParityWithTokyo:
    def test_version_matches_tokyo_not_korea_default(self):
        """刻意用东京同版本，而不是韩国的默认版本。"""
        assert param_default(load_cfn(ADDONS), "ObservabilityVersion") == (
            "v6.5.0-eksbuild.1"
        )

    def test_config_uses_pseudo_params_not_hardcoded_tokyo(self):
        """region/cluster 必须由 !Sub 取，写死东京值是本会话最常见的缺陷。"""
        src = ADDONS.read_text(encoding="utf-8")
        cfg = load_cfn(ADDONS)["Resources"]["CloudWatchObservability"]["Properties"]
        rendered = str(cfg["ConfigurationValues"])
        assert "ap-northeast-1" not in rendered, "配置里写死了东京 region"
        assert "PetSite" not in rendered.replace("dr-korea-petsite", ""), (
            "配置里写死了东京集群名"
        )
        assert "${AWS::Region}" in src and "${ClusterName}" in src

    def test_collection_interval_matches_tokyo(self):
        """东京是 300 不是默认 60 —— 改小会让指标不可比，也多花钱。"""
        rendered = str(
            load_cfn(ADDONS)["Resources"]["CloudWatchObservability"]["Properties"][
                "ConfigurationValues"
            ]
        )
        assert '"metrics_collection_interval": 300' in rendered
        assert '"metrics_collection_interval": 60' not in rendered

    def test_node_role_gained_exactly_one_policy(self):
        """节点角色补一条就够，与东京逐条相同 —— 多加就是单方面偏离生产。"""
        arns = load_cfn(EKS)["Resources"]["NodeRole"]["Properties"]["ManagedPolicyArns"]
        names = {a.split("/")[-1] for a in arns}
        assert names == {
            "AmazonEKSWorkerNodePolicy",
            "AmazonEKS_CNI_Policy",
            "AmazonEC2ContainerRegistryReadOnly",
            "AmazonSSMManagedInstanceCore",
            "CloudWatchAgentServerPolicy",
        }, f"节点角色策略集与东京实测的 5 条不一致:{sorted(names)}"


class TestHandoverKeepsTheThingsThatBite:
    def test_four_false_green_shapes_are_all_there(self, handover: str):
        """**本文件最重要的一条。** 这是整段工作最贵的产出。"""
        for phrase in (
            "硬编码 5 字节",
            "`<title>Error - …</title>`",
            "`kubectl get pod` 一个异常都看不到",
            "挂 60 秒返回 504",
        ):
            assert_contains(handover, phrase)

    def test_the_single_acceptance_criterion_is_stated(self, handover: str):
        assert_contains(
            handover, "**验收判据就一条:逐个业务页面打一遍,看 `<title>`。**"
        )

    def test_zero_node_service_rejection_is_documented(self, handover: str):
        """零节点拒绝建 Service —— 不知道这条，接管时会撞上指不到真因的报错。"""
        assert_contains(handover, "零节点时集群**拒绝创建任何 Service**")
        assert_contains(handover, "no endpoints available for service")
        assert_contains(handover, "**这是直接实验出来的**")

    def test_pdb_deadlock_arithmetic_is_documented(self, handover: str):
        assert_contains(handover, "允许驱逐 **0** 个")
        assert_contains(handover, "卡满 **30 分钟**")

    def test_three_switchover_cli_details(self, handover: str):
        assert_contains(handover, "**回切时它变了**")
        assert_contains(handover, "必须是 **ARN**")
        assert_contains(
            handover, "**东京还活着时绝不要用 `--allow-data-loss`。**"
        )

    def test_unresolved_items_are_listed_not_buried(self, handover: str):
        """没解决的事必须留在文档里，且不许弱化。"""
        assert_contains(handover, "cart_repository.rs:338")
        assert_contains(handover, "**记 inconclusive,不记通过。**")
        assert_contains(handover, "0/2 healthy")
        assert_contains(handover, "两个 region 的 ECR 都没有这个仓库")

    def test_destroy_commands_carry_their_traps(self, handover: str):
        assert_contains(handover, "要先从 `petsite-global` 移除")
        assert_contains(handover, "**栈删了资源还在**")
        assert_contains(handover, "registry 级")

    def test_criteria_discipline_ratio_is_stated(self, handover: str):
        """判据错 15 次、功能从未错 —— 这个比例本身是信息。"""
        assert_contains(handover, "判据错过 15 次,功能实现从未错")
        assert_contains(
            handover, "**难的不是把事情做对,是判断自己有没有做对。**"
        )

    def test_python311_and_ci_floor_are_stated(self, handover: str):
        assert_contains(handover, "必须 python3.11")
        assert_contains(handover, "通过数下限 1040")

    def test_generated_file_warning(self, handover: str):
        assert_contains(handover, "是生成的,不要手改")

    def test_resting_signals_that_look_like_failures(self, handover: str):
        """静息时 addon 是 DEGRADED、ALB 目标是 unhealthy —— 都是常态。

        不写下来的后果是接手人去追一个不存在的故障；
        更要紧的是**这两个读数都无法区分「正常休眠」与「切换失败」**，
        所以它们不能当健康判据。
        """
        assert_contains(handover, "静息状态下有两个信号**长得像故障**")
        assert_contains(handover, "InsufficientNumberOfReplicas")
        assert_contains(
            handover, "这两个读数都无法区分「正常休眠」与「切换失败」"
        )
        assert_contains(handover, "判断守夜灯是否健康**不能看它们**")


class TestRecordAdmitsTheSkippedItem:
    def test_the_missed_item_is_admitted(self):
        """第⑤项被我跳过了 —— 这个事实必须留着。"""
        record = RECORD.read_text(encoding="utf-8")
        assert_contains(record, "这一项我漏了")
        assert_contains(
            record, "**顺序清单存在的意义就是防这个,\n而我没照它核对。**"
        )

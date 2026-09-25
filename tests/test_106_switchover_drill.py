"""
test_106_switchover_drill.py — 数据库切换演练的记录契约。

这次演练**真的降级过东京主库 57 秒、又切回来 52 秒**。它留下的东西里，
有几条一旦被人「顺手清理」掉，下一个照着手册做的人就会踩回去:

1. **回切必须在事前写好**，而不是演练完再想。红线原文如此。
2. **三个 CLI 细节**（专用命令名、--region 指主库、target 必须是 ARN），
   而且**回切时 --region 要跟着主库走** —— 顺序反了就会用错 region。
3. **提升前基线**。没有「提升前写入被拒」这个对照，「提升后能写」只是孤立观察。
4. **东京侧行为验证做不到，且原因是结构性的**（DR worker 在东京集群没有访问条目）。
   这一条必须留着记 inconclusive —— 把它改成「通过」是本会话最贵的那类错误。

## 为什么这个文件不检查基础设施而只检查记录

演练是**一次性动作**，它的产物就是记录本身。资源侧唯一的持久改动是
密钥资源策略多了第 8 条 principal —— 那一条在下面守。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cfn_yaml import load_cfn
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
STACK = ROOT / "infra" / "dr-korea" / "16-korea-backends.yaml"


@pytest.fixture(scope="module")
def record() -> str:
    return RECORD.read_text(encoding="utf-8")


class TestDrillUsedSwitchoverNotFailover:
    def test_no_allow_data_loss_anywhere_in_the_drill_section(self, record: str):
        """东京当时还活着 —— 用 --allow-data-loss 会白丢数据。"""
        start = record.index("### 4.29")
        end = record.index("## 六、待记录", start)
        section = record[start:end]
        assert "--allow-data-loss" not in section, (
            "演练记录里出现了 --allow-data-loss —— 东京还活着时应当用 switchover"
        )
        assert_contains(section, "switchover-global-cluster")

    def test_three_cli_details_are_cited(self, record: str):
        assert_contains(record, "`--region` 是**主库所在的 region**")
        assert_contains(record, "必须是 **ARN**")
        assert_contains(record, "aurora-global-database-disaster-recovery.html")

    def test_failback_region_follows_the_primary(self, record: str):
        """最容易弄错的一条:回切时主库已经在韩国。"""
        assert_contains(record, "回切时 `--region` 要跟着主库走")
        assert_contains(record, "`--region ap-northeast-2`")


class TestBaselineMakesTheConclusionCausal:
    def test_pre_promotion_rejection_is_recorded(self, record: str):
        """提升前写入被拒 —— 这是对照组，不许删。"""
        assert_contains(record, "cannot execute CREATE TABLE in a read-only transaction")

    def test_both_sides_of_the_comparison_present(self, record: str):
        assert_contains(record, "pg_is_in_recovery")
        assert_contains(record, "没有这个基线")

    def test_real_business_tables_were_seen(self, record: str):
        """复制的是真数据 —— 空库也能「写成功」，那证明不了复制。"""
        assert_contains(record, "transactions_history")
        assert_contains(record, "复制的是真数据,不是空壳")

    def test_drill_tables_were_dropped(self, record: str):
        assert_contains(record, "演练表建完即 `DROP`")


class TestFailbackHappenedAndWasVerifiedIndependently:
    def test_failback_is_recorded(self, record: str):
        assert_contains(record, "东京成为新主")
        assert_contains(record, "IsWriter=true")

    def test_verification_used_a_different_means(self, record: str):
        """红线:核实必须用与操作不同的手段。"""
        assert_contains(record, "不看切换命令的回显")

    def test_timings_are_from_rds_events(self, record: str):
        """时间来自 RDS 事件，而不是我那个坏掉的轮询。"""
        assert_contains(record, "Waiting for data synchronization")


class TestTokyoWritabilityStaysInconclusive:
    def test_it_is_recorded_as_inconclusive_not_passed(self, record: str):
        """**本文件最重要的一条。**

        把「没测到」改写成「通过」是本会话最贵的那类错误 ——
        四种「全绿但不能服务」的形态全都是这么来的。
        """
        assert_contains(record, "**这一条记 inconclusive,不记通过。**")
        assert_contains(record, "没有行为证据")

    def test_the_structural_reason_is_recorded(self, record: str):
        """原因是结构性的（东京集群无访问条目），不是偶发可重试。"""
        assert_contains(record, "DR worker 从设计上就打不到东京集群")
        assert_contains(record, "20 个条目，无 Temporal 角色")

    def test_the_earlier_wrong_attribution_is_corrected(self, record: str):
        """4.28 里把原因写成「token 中途失效」—— 那是错的，订正必须留着。"""
        assert_contains(record, "归因订正")
        assert_contains(record, "**那是错的**")
        assert_contains(
            record, "记错归因的代价是把一个「本来就不可能成功」的测法"
        )


class TestDrillGrantIsMinimalAndJustified:
    def test_eighth_principal_exists(self):
        sts = load_cfn(STACK)["Resources"]["KoreaDbSecretPolicy"]["Properties"][
            "ResourcePolicy"
        ]["Statement"]
        sids = [st["Sid"] for st in sts]
        assert "AllowDrWorkerReadForDrill" in sids, "演练用的那条授权不见了"

    def test_it_is_read_only(self):
        sts = load_cfn(STACK)["Resources"]["KoreaDbSecretPolicy"]["Properties"][
            "ResourcePolicy"
        ]["Statement"]
        st = next(s for s in sts if s["Sid"] == "AllowDrWorkerReadForDrill")
        assert st["Action"] == "secretsmanager:GetSecretValue", (
            "这条授权只该是只读 —— 出现写动作说明范围被放大了"
        )

    def test_business_roles_statement_is_untouched(self):
        """加第 8 条不许顺手改动前 7 个。"""
        sts = load_cfn(STACK)["Resources"]["KoreaDbSecretPolicy"]["Properties"][
            "ResourcePolicy"
        ]["Statement"]
        biz = next(s for s in sts if s["Sid"] == "AllowPetsiteAppRolesRead")
        assert len(biz["Principal"]["AWS"]) == 7, (
            f"业务角色从 7 个变成了 {len(biz['Principal']['AWS'])} 个"
        )

    def test_justification_is_recorded(self, record: str):
        assert_contains(record, "它不是为了绕过检查")


class TestMyOwnMeasurementMistakesAreRecorded:
    def test_all_three_are_listed(self, record: str):
        """判据错过 15 次、功能从未错 —— 这个比例本身是要留下的信息。"""
        assert_contains(record, "读不出值要先怀疑查询,而不是先下结论")
        assert_contains(record, "数缩进要看 `repr`")
        assert_contains(record, "又一次撞上**手抄标点**")

    def test_the_benefit_of_assert_first_is_recorded(self, record: str):
        assert_contains(record, "前两次都被 `assert` 拦住了,没有静默写坏")

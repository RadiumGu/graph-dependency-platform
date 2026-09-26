"""
test_112_public_exposure.py — 公网暴露面的结论 + 一次我造成的生产故障的记录契约。

## 为什么这个文件最重要的一条是「不许把故障记录改好看」

2026-09-26 我用一次**验证探测**打停了东京的领养流程约 22 分钟，
而这是同一个错误的**第二次** —— 4.34 里我 40 分钟前刚写下那条教训。

**把教训写下来不等于会照着做。** 这条记录的价值全在它的难看程度:
它记着我重复犯错、记着我第一次诊断也是错的（又一次子串误配）、
记着我用 IP 白名单造成了稳定 1/3 的静默拒绝。

删掉任何一条，下一个人（包括未来的我）就会重复它。

## 守的四件事

1. 两次事故的**因果链与影响面**不许被弱化
2. 「写下教训 ≠ 会照做」这个元教训不许被删
3. 东京那个网关**仍然开放**，不许被记成已修
4. 「同一手法在 A 可靠不代表在 B 可靠」—— IP 白名单的适用边界
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.36")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheOutageIsRecordedHonestly:
    def test_the_outage_and_its_duration(self, section: str):
        assert_contains(section, "约 22 分钟领养全停")
        assert_contains(section, "**领养完全停止**")

    def test_the_causal_chain_is_complete(self, section: str):
        """畸形行 → NPE → 500 → 取不到宠物 → LoadPetData 失败 → 无领养。"""
        assert_contains(section, "upsert 出一条只有 3 个属性的行")
        assert_contains(section, "NullPointerException")
        assert_contains(section, "LoadPetData 失败")

    def test_the_probe_was_mine(self, section: str):
        """不许写成「发现了一条畸形行」—— 那是我写进去的。"""
        assert_contains(section, "我用一次「验证探测」打停了东京的领养流程")
        assert_contains(section, "NONEXISTENT-verify-closed")

    def test_it_was_the_second_time(self, section: str):
        """**本文件最重要的一条。** 40 分钟前刚写下教训，然后又犯。"""
        assert_contains(section, "这是同一个错误的**第二次**")
        assert_contains(section, "然后我在 40 分钟后**又发了一次**")

    def test_the_meta_lesson(self, section: str):
        assert_contains(section, "**把教训写下来不等于会照着做。**")
        assert_contains(
            section,
            "**真正的防线是「不对生产发写请求」,而不是「写完记得清理」。**",
        )

    def test_the_wrong_first_diagnosis(self, section: str):
        """第一次诊断也错了 —— 又一次子串误配。"""
        assert_contains(section, "我的第一次诊断也是错的 —— 又一次子串误配")
        assert_contains(section, "含 `403`")
        assert_contains(section, "**「子串误配」这条我也写过,也又踩了一次。**")


class TestTheSilentPartialFailure:
    def test_the_one_third_denial_is_recorded(self, section: str):
        assert_contains(section, "稳定 1/3 的静默拒绝")
        assert_contains(section, "Count 24    4XX 8")

    def test_why_it_is_the_worst_shape(self, section: str):
        """领养成功、页面正常、日志无错，只有 1/3 状态不更新。"""
        assert_contains(
            section, "**没有任何症状会让人去查。**"
        )

    def test_the_ip_allowlist_boundary(self, section: str):
        """穷举失败的表现是静默部分失败，不是报错。"""
        assert_contains(
            section, "而**穷举失败的表现是静默部分失败,不是报错**"
        )

    def test_the_transferability_lesson(self, section: str):
        """同一手法在 A 可靠不代表在 B 可靠 —— 可靠性来自那次验证。"""
        assert_contains(
            section,
            "**「同一个手法在 A 处可靠」不代表在 B 处可靠 —— 可靠性来自那次验证，不是手法本身。**",
        )

    def test_the_rollback_is_recorded(self, section: str):
        assert_contains(section, "**已回滚**")


class TestRemainingExposureNotMarkedFixed:
    def test_tokyo_gateway_still_open(self, section: str):
        """不许记成已修 —— 它现在确实是开放的。"""
        assert_contains(section, "所以东京那个网关**仍然是开放的**")

    def test_the_proposed_correct_design(self, section: str):
        """正确做法是不依赖猜 IP。"""
        assert_contains(section, "private REST API + execute-api 接口 VPC 端点")
        assert_contains(section, "aws:SourceVpc")

    def test_bypass_header_left_alone_with_reason(self, section: str):
        """删它是影响共享系统的改动，需要用户拍板。"""
        assert_contains(section, "`X-Demo-Bypass` 旁路我也没动")
        assert_contains(section, "需要用户拍板，我不自行删除")

    def test_korea_is_clean(self, section: str):
        """韩国零公网暴露 —— 这个结论要留着作为对照。"""
        assert_contains(section, "公网 IP 的 EC2  0 台")

    def test_other_projects_not_touched(self, section: str):
        """别的项目的资源只报不动。"""
        assert_contains(section, "不是我建的 —— 只报不动")


class TestCrossAgentIncidentReport:
    """4.37 —— 另一个 agent 的事故报告与我方交叉核实，守住三处订正与两条边界。"""

    def test_blast_radius_mechanism_is_corrected(self, section: str):
        """订正:不是那一条坏，是那一条让整批一起坏 —— 否则解释不了全站影响。"""
        assert_contains(section, "不是那一条坏,是那一条让整批一起坏")

    def test_both_outage_windows_are_kept(self, section: str):
        """两个口径量的不是同一件事，混成一个数字会让下次复盘对不上账。"""
        assert_contains(section, "两者量的不是同一件事")

    def test_global_table_not_isolated_is_now_measured(self, section: str):
        """原本是推断的断言现在有实测 —— 这条升级不许被改回推断。"""
        assert_contains(section, "内容一模一样,当时首尔首页也是挂的")

    def test_residue_scan_used_shape_not_names(self, section: str):
        """按名字扫只能找到我记得的那几个 —— 判据必须是属性数分布。"""
        assert_contains(section, "**属性数分布单一值 = 全表形状一致**")

    def test_pre_edit_worktree_is_unobservable(self, section: str):
        """不许把「四条事实的合力」写成「我看过它编辑前的工作区」。"""
        assert_contains(section, "**我无法直接观测它编辑之前的工作区**")

    def test_external_fix_is_not_marked_verified(self, section: str):
        """本机无 JDK/docker —— 没编译没跑 IT，不许当作已验证。"""
        assert_contains(section, "不许当作已验证")
        assert_contains(section, "没编译、没跑 IT、没重建镜像")

    def test_skip_beats_default_values(self, section: str):
        """补默认值会让上游问题永久隐身 —— 这是采纳它方向的理由。"""
        assert_contains(section, "**跳过而不补默认值是对的**")

    def test_pod_and_target_group_are_not_service_criteria(self, section: str):
        """盲区第三次重复 —— 判据只能是逐个业务页面看标题。"""
        assert_contains(section, "本次是**第三次**")
        assert_contains(section, "Pod 状态与目标组健康都不构成「能服务」的判据")

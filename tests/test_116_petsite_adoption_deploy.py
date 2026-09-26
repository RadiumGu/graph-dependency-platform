"""test_116_petsite_adoption_deploy.py — 「已合并 ≠ 已部署」这一课的门禁。

## 守什么

本次发现的缺口不是代码缺陷，而是**交付缺陷**：修复在主干里躺了 22 天，
review 通过、测试通过、源码里明明有 —— 而线上镜像是在它之前 3 分 53 秒
构建的。读代码永远发现不了这种事。

门禁守四类容易被改写或遗忘的东西：

1. **判据本身**（镜像构建时间 vs 提交时间；以及 3 分 53 秒这个量级 ——
   只看日期会得出相反结论）。
2. **我选错过的成败判据**：那两条字符串只在异常 message 里、从不写日志。
   少了这条记录，下一个人会重复数日志然后误判修复无效。
3. **两次「表是空的」误判** —— 读写是两张不同的表；以及清表循环。
4. **合成流量有两个来源**，其中一个在集群外、手工启动、无服务单元。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
TPL = ROOT / "infra" / "tokyo" / "02-petsite-hotfix-build.yaml"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.42")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheDeliveryGapCriterion:
    """靠读代码发现不了 —— 判据只有构建时间与提交时间的先后。"""

    def test_criterion_is_build_time_vs_commit_time(self, section: str):
        assert_contains(section, "**这类缺口靠读代码发现不了**")

    def test_the_margin_is_recorded(self, section: str):
        """3 分 53 秒。只看日期（都是 09-05）会得出相反结论。"""
        assert_contains(section, "3 分 53 秒")
        assert_contains(section, "如果只看日期（都是 09-05），会得出完全相反的结论")

    def test_both_regions_ran_the_same_digest(self, section: str):
        assert_contains(section, "**同一个 digest**")


class TestTheWrongSuccessCriterion:
    """我差点判定修复无效 —— 因为选了一个永远不会被产生的信号。"""

    def test_exception_never_reaches_logs(self, section: str):
        assert_contains(section, "从不写日志")

    def test_lesson_verify_the_signal_exists(self, section: str):
        assert_contains(section, "**选判据前要先确认那个信号真的会被产生。**")

    def test_the_working_criterion_is_the_page(self, section: str):
        """带 userId 真成功 / 不带则诚实失败 —— 这个对照是唯一有效的判据。"""
        assert_contains(section, '"Sorry, something went wrong"')
        assert_contains(section, '"Adoption Complete / Thank you for adopting"')


class TestEmptyTableIsNotWriteFailure:
    def test_two_different_tables(self, section: str):
        assert_contains(section, "**「22 天没有新增交易」—— 看错了表。**")

    def test_cleanup_loop_explains_the_empty_table(self, section: str):
        assert_contains(section, "随后被那个重置循环清掉")

    def test_generalised_lesson(self, section: str):
        assert_contains(section, "**「表是空的」几乎从不等于「写入失败」。**")

    def test_trailing_slash_mistake_recorded(self, section: str):
        """纯操作失误也记下来：路由带尾斜杠。"""
        assert_contains(section, "**带尾斜杠**")


class TestTwoTrafficSources:
    def test_ec2_loadgen_identified(self, section: str):
        assert_contains(section, "i-05f0b897988a48d17")
        assert_contains(section, "petsite-loadgen")

    def test_no_service_unit_means_no_auto_restart(self, section: str):
        """杀掉不会自动回来 —— 动它之前必须知道这件事。"""
        assert_contains(section, "杀掉不会自动回来")

    def test_it_is_the_third_gateway_path(self, section: str):
        """它也是收窄无认证网关时绕不开的那条公网路径。"""
        assert_contains(section, "第三条公网调用路径")

    def test_source_evidence_for_missing_userid(self, section: str):
        """不是推断，是 Worker.cs 的直接证据。"""
        assert_contains(section, "Worker.cs:139")
        assert_contains(section, "// ← 没有 userId")

    def test_real_users_are_fine(self, section: str):
        assert_contains(section, "**真实用户的领养是好的，坏的是合成流量。**")


class TestBrowserFlowVerified:
    def test_confirmation_page_renders(self, section: str):
        assert_contains(section, 'title "Adopt Me - Observability PetAdoptions"')

    def test_earlier_302_was_a_probe_artefact(self, section: str):
        """一个正常页面在缺令牌时看起来和坏掉的一模一样。"""
        assert_contains(section, "**那是因为请求缺防伪令牌与 userId，不是页面的问题。**")


class TestBuildSelfProof:
    def test_buildspec_greps_both_halves_of_the_fix(self):
        tpl = TPL.read_text(encoding="utf-8")
        assert "userId is required by payforadoption" in tpl
        assert "IsSuccessStatusCode" in tpl

    def test_native_arm64(self):
        tpl = TPL.read_text(encoding="utf-8")
        assert "ARM_CONTAINER" in tpl
        assert "aarch64" in tpl

    def test_timeout_accounts_for_dotnet_restore(self):
        """restore 实测可达 26 分钟，超时给足余量比再踩一次误导性失败便宜。"""
        tpl = TPL.read_text(encoding="utf-8")
        assert "TimeoutInMinutes: 90" in tpl


class TestPreexistingHealthCheckMismatch:
    def test_not_caused_by_this_deploy(self, section: str):
        assert_contains(section, "健康检查是**目标组的属性**")
        assert_contains(section, "/health/status` 实测返回 200")

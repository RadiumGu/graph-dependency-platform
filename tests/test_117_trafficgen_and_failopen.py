"""test_117_trafficgen_and_failopen.py — 合成流量修复 + 「前门靠 fail-open」这一课。

## 守什么

1. **ALB fail-open**：一个目标组里没有健康目标时，ALB 把请求发给全部目标。
   所以「站点返回 200」永远不能证明健康检查在工作 —— 本次前门就这样
   至少运行了 4 天（HealthyHostCount 恒为 0）。这条最容易被忘记，
   因为它的表现是「一切正常」。
2. **两个交付目标**：合成流量有集群内与集群外两个来源，只改一个等于没改。
3. **判据不用日志**：Logs Insights 有摄取延迟，用 DynamoDB 与 transactions 表。
4. **灾备两侧同版本**：不同版本的代码是灾备最难发现的不一致。
5. 我在 4.42 写错的那句（`restart=unless-stopped` 会自动拉回）已订正。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
REPL = ROOT / "infra" / "dr-korea" / "09-ecr-replication.yaml"
DRILL = ROOT / "infra" / "dr-korea" / "petsite-korea-drill.yaml"
TPL = ROOT / "infra" / "tokyo" / "03-trafficgen-hotfix-build.yaml"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.43")
    return txt[start : txt.index("## 六、待记录", start)]


class TestFailOpenIsTheHeadline:
    def test_metric_proves_it_predates_the_deploy(self, section: str):
        assert_contains(section, "每天恒定")

    def test_two_hundred_never_proves_health_checking(self, section: str):
        assert_contains(section, "**所以「站点是 200」这件事从来不能证明健康检查在工作。**"
                        .replace("**所以", "所以").replace("工作。**", "工作"))

    def test_three_consequences_listed(self, section: str):
        assert_contains(section, "健康检查提供零保护")
        assert_contains(section, "ALB 会把全部流量压到那一个上")

    def test_first_real_health_check_in_days(self, section: str):
        assert_contains(section, "第一次有真实的健康检查")

    def test_uncodified_infrastructure_named(self, section: str):
        assert_contains(section, "**未编码的基础设施。**")


class TestTwoDeliveryTargets:
    def test_both_sources_recorded(self, section: str):
        assert_contains(section, "只做一个等于没做")
        assert_contains(section, "i-05f0b897988a48d17")

    def test_no_credential_path_used(self, section: str):
        """从公开仓库在本机构建，绕开被拦的 ECR 登录形状。"""
        assert_contains(section, "全程零凭据")

    def test_env_exported_not_transcribed(self, section: str):
        assert_contains(section, "导出比誊写可靠")
        assert_contains(section, "TRAFFIC_GENERATOR_HEADER")

    def test_rollback_tag_kept(self, section: str):
        assert_contains(section, "petsite-trafficgen:pre-userid")


class TestCriterionAvoidsLogs:
    def test_ingestion_lag_recorded(self, section: str):
        assert_contains(section, "**日志摄取有延迟**")

    def test_evidence_is_data_not_logs(self, section: str):
        assert_contains(section, "7 yes / 19 no")

    def test_no_malformed_rows_still_holds(self, section: str):
        assert_contains(section, "零残缺行，上午的 upsert 修复守住了")


class TestMyOwnCorrection:
    def test_restart_policy_correction(self, section: str):
        assert_contains(section, "**后半句错**")
        assert_contains(section, "restart=unless-stopped")


class TestDrParity:
    def test_replication_does_not_backfill(self, section: str):
        assert_contains(section, "**复制不追溯存量**")

    def test_hotfix_prefix_in_replication(self):
        assert "petsite-hotfix" in REPL.read_text(encoding="utf-8")

    def test_korea_manifest_pinned_by_digest(self):
        txt = DRILL.read_text(encoding="utf-8")
        assert "petsite-hotfix@sha256:0a7dde1bf631b4568ce7b8f762c6a0e82576b9aeaf8b054d7e5b0255df316c93" in txt

    def test_korea_manifest_explains_why(self):
        """不改它不是「缺个补丁」，而是切过去会把缺陷带回来。"""
        txt = DRILL.read_text(encoding="utf-8")
        assert "会把已修掉的缺陷带回来" in txt

    def test_version_skew_is_the_hardest_dr_bug(self, section: str):
        assert_contains(section, "切过去时一切\"正常\"，只有数据不对")


class TestBuildSelfProof:
    def test_buildspec_greps_the_fix(self):
        assert "userId=traffic-generator" in TPL.read_text(encoding="utf-8")

    def test_template_names_the_second_target(self):
        """模板本身要提醒别忘了集群外那两个进程。"""
        assert "i-05f0b897988a48d17" in TPL.read_text(encoding="utf-8")

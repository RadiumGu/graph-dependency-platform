"""test_114_petsearch_hotfix.py — NPE 热修上线的记录契约。

## 守什么

这次改动同时动了**生产**（东京 search-service 的 image）和**另一个仓库的 main**，
而它带的回归保护是 **0**。所以仓库里必须守住四件事不被改写：

1. **修复曾落在不会被构建的副本上** —— 这是本次最容易重犯的错：
   两份源码副本、不同 AWS SDK 版本、只有一份进镜像。
2. **判据是 Dockerfile 的 COPY 路径**，不是「错误日志文案对得上」——
   那句文案两份副本里都有。
3. **回滚锚点与「下一次 cdk deploy 会覆盖」** —— 少了后者，
   后人会把 image 被改回 asset 镜像当成回归。
4. **行为未验证** —— 不许把「新代码在跑」写成「修复已验证」。
   没有安全的注入途径：那张表是全局表，往任一侧写都会到东京。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
TPL = ROOT / "infra" / "tokyo" / "01-petsearch-hotfix-build.yaml"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.39")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheFixWasInTheWrongCopy:
    def test_three_independent_evidences(self, section: str):
        """不是「我觉得」，而是 Dockerfile / settings.gradle / SDK 版本三条。"""
        assert_contains(section, "settings.gradle 只有 rootProject.name，**没有 include** 那个子目录")
        assert_contains(section, "③ 两份 SearchController.java 的 md5 不同，且 **AWS SDK 版本不同**：")

    def test_log_string_is_not_a_criterion(self, section: str):
        """两份副本里都有那句文案 —— 它不能用来判断哪一份在跑。"""
        assert_contains(section, "**不能作为判据**")

    def test_silently_ineffective_is_named(self, section: str):
        """最坏的形态：看起来修好了而线上一行没动。"""
        assert_contains(section, "静默无效")


class TestWhyNotCdkDeploy:
    def test_cdk_deploy_would_wipe_manual_rules(self, section: str):
        """不走 cdk deploy 的理由必须留着，否则下次有人直接部整栈。"""
        assert_contains(section, "**已被证明不安全**：443 监听器上的 Cognito 规则")

    def test_next_cdk_deploy_overwrite_is_not_a_regression(self, section: str):
        """覆盖回 asset 镜像不是回归 —— 少了这句会被误判。"""
        assert_contains(section, "所以覆盖不是退步")

    def test_repo_name_avoids_side_effect(self, section: str):
        """刻意不占用 petsearch-java 这个名字。"""
        assert_contains(section, "不制造未知副作用")


class TestBuildSelfProof:
    def test_buildspec_greps_the_fix(self):
        """构建前先证明修复在源码里 —— 否则会产出「看起来是热修」的镜像。"""
        tpl = TPL.read_text(encoding="utf-8")
        assert "REQUIRED_PET_ATTRIBUTES" in tpl
        assert "petsSkippedMalformed" in tpl

    def test_native_arm64_not_emulated(self):
        """Dockerfile 钉死 arm64，构建环境也用原生 arm64。"""
        tpl = TPL.read_text(encoding="utf-8")
        assert "ARM_CONTAINER" in tpl
        assert "aarch64" in tpl

    def test_verify_step_needs_its_own_permission(self, section: str):
        """核实步骤缺权限时，表现是「构建失败」而产物其实已经出来了。"""
        assert_contains(section, "而它失败的表现是「构建失败」——")


class TestRollbackAndBaseline:
    def test_rollback_digest_recorded(self, section: str):
        """回滚锚点必须是具体 digest，不是「改回去就行」。"""
        assert_contains(section, "sha256:2983db73c18055b27bc9aefb7c7f937778ba59b8c58dd0a5b54a21aba73e69bd")

    def test_pre_deploy_baseline_recorded(self, section: str):
        """没有部署前基线，「部署后是 26 条」说明不了任何事。"""
        assert_contains(section, "部署前基线")


class TestBehaviourIsNotClaimedVerified:
    def test_unverified_half_is_explicit(self, section: str):
        """只验证了「新代码在跑」，没验证「跳过行为成立」。"""
        assert_contains(section, "仍然没有验证的那一半")
        assert_contains(section, "**我不会为了验证再对生产写一次。**")

    def test_zero_regression_protection_is_stated(self, section: str):
        """回归保护是 0 —— 不许省略这句。"""
        assert_contains(section, "本次上线带的回归保护是 0")

    def test_ci_enablement_correction(self, section: str):
        """我自己那条订正也不完整 —— 真因是 fork 的 Actions 从未被放行。"""
        assert_contains(section, "**那条订正本身也不完整。**")

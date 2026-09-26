"""test_115_statusupdater_upsert.py — 「状态更新不能创建记录」的门禁。

## 守什么

两次生产事故（2026-09-15、2026-09-26）的根因都是同一件事：
`UpdateItem` 是 **upsert**，没有 `ConditionExpression` 时，对不存在的键
调用它会创建一条只含 `pettype` + `petid` + `availability` 的残缺行。

这个缺陷有三个让它难被发现的性质，门禁逐条守住：

1. **缺了条件时接口照样返回 200** —— 所以判据不能是返回码。
2. **首尔那份是内联代码**（CFN `ZipFile`），不会被任何测试覆盖 ——
   所以必须由门禁在模板里断言那一行存在。
3. **「加请求校验」是一条听起来对而实际无效的修法** ——
   真实请求载荷是合法的。记录里必须留着这个反面判断，
   否则下一个人会重新提出它并以为问题解决了。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
KOREA = ROOT / "infra" / "dr-korea" / "19-korea-statusupdater.yaml"


@pytest.fixture(scope="module")
def korea() -> str:
    return KOREA.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.40")
    return txt[start : txt.index("## 六、待记录", start)]


class TestKoreaInlineLambdaHasTheCondition:
    """首尔那份是内联代码，没有任何测试会跑它 —— 只有门禁能守。"""

    def test_condition_expression_present(self, korea: str):
        assert "ConditionExpression: 'attribute_exists(petid)'" in korea

    def test_conditional_failure_maps_to_404(self, korea: str):
        assert "ConditionalCheckFailedException" in korea
        assert "'pet not found'" in korea

    def test_other_errors_are_rethrown(self, korea: str):
        """只有条件失败才是 404；否则真故障会被伪装成「宠物不存在」。"""
        assert "throw err;" in korea

    def test_bad_input_returns_400_not_502(self, korea: str):
        assert "'invalid json body'" in korea
        assert "'pettype and petid are required'" in korea

    def test_availability_is_still_booleanised(self, korea: str):
        """原有行为不能被这次改动带歪：带了字段就 yes，完全不带才 no。"""
        assert "payload.petavailability === undefined ? 'no' : 'yes'" in korea

    def test_comment_warns_against_removing_it(self, korea: str):
        """那一行看起来可以删 —— 注释必须说清删掉的后果。"""
        assert "upsert" in korea
        assert "不能删" in korea


class TestTheRejectedFixIsRecorded:
    """「加请求校验」这条无效修法必须留在记录里，否则会被重新提出。"""

    def test_validation_would_not_have_stopped_it(self, section: str):
        assert_contains(section, "**请求校验挡不住这次故障**")

    def test_my_own_advice_is_corrected(self, section: str):
        assert_contains(section, "**那条建议是错的。**")

    def test_cfn_cannot_adopt_existing_method(self, section: str):
        """线上用 CLI 建方法会让下次 cdk deploy 失败 —— 比漂移更糟。"""
        assert_contains(section, "CFN 无法接管已存在的资源")
        assert_contains(section, "定时炸弹")


class TestCriterionIsNotTheStatusCode:
    def test_missing_condition_still_returns_200(self, section: str):
        """这是它两次都没被当场发现的原因，必须写明。"""
        assert_contains(section, "因为缺了条件时接口返回码仍然是 200")

    def test_reverse_verification_recorded(self, section: str):
        assert_contains(section, "**判据是传给 UpdateCommand 的参数，不是返回码**")


class TestVerificationSafetyIsExplained:
    def test_korea_lambda_points_at_the_global_table(self, section: str):
        """「在首尔验证」不等于与东京隔离 —— 这个误解会让人放心地写脏数据。"""
        assert_contains(section, "**首尔那个 Lambda 指向的就是全局表**")

    def test_defence_in_depth_payoff_named(self, section: str):
        assert_contains(section, "它把一次验证从「有风险」变成「可做」")

    def test_unfinished_check_is_admitted(self, section: str):
        """流量是突发的，所以「没误伤真实调用方」还只有间接证据。"""
        assert_contains(section, "目前只有间接证据")


class TestEipReleaseDiscipline:
    def test_seven_checks_recorded(self):
        txt = RECORD.read_text(encoding="utf-8")
        start = txt.index("### 4.41")
        sec = txt[start : txt.index("## 六、待记录", start)]
        assert_contains(sec, "无 Route53 记录指向")
        # 「没被用」与「没有人准备用」是两件事 —— 这个区分是本节的要点。
        assert_contains(sec, "没有人**准备**用它")

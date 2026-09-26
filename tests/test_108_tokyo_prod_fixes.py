"""
test_108_tokyo_prod_fixes.py — 两项东京生产修复的记录契约。

## 为什么这个文件只检查记录,而不检查基础设施

这两项改的是**东京生产**,而东京的资源不在本仓库的 CFN 里(它们由 demo 仓库的
CDK 管)。本仓库能守住的是:**判断依据与它的边界不许被改写成更好看的样子。**

## 守的四件事

1. **健康检查那条 matcher 的诚实边界。** `200-399` 证明进程与管线活着，
   **不证明下游可用** —— 因为 302 在调 PetSearch 之前就返回。
   把这条边界删掉，下一个人就会把它当成 readiness。

2. **`cdk deploy ServicesEks2` 不安全这件事。** 线上监听器规则被手工改过，
   部署整栈会收敛掉优先级 1/2/3。这让「改源码再部署」这条本该最正规的路
   变成了最危险的路 —— 不写下来，下一个人会照着常识去部署。

3. **`/Checkout` 真因与我原判断相反这件事。** 4.28 记的是「该用 Query」，
   4.32 推翻了它。**订正必须留着** —— 否则「改成 Query」会被重新提出来。

4. **东京那张表还没改。** 它需要删生产表，按红线要先问用户。
   这个待办不许被写成已完成。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def record() -> str:
    return RECORD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def section(record: str) -> str:
    start = record.index("### 4.32")
    return record[start : record.index("## 六、待记录", start)]


class TestHealthCheckHonestBoundary:
    def test_what_it_proves_and_does_not(self, section: str):
        """**本文件最重要的一条。** 删掉边界，matcher 就会被当成 readiness。"""
        assert_contains(section, "**不能**:下游(PetSearch / SSM / AgentCore)可用")
        assert_contains(section, "因为 302 在调 PetSearch **之前**返回")

    def test_health_status_is_not_the_answer(self, section: str):
        """指向 /health/status 掩盖程度更高 —— 理由要留着。"""
        assert_contains(section, "**硬编码 5 字节 `\"Alive\"`**")
        assert_contains(section, "掩盖程度比现在更高")

    def test_no_endpoint_qualifies(self, section: str):
        """诚实结论:petsite 没有合格的 readiness 端点。"""
        assert_contains(
            section,
            "**petsite 里没有一个端点既稳定返回 200、又真反映可用性。**",
        )

    def test_the_default_value_root_cause(self, section: str):
        """健康检查是 CDK 默认值，不是谁写错了 —— 这个区别决定了修法。"""
        assert_contains(section, "**根本没有 `healthCheck` 配置块**")
        assert_contains(section, "**全是 CDK/ELBv2 的默认值**")

    def test_both_measurement_channels_recorded(self, section: str):
        """红线:核实要用与操作不同的手段。两个通道都要留。"""
        assert_contains(section, "describe-target-health")
        assert_contains(section, "UnHealthyHostCount 最大值")
        assert_contains(section, "HealthyHostCount   最小值")


class TestCdkDeployIsUnsafeWarning:
    def test_the_drift_is_recorded(self, section: str):
        assert_contains(section, "`cdk deploy ServicesEks2` 现在不安全")
        assert_contains(section, "线上 443 监听器规则**被手工改过**")

    def test_the_inversion_is_stated(self, section: str):
        """「改源码再部署」反而最危险 —— 这个反直觉结论要显式写出来。

        ⚠️ 这条断言最初写成跨行片段，匹配不到 —— 又一次踩「断言跨行」。
        文件里那句话在同一行上，所以只匹配不跨行的部分。
        """
        assert_contains(section, "这条本该最正规的路变成了最危险的路")


class TestCheckoutRootCauseCorrection:
    def test_the_earlier_judgement_is_marked_wrong(self, section: str):
        assert_contains(section, "**那个方向是错的。**")

    def test_the_decisive_evidence_is_the_write_shape(self, section: str):
        """决定性证据是写入形状，不是读法 —— 这是整条推理的支点。"""
        assert_contains(section, "决定性证据是**写入形状**")
        assert_contains(section, "**根本不写 `item_id`**")

    def test_put_item_also_broken(self, section: str):
        """比原判断更严重的一层:写也坏。表从来没被成功写入过。"""
        assert_contains(section, "**所以这张购物车表从来没被成功写入过。**")
        assert_contains(section, "scan 计数都是 0")

    def test_scan_not_itemcount(self, section: str):
        """用 scan 而不是 ItemCount —— 后者最终一致，会给出错误的空/非空判断。"""
        assert_contains(section, "后者是最终一致的")

    def test_the_wrong_comment_is_called_out(self, section: str):
        """那条 CDK 注释本身就是缺陷的一部分。"""
        assert_contains(section, "那条 CDK 注释本身就是缺陷的一部分")

    def test_cfn_custom_name_constraint(self, section: str):
        """CFN 拒绝替换自定义名资源，且不看名字是否已空出来。"""
        assert_contains(
            section, "CloudFormation cannot update a stack when a custom-named"
        )
        assert_contains(section, "**它不看名字是否已空出来**")

    def test_end_to_end_evidence(self, section: str):
        """API 级证据比页面更硬 —— 写入、读回、页面渲染三段都要留。"""
        assert_contains(section, "Catnip Kitten Treats")
        assert_contains(section, "真的持久化了")
        assert_contains(
            section,
            "**那个「从来没成功写入过」的 PutItem 现在能写了 —— "
            "这就是「缺陷在表键不在读法」的证明。**",
        )


class TestMyOwnMeasurementMistakeRecorded:
    def test_missing_userid_mistake(self, section: str):
        assert_contains(section, "页面测试忘了带 userId")
        assert_contains(section, "**无 `userId` 时重定向会把原路径丢掉**")

    def test_the_control_is_recorded(self, section: str):
        """对照组:不带 userId → Home。没有对照，四个相同标题说明不了原因。"""
        assert_contains(
            section, "**证明差别来自 userId 而不是别的**"
        )

    def test_the_irony_is_recorded(self, section: str):
        """我刚记档的机制下一步就骗了我自己的测量 —— 这个教训值钱。"""
        assert_contains(
            section,
            "**我刚写进记录的那个重定向行为,下一步就把我自己的测量骗了。**",
        )


class TestTokyoTableStillPending:
    def test_it_is_recorded_as_not_done(self, section: str):
        """不许写成已完成 —— 它需要删生产表，按红线要先问用户。"""
        assert_contains(section, "东京那张表**还没改** —— 需要用户确认")
        assert_contains(section, "命令已备好但**未执行**")

    def test_the_reason_it_needs_asking(self, section: str):
        assert_contains(section, "要**删一张生产表**再重建")

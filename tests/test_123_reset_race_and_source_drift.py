"""test_123_reset_race_and_source_drift.py — 守住重置竞态的修法与「生产领先于源码」的教训。

## 守什么

1. **删除必须按主键，不能按 (pet_id, pet_type)。** 后者有 TOCTOU 竞态：
   读列表之后新产生的交易行会被一起删掉，宠物就变成 availability='no'
   且无交易行 —— 而重置列表来自 transactions，所以永远回不来。
   2026-10-03 实测 20 小时里 26 只有 17 只这样消失，成功率 80%→33%。

2. **构建前自证必须包含「旧写法不在了」。** 只证明新写法在，不能排除
   两种写法同时存在（那样竞态仍然活着）。

3. **「单调下降」这个形状是判据。** 它排除了「阈值选错、系统本就在附近抖」
   这个解释。这句话丢了，下一个人会先去调阈值。

4. **停止流血 ≠ 补回失血。** 修复不会复原既有的孤立宠物。

5. **生产领先于源码这件事本身。** 守卫在线上跑了 7 天而主干没有，
   PR 一直 OPEN 而我的记录写成「已合并」。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
STACK = ROOT / "infra" / "tokyo" / "06-payforadoption-hotfix-build.yaml"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.49")
    return txt[start : txt.index("## 六、待记录", start)]


@pytest.fixture(scope="module")
def stack() -> str:
    return STACK.read_text(encoding="utf-8")


class TestBuildGateProvesBothDirections:
    def test_asserts_new_query_present(self, stack: str):
        assert "grep -q 'SELECT id, pet_id, pet_type FROM transactions'" in stack

    def test_asserts_new_delete_present(self, stack: str):
        assert "grep -q 'DELETE FROM transactions WHERE id IN'" in stack

    def test_asserts_old_dangerous_form_absent(self, stack: str):
        """只证明新写法在，不能排除两种写法并存 —— 那样竞态仍然活着。"""
        assert "! grep -q 'SELECT DISTINCT pet_id'" in stack

    def test_names_docker_build_as_the_real_compile_gate(self, stack: str):
        """本机没有 go 工具链，这一点必须写明，否则下一个人以为本地验证过了。"""
        assert "就是真正的编译门" in stack


class TestRaceMechanismRecorded:
    def test_toctou_steps_recorded(self, section: str):
        assert_contains(section, "生成器又领养了 X")
        assert_contains(section, "把第 3 步那条新行一起删掉了")

    def test_why_pet_never_returns(self, section: str):
        assert_contains(section, "而重置列表就来自 transactions")

    def test_monotonic_shape_is_the_criterion(self, section: str):
        """这句话丢了，下一个人会先去调阈值而不是找累积项。"""
        assert_contains(section, "**「单调」这个形状本身就是判据**")

    def test_threshold_explanation_excluded(self, section: str):
        assert_contains(section, "50% 阈值选错了")

    def test_dead_code_ruled_out(self, section: str):
        """DropTransactions 无 WHERE 版本一度是嫌疑人，其实没有调用者。"""
        assert_contains(section, "没有任何调用者")


class TestStopBleedingIsNotRestoring:
    def test_distinction_recorded(self, section: str):
        assert_contains(section, "停止流血 ≠ 补回失血")

    def test_manual_restore_recorded(self, section: str):
        assert_contains(section, "成功 18 / 失败 0")

    def test_payload_shape_trap_recorded(self, section: str):
        assert_contains(section, "invalid json body")

    def test_stack_warns_about_the_extra_step(self, stack: str):
        assert "AfterDeployMustDo" in stack
        assert "本栈只停止流血" in stack


class TestFalseSuccessOwnUp:
    def test_the_fake_build_pass_recorded(self, section: str):
        """我写过一句「编译通过」是 && 链出来的假成功。"""
        assert_contains(section, "**假成功**")

    def test_new_image_proven_by_marker_field(self, section: str):
        assert_contains(section, "带 txnCount: 5  不带: 0")


class TestProductionAheadOfSource:
    def test_drift_recorded(self, section: str):
        # ⚠️ 不要用「一个线上有、主干没有的修复」—— 它在大标题和小节标题里
        #    各出现一次，反向验证替换掉一处时另一处仍让断言通过（实测未挂靶）。
        #    用带「7 天没人发现」的小节标题，它是唯一的。
        assert_contains(section, "主干没有的修复(7 天没人发现)")

    def test_pr_was_never_merged(self, section: str):
        assert_contains(section, "**一直是 OPEN，从未合并**")

    def test_my_own_record_was_wrong(self, section: str):
        assert_contains(section, "写成了「已合并」")

    def test_byte_identical_criterion_was_wrong(self, section: str):
        """线上是 esbuild 产物，与源码不可能逐字相同。"""
        assert_contains(section, "**那个判据是错的**")
        assert_contains(section, "esbuild 打包产物")


class TestDowngradedIsNotForgotten:
    def test_lesson_recorded(self, section: str):
        assert_contains(section, "**一个被正确降级的缺陷,不等于一个可以忘记的缺陷。**")

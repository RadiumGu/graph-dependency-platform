"""
test_109_global_tables.py — 全局表收敛后的契约 + 两个新发现缺陷的记录契约。

## 守的第一件事:那两行表名改写**不许回来**

转全局表之后，副本与源表**必须同名**，所以「按 region 改表名」这一层
整个不需要了。如果有人把 REWRITE_ENV 里那两行加回来:

  **症状不是报错** —— 而是 petfood 静默去读一张已退役但还没删的旧表，
  拿到陈旧数据。这比 ResourceNotFound 危险得多。

## 守的第二件事:D 档移出 dynamodbtablename 的**条件**

那次移动的依据是「转全局表让表名不再随 region 变化」。
如果哪天副本被删掉、回到两张独立的表，**必须把它挪回 D 档**。
这里把条件写成断言，让「前提没了但结论还在」暴露出来。

## 守的第三件事:演练代价的正确记法

4.29 原先只报「提升 57 秒 / 回切 52 秒」。真正的业务中断是**两次切换之间的
12 分 41 秒**，168 次支付全部 500。**「每步都很快」与「整件事很快」是两回事。**
这条订正不许被删。

## 守的第四件事:两个缺陷不许被写成已修

  - payforadoption 永不重建连接池 → 切换后必须重启 pod
  - 韩国没有 PetAdoptionStatusUpdater → 领养状态链路是断的
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cfn_yaml import load_cfn
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "scripts" / "gen_korea_workloads.py"
SYNC_P = ROOT / "scripts" / "sync_korea_ssm_params.py"
BACKENDS = ROOT / "infra" / "dr-korea" / "16-korea-backends.yaml"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
RUNBOOK = ROOT / "docs" / "runbooks" / "dr-korea-switchover.md"
HANDOVER = ROOT / "docs" / "runbooks" / "dr-korea-handover.md"

#: 全局表副本名 = 东京源表名
REPLICA_NAMES = {
    "ServicesEks2-ddbpetfoodfoods00C5D62B-4FH25BBOAEWX",
    "ServicesEks2-ddbpetfoodcarts77F2B0EA-1HSU365TM7EXJ",
    "ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM",
}


def _dict_keys(src: str, name: str) -> list[str]:
    """用 AST 取模块级字典的键 —— 文本匹配会撞上解释这件事的注释。"""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError(f"找不到字典 {name}")


class TestTableNameRewritesMustNotComeBack:
    def test_two_table_name_rewrites_are_gone(self):
        """**本文件最重要的一条。**

        加回来的症状不是报错，而是静默读到退役旧表里的陈旧数据。
        """
        keys = _dict_keys(GEN.read_text(encoding="utf-8"), "REWRITE_ENV")
        for gone in ("PETFOOD_FOODS_TABLE_NAME", "PETFOOD_CARTS_TABLE_NAME"):
            assert gone not in keys, (
                f"{gone} 的改写回来了 —— 那两张表已是全局表，副本与源表同名，"
                "改写会让 petfood 指向一张已退役的表（症状是陈旧数据，不是报错）"
            )

    def test_event_bus_rewrite_is_still_there(self):
        """EventBridge 总线**不是**全局资源，它的改写必须留着 —— 别一起删掉。"""
        keys = _dict_keys(GEN.read_text(encoding="utf-8"), "REWRITE_ENV")
        assert "PETFOOD_EVENT_BUS_NAME" in keys, (
            "事件总线的改写被删了 —— 它是 region 级资源，不同名，必须改写"
        )

    def test_region_rewrites_still_there(self):
        keys = _dict_keys(GEN.read_text(encoding="utf-8"), "REWRITE_ENV")
        for must in ("AWS_REGION", "S3_REGION", "PETFOOD_REGION"):
            assert must in keys, f"{must} 的改写不见了"

    def test_the_reason_is_recorded_in_the_generator(self):
        src = GEN.read_text(encoding="utf-8")
        assert_contains(src, "**两条都删掉了**")
        assert_contains(src, "All replicas in a global table share the same table name")
        assert_contains(src, "**症状不是报错**")


class TestTierDMoveHasARecordedCondition:
    def test_dynamodbtablename_moved_out(self):
        keys = _dict_keys(SYNC_P.read_text(encoding="utf-8"), "REGION_SCOPED_BARE_NAMES")
        assert "dynamodbtablename" not in keys, (
            "dynamodbtablename 回到 D 档了 —— 转全局表之后两侧同名，它不再是 region 级"
        )

    def test_the_other_tier_d_entries_remain(self):
        """只移出一项，其余 D 档条目不许顺手删。"""
        keys = _dict_keys(SYNC_P.read_text(encoding="utf-8"), "REGION_SCOPED_BARE_NAMES")
        for must in (
            "s3bucketname",
            "agent/waggleai/guardrailid",
            "agent/waggleai/memoryid",
            "agent/waggleai/nutritionkbid",
            "searchimage",
        ):
            assert must in keys, f"D 档少了 {must}"

    def test_the_reversal_condition_is_recorded(self):
        """前提没了就要挪回去 —— 条件必须写下来。"""
        src = SYNC_P.read_text(encoding="utf-8")
        assert_contains(src, "**必须把它挪回 D 档**")


class TestSupersededTablesAreMarkedNotSilentlyKept:
    def test_backends_template_marks_them_superseded(self):
        src = BACKENDS.read_text(encoding="utf-8")
        assert_contains(src, "**三张 DynamoDB 表已被取代**")
        assert_contains(src, "**已不在活动路径上**")

    def test_the_cost_of_keeping_them_is_stated(self):
        """留着旧表比删掉更危险 —— 理由要留着。"""
        src = BACKENDS.read_text(encoding="utf-8")
        assert_contains(src, "**两套表会逐渐分叉**")
        assert_contains(
            src, "「旧表里有陈旧数据」比「旧表不存在」更危险"
        )

    def test_the_declarative_regression_is_admitted(self):
        """副本的资源策略是 CLI 打的，不在 CFN 里 —— 这个退步要承认。

        ⚠️ 这条断言最初写成跨行片段，匹配不到 —— 又一次踩「断言跨行」。
        """
        src = BACKENDS.read_text(encoding="utf-8")
        assert_contains(src, "**用 CLI 打上去的**")
        assert_contains(src, "这条性质在副本上**不成立**")

    def test_tables_still_declared_until_deleted(self):
        """还没删就还得留在模板里 —— 先删模板会让它们变成无人管的孤儿。"""
        res = load_cfn(BACKENDS)["Resources"]
        for logical in ("PetFoodFoodsTable", "PetFoodCartsTable", "PetAdoptionsTable"):
            assert logical in res, f"{logical} 被从模板里删了，但物理表还没删"


class TestDrillCostRecordedCorrectly:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_the_twelve_minute_outage_is_recorded(self, record: str):
        """**只报单步耗时是误导。** 这条订正不许删。"""
        assert_contains(record, "**真的造成了 12 分 41 秒的支付中断**")
        assert_contains(record, "168 次 POST /api/completeadoption **全部 500**")

    def test_the_misleading_framing_is_admitted(self, record: str):
        assert_contains(
            record, "**那两个数字是准确的,但它们不是影响面。**"
        )
        assert_contains(
            record, "**「每步都很快」与「整件事很快」是两回事。**"
        )

    def test_the_correct_way_to_measure_is_stated(self, record: str):
        assert_contains(
            record, "**演练的代价 = 提升完成到回切开始之间的时长**"
        )

    def test_metric_channel_trust_is_per_failure_shape(self, record: str):
        """同一个 ALB 指标通道在 4.32 可信、在这里失明 —— 这个对比是关键洞察。"""
        assert_contains(
            record,
            "**指标通道的可信度是按故障\n> 形态分的,不是按通道分的。**",
        )


class TestTwoNewGapsNotMarkedFixed:
    @pytest.fixture(scope="class")
    def runbook(self) -> str:
        return RUNBOOK.read_text(encoding="utf-8")

    def test_connection_pool_restart_is_required(self, runbook: str):
        """payforadoption 永不重建 sql.DB → 必须重启 pod。

        ⚠️ 这条最初断言「永不重建数据库连接池」，那句话在**记录**里而不在手册里 ——
        我照着自己另一份文档的措辞写断言，而不是照着被守的这份。
        改成断言手册里真正那句决定性结论。
        """
        assert_contains(runbook, "**只更新内存缓存**")
        assert_contains(runbook, "全仓没有任何重建 `sql.DB` 的代码路径")
        assert_contains(runbook, "rollout restart deploy/pay-for-adoption")
        assert_contains(
            runbook,
            "**把「刷新间隔 5 分钟」当成「5 分钟后会自动指向新主库」是错的。**",
        )

    def test_the_drill_did_not_cover_this(self, runbook: str):
        """4.29 的演练没覆盖应用层 —— 这个边界要写明。"""
        assert_contains(runbook, "**4.29 的演练完全没覆盖这一层。**")

    def test_missing_status_updater_is_recorded(self, runbook: str):
        assert_contains(runbook, "韩国缺 `PetAdoptionStatusUpdater`")
        assert_contains(runbook, "**韩国零个 REST API 网关**")
        assert_contains(runbook, "**领养记进了 Aurora 但宠物状态永远不更新**")

    def test_why_it_was_missed(self, runbook: str):
        """隔两跳的依赖不在任何 Deployment 的环境变量里 —— 这是漏掉的机制。"""
        assert_contains(runbook, "**隔了两跳**")

    def test_alb_metrics_green_warning(self, runbook: str):
        assert_contains(runbook, "**靠 ALB 5xx 告警发现不了这类故障。**")
        assert_contains(runbook, "**遥测缺失 ≠ 事件未发生。**")


class TestHandoverHasTheFifthShape:
    @pytest.fixture(scope="class")
    def handover(self) -> str:
        return HANDOVER.read_text(encoding="utf-8")

    def test_fifth_shape_present(self, handover: str):
        assert_contains(handover, "**指标全绿**(最危险)")
        assert_contains(handover, "**168 次写入全部 500**")

    def test_the_irony_with_432_is_recorded(self, handover: str):
        assert_contains(
            handover, "**指标通道的可信度是按故障形态分的,不是按通道分的。**"
        )

"""
test_105_petfood_backends.py — petfood 后端的契约。

## 守的第一件事:**抄表不是只抄主键**

2026-09-25 的教训，代价是把一个好页面弄坏了:

    改动前  /FoodService  200  "Pet Food Store"   ✅
    改动后  /FoodService  200  "Error - …"        ❌  ← 我造成的回退

第一版只抄了 `KeySchema`，**没抄两个 GSI**。而报错是 ValidationException
（"The provided key element does not match the schema"），
**指向的是另一张表（carts）** —— 完全看不出真因在 foods 表缺索引。

表的契约包括:主键、属性定义、GSI/LSI、投影。
缺任何一个都可能**只在某一条查询路径上显形**。

## 守的第二件事:carts 表必须与东京一致，**不许「修好」它**

petfood 的 `get_cart` 用 `GetItem` 且只给 `user_id`
（`petfood-rs/src/repositories/cart_repository.rs:338`），
而表是复合主键 —— 必然 ValidationException。**这是应用自身的缺陷，东京也一样。**

把韩国表改成 `user_id` 单键能让 /Checkout 好起来，但那会让灾备站点的行为
与生产不一致，违背「演练的必须是同一个东西」。所以这里守着「别改」。

## 守的第三件事:两个 DynamoDB 硬约束要留在模板里

  - 单次更新只能创建/删除一个 GSI → 首次建两个索引必须分两次部署
  - AttributeDefinitions 必须**恰好**等于所有 KeySchema 用到的属性
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cfn_yaml import load_cfn
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "dr-korea" / "16-korea-backends.yaml"
GEN = ROOT / "scripts" / "gen_korea_workloads.py"
SYNC = ROOT / "scripts" / "sync_korea_ddb_items.py"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def stack() -> dict:
    return load_cfn(STACK)


@pytest.fixture(scope="module")
def stack_text() -> str:
    return STACK.read_text(encoding="utf-8")


def _table(stack: dict, logical: str) -> dict:
    return stack["Resources"][logical]["Properties"]


class TestFoodsTableCarriesTheWholeContract:
    def test_both_gsis_are_present(self, stack: dict):
        """**本文件最重要的一条。** 少了 GSI，/FoodService 渲染错误页。"""
        t = _table(stack, "PetFoodFoodsTable")
        names = {g["IndexName"] for g in t.get("GlobalSecondaryIndexes") or []}
        assert names == {"FoodTypeIndex", "PetTypeIndex"}, (
            f"GSI 不全:{names} —— petfood 查的是 GSI，"
            "少了它 /FoodService 会渲染错误页，而报错指向另一张表"
        )

    def test_gsi_keys_match_tokyo(self, stack: dict):
        """键与投影都照东京实测。"""
        t = _table(stack, "PetFoodFoodsTable")
        got = {
            g["IndexName"]: (
                [(k["AttributeName"], k["KeyType"]) for k in g["KeySchema"]],
                g["Projection"]["ProjectionType"],
            )
            for g in t["GlobalSecondaryIndexes"]
        }
        assert got["FoodTypeIndex"] == (
            [("food_type", "HASH"), ("price", "RANGE")], "ALL"
        )
        assert got["PetTypeIndex"] == (
            [("pet_type", "HASH"), ("name", "RANGE")], "ALL"
        )

    def test_attribute_definitions_exactly_match_key_usage(self, stack: dict):
        """DynamoDB 的硬约束:AttributeDefinitions 必须**恰好**等于用到的属性。

        多一个少一个都会被拒:
          "Number of attributes in KeySchema does not exactly match
           number of attributes defined in AttributeDefinitions"
        """
        for logical in ("PetFoodFoodsTable", "PetFoodCartsTable", "PetAdoptionsTable"):
            t = _table(stack, logical)
            used = {k["AttributeName"] for k in t["KeySchema"]}
            for g in t.get("GlobalSecondaryIndexes") or []:
                used |= {k["AttributeName"] for k in g["KeySchema"]}
            defined = {a["AttributeName"] for a in t["AttributeDefinitions"]}
            assert used == defined, (
                f"{logical}: KeySchema 用到 {sorted(used)}，"
                f"AttributeDefinitions 是 {sorted(defined)} —— 差集 {used ^ defined}"
            )

    def test_two_pass_deploy_constraint_is_documented(self, stack_text: str):
        assert_contains(stack_text, "必须分两次部署")
        assert_contains(stack_text, "Cannot perform more than one GSI creation")

    def test_the_regression_lesson_is_in_the_template(self, stack_text: str):
        """把好页面弄坏那件事必须留着 —— 否则有人会再删一次索引。"""
        assert_contains(stack_text, "我把一个本来好的页面弄坏了")
        assert_contains(stack_text, "指向的是**另一张表**")


class TestCartsTableMatchesWhatTheAppActuallyWrites:
    """⚠️ 2026-09-26 这个类被**有意识地反转**了，不是随手改绿。

    原先它守的是「不许把 carts 表『修好』成单键」，理由是
    「改成单键能让 /Checkout 好起来，但会让灾备站点的行为与生产不一致」。

    **那条理由的前提错了。** 当时我以为复合主键是东京生产的既定事实、
    韩国该照抄；实际上复合主键**与应用从根上不匹配**，两侧都坏，
    只是症状先撞在读上。

    决定性证据（petfood-rs/src/repositories/cart_repository.rs）:
      cart_to_item()（:108-159）写入的顶层属性**只有 4 个** ——
        user_id (S) / items (L) / created_at (S) / updated_at (S)
      **根本不写 item_id。** models/cart.rs:6-12 印证 Cart 只有 user_id
      一个标量键。**一个用户的购物车是「一行」。**

    比原判断更严重的一层:save_cart 的 PutItem（:372）**也必然失败** ——
    所以这张表**从来没被成功写入过**（实测两侧 scan 计数都是 0）。

    所以现在守的是反过来的不变量:**表键必须与应用实际写入的形状一致。**
    原来那条「别单方面偏离生产」的用意仍然有效 —— 变的是生产自己也在被修，
    两侧一起动，而不是灾备侧单方面偏离。
    """

    def test_carts_is_simple_key(self, stack: dict):
        """键必须是 user_id 单键 —— 加回 sort key 就会让读写全部 ValidationException。"""
        t = _table(stack, "PetFoodCartsTable")
        keys = [(k["AttributeName"], k["KeyType"]) for k in t["KeySchema"]]
        assert keys == [("user_id", "HASH")], (
            f"carts 表的键是 {keys} —— 应用的 cart_to_item() 从不写 item_id，"
            "任何 sort key 都会让 GetItem/PutItem/DeleteItem 全部报 "
            "ValidationException «provided key element does not match the schema»"
        )

    def test_no_stale_item_id_attribute_definition(self, stack: dict):
        """item_id 的属性定义也要一起去掉，否则 CFN 拒绝（属性必须恰好等于键用到的）。"""
        t = _table(stack, "PetFoodCartsTable")
        attrs = {a["AttributeName"] for a in t["AttributeDefinitions"]}
        assert attrs == {"user_id"}, f"属性定义应只剩 user_id，实际 {sorted(attrs)}"

    def test_the_evidence_is_recorded_in_the_template(self, stack_text: str):
        """证据必须留在模板里 —— 否则下一个人会看着复合键更「规范」而改回去。"""
        assert_contains(stack_text, "**根本不写 item_id。**")
        assert_contains(stack_text, "cart_repository.rs:108-159")
        assert_contains(stack_text, "**从来没被成功写入过**")

    def test_why_not_query_is_recorded(self, stack_text: str):
        """「改成 Query」这个错方向要写明为什么错，否则会被重新提出来。"""
        assert_contains(stack_text, "为什么不是「把读法改成 Query」")
        assert_contains(stack_text, "只接收**单个** item")

    def test_no_gsi_on_carts(self, stack: dict):
        """东京实测 carts 无 GSI/LSI —— 多加也是偏离。"""
        t = _table(stack, "PetFoodCartsTable")
        assert not t.get("GlobalSecondaryIndexes")
        assert not t.get("LocalSecondaryIndexes")


class TestAuthorizationUsesResourcePolicies:
    def test_both_tables_have_resource_policies(self, stack: dict):
        for logical in ("PetFoodFoodsTable", "PetFoodCartsTable"):
            t = _table(stack, logical)
            assert "ResourcePolicy" in t, f"{logical} 没有资源策略"

    def test_actions_copied_from_tokyo_role(self, stack: dict):
        """动作集照东京那个角色的内联策略逐条抄（实测 10 个）。"""
        expected = {
            "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:GetItem",
            "dynamodb:Scan", "dynamodb:ConditionCheckItem",
            "dynamodb:BatchWriteItem", "dynamodb:PutItem",
            "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable",
        }
        for logical in ("PetFoodFoodsTable", "PetFoodCartsTable"):
            st = _table(stack, logical)["ResourcePolicy"]["PolicyDocument"][
                "Statement"
            ][0]
            assert set(st["Action"]) == expected, f"{logical} 动作集不符"
            # 表自己的资源策略里 Resource 只能是 "*"（引用自己会循环依赖）
            assert st["Resource"] == "*"

    def test_event_bus_policy_is_put_events_only(self, stack: dict):
        st = stack["Resources"]["PetFoodEventBusPolicy"]["Properties"]["Statement"]
        assert st["Action"] == "events:PutEvents"

    def test_no_production_role_is_modified(self, stack: dict):
        """不许出现 IAM::Policy / IAM::ManagedPolicy 去挂到生产角色上。"""
        types = {r["Type"] for r in stack["Resources"].values()}
        assert "AWS::IAM::Policy" not in types, (
            "出现了 IAM::Policy —— 这会改动生产角色，"
            "而 4.26 已确立用资源策略的做法"
        )


class TestEnvRewritesCoverAllPetfoodResources:
    def test_all_four_petfood_vars_rewritten(self):
        src = GEN.read_text(encoding="utf-8")
        for name, val in (
            ("PETFOOD_REGION", "KOREA_REGION"),
            ("PETFOOD_FOODS_TABLE_NAME", '"dr-korea-petfood-foods"'),
            ("PETFOOD_CARTS_TABLE_NAME", '"dr-korea-petfood-carts"'),
            ("PETFOOD_EVENT_BUS_NAME", '"dr-korea-petfood-eventbus"'),
        ):
            assert f'"{name}": {val}' in src, f"{name} 没有被改写"

    def test_table_names_match_the_stack(self, stack: dict):
        """生成器里写的名字必须与栈里真正建的一致。

        写错的表现是运行时 ResourceNotFound，而不是部署失败。
        """
        src = GEN.read_text(encoding="utf-8")
        for logical, var in (
            ("PetFoodFoodsTable", "PETFOOD_FOODS_TABLE_NAME"),
            ("PetFoodCartsTable", "PETFOOD_CARTS_TABLE_NAME"),
        ):
            real = _table(stack, logical)["TableName"]
            assert f'"{var}": "{real}"' in src, f"{var} 与栈里的 {real} 不一致"


class TestSyncScriptDeclaresKeysPerTable:
    def test_keys_are_declared_not_inferred(self):
        src = SYNC.read_text(encoding="utf-8")
        assert '"keys": ("pettype", "petid")' in src
        assert '"keys": ("id",)' in src
        assert_contains(src, "键结构必须**逐表声明**，不能推断")

    def test_carts_is_deliberately_excluded(self):
        """购物车是用户数据，刻意不同步 —— 理由要留着。"""
        src = SYNC.read_text(encoding="utf-8")
        assert_contains(src, "刻意不同步")
        assert_contains(src, "用户数据而不是参考数据")


class TestRecordSeparatesEvidenceStrength:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_failed_control_is_not_used_as_evidence(self, record: str):
        """东京对照没做成 —— 必须写明结论不是建立在它上面。"""
        assert_contains(record, "东京的活体对照**没做成**,不能当证据")
        assert_contains(record, "建立在源码 + 两侧表结构上的")

    def test_source_citation_is_precise(self, record: str):
        assert_contains(record, "cart_repository.rs:338")

    def test_deliberate_non_divergence_is_recorded(self, record: str):
        assert_contains(record, "刻意不把韩国表改成单键")

    def test_my_regression_is_recorded(self, record: str):
        assert_contains(record, "我把一个好页面弄坏了")

    def test_truncated_stderr_cost_is_recorded(self, record: str):
        assert_contains(record, "把错误文本截掉了")

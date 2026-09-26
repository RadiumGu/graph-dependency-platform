"""
test_110_korea_statusupdater.py — 韩国 PetAdoptionStatusUpdater 的契约。

## 守的第一件事:**不许退回东京那个无认证公网写接口**

东京那个网关实测 `authorizationType: NONE` + 无 authorizer + 无 API key，
任何知道 URL 的人都能改任意宠物的 availability。韩国这个刻意加了
按来源 IP 的资源策略。

**如果有人删掉那个资源策略，症状是什么都不会发生** —— 调用方照常工作，
只是接口对全世界开放了。没有任何测试、任何页面、任何指标会变。
所以这件事只能靠门禁守。

## 守的第二件事:Lambda 的**布尔化**行为

  const availability = payload.petavailability === undefined ? 'no' : 'yes';

写成「透传 payload.petavailability」看起来更自然，但会存进 "true"/"1"，
而读取方期望 "yes"/"no"。这个错**不会报错**，只会让宠物状态显示不对。

## 守的第三件事:来源 IP 与实际 NAT 一致

EIP 被重建就会变。失效形态是**调用方 403 而领养仍然成功** ——
领养记进 Aurora、页面正常，只有宠物状态不更新。又一个静默形态。

## 守的第四件事:表名**不许**改成 dr-korea-*

那张表是全局表，副本与源表同名。改成 dr-korea-petadoptions 会指向一张
已退役的旧表（症状是陈旧数据，不是报错）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfn_yaml import load_cfn, param_default
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "dr-korea" / "19-korea-statusupdater.yaml"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"

#: 全局表名 —— 两侧同名，刻意与东京一致
GLOBAL_TABLE = "ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM"
#: 韩国 VPC 唯一 NAT 的 EIP
KOREA_NAT_EIP = "3.37.176.45/32"


@pytest.fixture(scope="module")
def stack() -> dict:
    return load_cfn(STACK)


@pytest.fixture(scope="module")
def src() -> str:
    return STACK.read_text(encoding="utf-8")


class TestApiIsNotAnOpenWriteSurface:
    def test_resource_policy_exists(self, stack: dict):
        """**本文件最重要的一条。**

        删掉它不会有任何可观测症状 —— 调用方照常工作，
        接口只是对全世界开放了。
        """
        props = stack["Resources"]["UpdaterApi"]["Properties"]
        assert "Policy" in props, (
            "网关没有资源策略 —— 那就退回成了东京那个无认证公网写接口，"
            "任何人都能改任意宠物的 availability，而且不会有任何症状"
        )

    def test_policy_has_both_allow_and_explicit_deny(self, stack: dict):
        """只写 Allow 也能拦住，但显式 Deny 让意图不依赖「默认拒绝」这个隐含前提。"""
        sts = stack["Resources"]["UpdaterApi"]["Properties"]["Policy"]["Statement"]
        effects = [st["Effect"] for st in sts]
        assert "Allow" in effects and "Deny" in effects, (
            f"资源策略只有 {set(effects)} —— 需要 Allow + 显式 Deny 两条"
        )

    def test_conditions_are_ip_scoped(self, stack: dict):
        sts = stack["Resources"]["UpdaterApi"]["Properties"]["Policy"]["Statement"]
        allow = next(st for st in sts if st["Effect"] == "Allow")
        deny = next(st for st in sts if st["Effect"] == "Deny")
        assert "IpAddress" in allow["Condition"], "Allow 没有按来源 IP 限制"
        assert "NotIpAddress" in deny["Condition"], "Deny 没有用 NotIpAddress"

    def test_source_ip_matches_korea_nat(self, stack: dict):
        """EIP 被重建就会变 —— 失效形态是调用方 403 而领养仍然成功。"""
        assert param_default(stack, "AllowedSourceIp") == KOREA_NAT_EIP, (
            f"来源 IP 与实测的韩国 NAT EIP {KOREA_NAT_EIP} 不一致。"
            "NAT 被重建过就要一起改，否则调用方 403 而领养仍会成功"
            "（宠物状态静默不更新）"
        )

    def test_the_deviation_is_documented(self, src: str):
        """偏离东京这件事要写明理由，否则会被当成不一致而「修正」回去。"""
        assert_contains(src, "**我不在首尔原样复制一个新的无认证公网写入面。**")
        assert_contains(src, "**不签 SigV4**")

    def test_the_boundary_is_documented(self, src: str):
        """按 IP 限制不是强认证 —— 边界必须写下来。"""
        assert_contains(src, "它是**网络层**限制，不是身份认证")
        assert_contains(src, "而**领养仍然会成功**")


class TestLambdaBehaviourMatchesTokyoExactly:
    @pytest.fixture(scope="class")
    def code(self, stack: dict) -> str:
        return stack["Resources"]["UpdaterFunction"]["Properties"]["Code"]["ZipFile"]

    def test_availability_is_booleanised_not_passed_through(self, code: str):
        """**透传会存进 "true"/"1"，而读取方期望 "yes"/"no"** —— 且不会报错。"""
        assert "payload.petavailability === undefined ? 'no' : 'yes'" in code, (
            "布尔化逻辑变了。东京那个 Lambda 是:带了 petavailability（任何值）"
            "就写 'yes'，完全不带才写 'no'。透传不会报错，只会让状态显示不对"
        )

    def test_both_key_parts_are_given(self, code: str):
        """主键两个都要给 —— 少一个就是 ValidationException。"""
        assert "pettype: { S: payload.pettype }" in code
        assert "petid: { S: payload.petid }" in code

    def test_only_availability_is_updated(self, code: str):
        assert "set availability = :r" in code

    def test_uses_client_dynamodb_not_lib_dynamodb(self, code: str):
        """内联代码不能带依赖 —— lib-dynamodb 不一定随运行时自带。"""
        assert "@aws-sdk/client-dynamodb" in code
        assert "lib-dynamodb" not in code

    def test_runtime_matches_tokyo(self, stack: dict):
        p = stack["Resources"]["UpdaterFunction"]["Properties"]
        assert p["Runtime"] == "nodejs22.x"
        assert p["Architectures"] == ["arm64"]
        assert p["MemorySize"] == 128
        assert p["Timeout"] == 3


class TestTableNameStaysTheGlobalTableName:
    def test_table_name_is_the_shared_global_name(self, stack: dict):
        """改成 dr-korea-* 会指向一张已退役的旧表 —— 症状是陈旧数据，不是报错。"""
        assert param_default(stack, "TableName") == GLOBAL_TABLE, (
            f"表名应当是全局表名 {GLOBAL_TABLE}（两侧同名）。"
            "改成 dr-korea-petadoptions 会读到一张已退役旧表里的陈旧数据"
        )

    def test_why_it_is_the_same_as_tokyo_is_explained(self, src: str):
        """值与东京相同看起来像抄错了 —— 必须写明这是刻意的。"""
        assert_contains(src, "这个值与东京**完全相同**，不是写错了")
        assert_contains(
            src, "All replicas in a global table share the same table name"
        )


class TestRoleIsScopedNotFullAccess:
    def test_no_full_access_managed_policies(self, stack: dict):
        arns = stack["Resources"]["UpdaterRole"]["Properties"]["ManagedPolicyArns"]
        joined = " ".join(arns)
        assert "FullAccess" not in joined, (
            f"角色挂了 FullAccess 策略:{arns} —— 一个只改一个字段的 3 秒 Lambda 不需要"
        )

    def test_inline_policy_is_update_item_on_one_table(self, stack: dict):
        pol = stack["Resources"]["UpdaterRole"]["Properties"]["Policies"][0]
        st = pol["PolicyDocument"]["Statement"][0]
        assert st["Action"] == "dynamodb:UpdateItem", (
            f"内联策略的动作是 {st['Action']} —— 应当只有 UpdateItem"
        )
        assert "${TableName}" in json.dumps(st["Resource"]) or GLOBAL_TABLE in json.dumps(
            st["Resource"]
        ), "Resource 没有限定到那一张表"


class TestStageNameIsProd:
    def test_stage_is_prod(self, stack: dict):
        """调用方拼的 URL 里带 /prod/ —— 改 stage 名会让它 404（而领养仍成功）。"""
        assert stack["Resources"]["ProdStage"]["Properties"]["StageName"] == "prod"

    def test_output_url_ends_with_slash(self, src: str):
        """结尾斜杠很重要:调用方 PUT 的是根 URL。"""
        assert "/prod/'" in src


class TestRecordOwnsTheProductionSideEffect:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_the_junk_row_is_recorded(self, record: str):
        """为做对照往生产表写了一条垃圾行 —— 这件事必须留着。"""
        assert_contains(record, "为做对照往生产表写了一条垃圾行")
        assert_contains(record, "NONEXISTENT-probe-do-not-use")

    def test_the_lesson_is_recorded(self, record: str):
        assert_contains(
            record,
            "**「演示一个漏洞存在」和「利用它」之间只差一次请求**",
        )
        assert_contains(record, "那已经足够证明它是开放的")

    def test_tokyo_open_endpoint_recorded_as_debt(self, record: str):
        """东京那个开放接口没动 —— 作为已知安全债留档，不许写成已修。"""
        assert_contains(record, "**东京那个开放接口我没有动**")

    def test_global_table_side_effect_recorded(self, record: str):
        """同名全局表意味着灾备侧不是隔离副本 —— 这个副作用要写明。"""
        assert_contains(
            record, "**「同名全局表」意味着灾备侧不是隔离副本**"
        )

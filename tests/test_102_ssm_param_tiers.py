"""
test_102_ssm_param_tiers.py — /petstore 参数分档的契约。

## 守的第一件事:**按值的形状分类会漏掉一整类**

这 6 个参数的值里**不含 region 字样**（就是个裸名字或裸 ID）：

    dynamodbtablename              ServicesEks2-ddbpetadoption7B7CFEC9-…
    s3bucketname                   serviceseks2-s3bucketpetadoptioncb20dce5-…
    agent/waggleai/guardrailid     u4jw0mo0r7pq
    agent/waggleai/memoryid        WaggleAIMemory-HuA0HS92Yd
    agent/waggleai/nutritionkbid   QOWP8XIMMU
    searchimage                    petsearch-java:latest

任何「值里有 ap-northeast-1 才算 region 绑定」的判据都会把它们放进
「可直接复制」，而它们指向的资源全是 region 级的 —— 照抄过去的表现是
运行时 `ResourceNotFound`。

所以脚本里这一档必须是**手工列表**，而这个文件守那份列表不被改成推断。

## 守的第二件事:SecureString 的值不许被读写

4 个 dataprotection 密钥是 SecureString。脚本不读也不写它们 ——
密钥环怎么处理是一个明确的 DR 决定（见下），不是脚本的副作用。

## 守的第三件事:演练制造的密钥环分叉必须记着

4.21 那次演练里 petsite 自己在韩国生成了一个新的 Data Protection 密钥
（`key-846044d6…`，改动者是 PetSite 的 SA 角色）。东京 4 个、韩国 1 个。
切换后东京签发的 cookie 与防伪令牌在韩国**验不过**，而这不会告警。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_korea_ssm_params.py"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("syncmod", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(m)
    return m


class TestBareNamedRegionScopedParamsAreListedByHand:
    """值里看不出 region 的那一档必须是显式列表，不能靠推断。"""

    #: ⚠️ 2026-09-26 `dynamodbtablename` **从这里移出去了**，不是漏掉。
    #
    #  原因:那张表已转成 DynamoDB 全局表，而全局表副本**必须与源表同名**
    #  （文档 V2globaltables_HowItWorks: "All replicas in a global table
    #  share the same table name"）。所以两个 region 的值**逐字相同** ——
    #  它不再具备「D 档」的定义性质（值里看不出它是 region 级的）。
    #
    #  这条移动的前提是**副本存在**。见下面 test_tier_d_move_is_conditional。
    EXPECTED = {
        "s3bucketname",
        "agent/waggleai/guardrailid",
        "agent/waggleai/memoryid",
        "agent/waggleai/nutritionkbid",
        "searchimage",
    }

    #: 移出去的那些，以及移出的前提。前提不成立就必须挪回来。
    MOVED_OUT = {
        "dynamodbtablename": "已转 DynamoDB 全局表，副本与源表同名",
    }

    def test_all_listed(self, mod):
        listed = set(mod.REGION_SCOPED_BARE_NAMES)
        missing = self.EXPECTED - listed
        assert not missing, (
            f"这些参数的值里没有 region 字样，漏掉就会被当成可移植复制过去，"
            f"运行时 ResourceNotFound:{missing}"
        )

    def test_moved_out_ones_are_not_listed(self, mod):
        """移出去的不许悄悄回来 —— 回来说明有人没读为什么移的。"""
        listed = set(mod.REGION_SCOPED_BARE_NAMES)
        back = set(self.MOVED_OUT) & listed
        assert not back, (
            f"{back} 回到 D 档了。它们是因为转成全局表（副本与源表同名）才移出的；"
            "如果副本真的被删了，改这份测试并说明，不要只改脚本"
        )

    def test_tier_d_move_is_conditional(self, src: str):
        """移出的**前提**必须写在脚本里 —— 前提没了要挪回去。

        失效形态很隐蔽:副本被删之后表名又变成 region 级的，
        而参数仍按「逐字复制」处理 → 韩国会去读**东京那张表**，
        在真灾难时那张表不可达。
        """
        assert_contains(src, "**必须把它挪回 D 档**")
        assert_contains(
            src, "All replicas in a global table share the same table name"
        )

    def test_each_says_what_resource_is_needed(self, mod):
        """光列名字不够 —— 要说明需要哪种韩国资源，否则估不出工作量。"""
        for name, why in mod.REGION_SCOPED_BARE_NAMES.items():
            assert why and len(why) > 2, f"{name} 没写需要什么资源"

    def test_the_trap_is_explained_in_the_script(self, src: str):
        # ⚠️ 断言不能跨行:脚本里这句话被换行拆成了两行
        #    （"…值里不含 region\n字样**（就是个裸名字…"）。
        #    这是第 11 次「判据没照抄实现」，这次的形态是**忽略了换行**。
        assert_contains(src, "就是个裸名字或裸 ID")
        assert_contains(src, "ResourceNotFound")
        assert_contains(src, "按值形状」会把它们判成可移植")

    def test_tier_function_does_not_infer_this_class(self, src: str):
        """判据查**结构**:tier_of 必须先查手工列表，再做形状推断。

        顺序反了的话，`searchimage: petsearch-java:latest` 这种值会先被
        形状规则吃掉（它不含 ap-northeast-1），于是永远进不了 D 档。
        """
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "tier_of"
        )
        body = ast.get_source_segment(src, fn) or ""
        i_manual = body.find("REGION_SCOPED_BARE_NAMES")
        i_shape = body.find("svc.cluster.local")
        assert i_manual != -1 and i_shape != -1
        assert i_manual < i_shape, (
            "手工列表的检查必须排在形状推断之前，否则裸名字参数会被形状规则吃掉"
        )


class TestSecureStringsAreNeverReadOrWritten:
    def test_secure_strings_go_to_their_own_tier(self, mod):
        tier, _ = mod.tier_of("/petstore/dataprotection/key-x", "SecureString", "")
        assert tier == "F", "SecureString 必须单独一档"

    def test_script_says_it_does_not_touch_them(self, src: str):
        assert_contains(src, "不读也不写")

    def test_writable_tiers_exclude_f(self, src: str):
        """真正写入的只有 A/B/C 三档 —— F 不许进去。"""
        assert 'buckets.get("A", []) + buckets.get("B", []) + buckets.get("C", [])' in src
        # 写入时类型固定 String，不会误把 SecureString 降级成明文
        assert '"--type", "String"' in src


class TestClusterInternalDnsIsTreatedAsPortable:
    def test_svc_cluster_local_is_tier_a(self, mod):
        tier, why = mod.tier_of(
            "/petstore/searchapiurl", "String",
            "http://search-service.petadoptions.svc.cluster.local/api/search",
        )
        assert tier == "A", "集群内 DNS 应当判为可移植"
        assert_contains(why, "与 region 无关")

    def test_tokyo_arn_is_not_portable(self, mod):
        tier, _ = mod.tier_of(
            "/petstore/snsarn", "String",
            "arn:aws:sns:ap-northeast-1:926093770964:topic",
        )
        assert tier == "E"

    def test_korea_override_params_are_tier_c(self, mod):
        for name in ("rdsendpoint", "rds-reader-endpoint", "pethistoryrepositoryuri"):
            tier, _ = mod.tier_of(f"/petstore/{name}", "String", "whatever")
            assert tier == "C", f"{name} 应当是「写韩国值」那一档"


class TestScriptRefusesToRunOnChangedInventory:
    def test_warns_when_param_count_differs(self, src: str):
        """东京参数数量变了 → 分档依据可能过期，必须提示而不是盲目写。"""
        assert "!= 41" in src
        assert_contains(src, "分档依据可能过期")


class TestRecordKeepsTheDataProtectionFinding:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_keyring_divergence_recorded(self, record: str):
        assert_contains(record, "演练本身制造了一个数据保护密钥环分叉")
        assert_contains(record, "key-846044d6")
        # 后果要写清楚 —— 否则读者不知道为什么要在意
        assert_contains(record, "用户被登出")
        assert_contains(record, "不会有任何告警")

    def test_pethistory_root_cause_marked_as_inferred(self, record: str):
        """四个测量支持，但**未直接观测** —— 不许写成已确定。"""
        assert_contains(record, "根因未直接观测到")
        assert_contains(record, "四个测量互相支持")

    def test_fix_direction_is_korea_local_not_cross_region(self, record: str):
        """修法不是「让韩国连上东京」—— 真灾难时东京就是没了。"""
        assert_contains(record, "真灾难时东京就是没了")

    def test_petsite_fix_not_claimed_verified(self, record: str):
        """写了 19 个参数 ≠ petsite 就能渲染。不许把未验证写成已修好。"""
        assert_contains(record, "这一节不声称已修好")

    def test_pagination_trap_recorded(self, record: str):
        assert_contains(record, "按页各算一次")
        assert_contains(record, "存在性检查要用退出码")

    def test_remaining_resources_have_cost_estimates(self, record: str):
        assert_contains(record, "剩下的需要建什么")
        assert_contains(record, "需向量库")

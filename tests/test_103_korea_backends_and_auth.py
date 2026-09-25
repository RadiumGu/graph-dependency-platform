"""
test_103_korea_backends_and_auth.py — 韩国后端资源与密钥授权的契约。

## 守的第一件事:授权必须覆盖**全部 7 个业务角色**

2026-09-25 的教训。资源策略第一版只授权了 pethistory 一个角色，结果:

    7/7 Deployment 就绪          ✅
    pethistory 日志正常           ✅
    首页渲染正确                  ✅
    /PetListAdoptions            ❌ 挂满 60 秒后 ALB 返回 504

list-adoptions 撞的是**同一个** AccessDenied，但它的表现不是启动失败
而是**请求超时** —— 从「7/7 就绪」和任何 pod 级检查里完全看不出来。

**这次的新形态:缺陷只在某一条请求路径上显形，而那条路径不在任何健康检查里。**

## 守的第二件事:授权走**资源策略**，不改生产角色

官方文档（secretsmanager/latest/userguide/auth-and-access_resource-policies.html）:
"you can attach policies to secrets **or** identities" —— 两条路等价。
选资源策略的理由:不动东京 Applications 栈建的角色、授权与资源同生共死、
范围天然最小。

## 守的第三件事:托管服务的选用结论不许被悄悄改掉

用户要求「尽量用 AWS 托管服务」。查清的结论:

  - Secrets Manager **跨 region 复制不适用** —— 副本是只读同值副本，
    而 pethistory 从密钥里读 host（源码 config.py），副本的 host 会留在东京
  - **RDS 托管主密码官方明确不支持** Aurora 全局数据库集群

这两条都是有出处的，不是偏好。记录里必须留着出处，否则下一个人会重试一遍。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfn_yaml import load_cfn
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "dr-korea" / "16-korea-backends.yaml"
MAPPING = ROOT / "infra" / "dr-korea" / "irsa-korea-mapping.json"
GEN = ROOT / "scripts" / "gen_korea_workloads.py"
SECRET_SCRIPT = ROOT / "scripts" / "create_korea_db_secret.py"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def stack() -> dict:
    return load_cfn(STACK)


@pytest.fixture(scope="module")
def stack_text() -> str:
    return STACK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def business_roles() -> set[str]:
    m = json.loads(MAPPING.read_text(encoding="utf-8"))
    return {e["role"] for e in m["service_accounts"] if e["kind"] == "business"}


class TestSecretPolicyCoversEveryBusinessRole:
    def test_all_seven_roles_are_principals(self, stack: dict, business_roles: set[str]):
        """**本文件最重要的一条。**

        少授权一个的表现不是启动失败，而是**某条请求路径超时**，
        从 Deployment 就绪状态里看不见。
        """
        sts = stack["Resources"]["KoreaDbSecretPolicy"]["Properties"]["ResourcePolicy"][
            "Statement"
        ]
        principals: list[str] = []
        for st in sts:
            p = st["Principal"]["AWS"]
            principals += [p] if isinstance(p, str) else p
        # CFN 里是 !Sub 结构，取角色名部分
        named = {str(x).rsplit("/", 1)[-1].rstrip("'") for x in principals}
        missing = business_roles - named
        assert not missing, (
            f"这些业务角色没被授权读韩国 DB 密钥:{missing}。"
            "少授权一个的后果是那个服务的请求路径 504，而 Deployment 仍然 7/7 就绪"
        )

    def test_count_matches_mapping(self, stack: dict, business_roles: set[str]):
        """数量对不上就说明映射变了而这里没跟着改。

        ⚠️ 2026-09-25 修正:这里原先把**所有**语句的 principal 加在一起跟 7 比。
        那个算法把「业务角色齐不齐」和「一共有几个消费者」混成了一条断言，
        于是切换演练加第 8 个消费者（Temporal worker，非业务角色）时它就挂了 ——
        而真正的不变量并没有被破坏。

        现在分成两条:业务语句必须恰好是那 7 个；额外语句必须在**白名单**里。
        白名单的作用是让「刻意新增」与「顺手加进来」区分开 ——
        不写进白名单的新消费者仍然会挂。
        """
        biz = self._business_statement(stack)
        principals = biz["Principal"]["AWS"]
        principals = [principals] if isinstance(principals, str) else principals
        assert len(principals) == len(business_roles) == 7, (
            f"业务语句里有 {len(principals)} 个 principal，映射里有 "
            f"{len(business_roles)} 个 —— 对不上说明映射变了而这里没跟着改"
        )

    #: 刻意新增的**非业务**消费者。新增一个就要在这里登记，并说明理由。
    DELIBERATE_EXTRA_SIDS = {
        # 切换演练要求提升数据库后核实韩国真的可写；没有这条权限那一步
        # 只能记 inconclusive，而「没测到」与「坏了」表现完全一样。
        "AllowDrWorkerReadForDrill",
    }

    def test_extra_consumers_are_declared(self, stack: dict):
        """非业务消费者必须登记，顺手加进来的会被挂住。"""
        sts = stack["Resources"]["KoreaDbSecretPolicy"]["Properties"]["ResourcePolicy"][
            "Statement"
        ]
        extra = {st["Sid"] for st in sts} - {"AllowPetsiteAppRolesRead"}
        undeclared = extra - self.DELIBERATE_EXTRA_SIDS
        assert not undeclared, (
            f"密钥资源策略里出现了未登记的消费者 {undeclared} —— "
            "新增消费者要写进 DELIBERATE_EXTRA_SIDS 并说明理由"
        )

    @staticmethod
    def _business_statement(stack: dict) -> dict:
        sts = stack["Resources"]["KoreaDbSecretPolicy"]["Properties"]["ResourcePolicy"][
            "Statement"
        ]
        return next(st for st in sts if st["Sid"] == "AllowPetsiteAppRolesRead")

    def test_action_is_only_get_secret_value(self, stack: dict):
        """最小权限:只给读，不给改。"""
        sts = stack["Resources"]["KoreaDbSecretPolicy"]["Properties"]["ResourcePolicy"][
            "Statement"
        ]
        for st in sts:
            acts = st["Action"]
            acts = [acts] if isinstance(acts, str) else acts
            assert acts == ["secretsmanager:GetSecretValue"], f"动作过宽:{acts}"

    def test_the_504_lesson_is_in_the_template(self, stack_text: str):
        """那条教训必须留在模板里 —— 否则下一个人会以为只授权一个就够。"""
        assert_contains(stack_text, "ALB 返回 504")
        assert_contains(stack_text, "又一次「全绿但不能服务」")


class TestSecretValueNeverEntersTheTemplate:
    def test_no_secret_resource_with_inline_value(self, stack: dict):
        """密钥的值不许出现在模板里。"""
        for name, res in stack["Resources"].items():
            if res["Type"] == "AWS::SecretsManager::Secret":
                props = res.get("Properties") or {}
                assert "SecretString" not in props, (
                    f"{name} 把密钥值写进了模板 —— 等于提交进仓库"
                )

    def test_template_explains_why_value_is_scripted(self, stack_text: str):
        assert_contains(stack_text, "把密码写进模板就等于提交进仓库")

    def test_script_never_prints_password(self):
        """脚本必须有脱敏机制，且把 password 列进敏感字段。"""
        src = SECRET_SCRIPT.read_text(encoding="utf-8")
        assert "def redact(" in src
        assert 'SENSITIVE = ("password",)' in src
        assert_contains(src, "全程不打印密码")


class TestManagedServiceFindingsAreRecorded:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_replication_not_applicable_with_reason(self, record: str):
        """跨 region 复制为什么不适用 —— 要留出处，否则会被重试一遍。"""
        assert_contains(record, "副本是只读同值副本")
        assert_contains(record, "从密钥里读 `host`")

    def test_rds_managed_password_unsupported_with_citation(self, record: str):
        assert_contains(record, "rds-secrets-manager.html")
        assert_contains(record, "part of an Aurora global database")

    def test_resource_policy_choice_is_cited(self, record: str):
        assert_contains(record, "auth-and-access_resource-policies.html")
        # 照抄记录里的真实措辞（写的是「不动东京 Applications 栈建的那 7 个角色」）
        assert_contains(record, "不动东京 `Applications` 栈建的那 7 个角色")


class TestEnvFixIsGroundedInSource:
    def test_generator_drops_rather_than_rewrites(self):
        """删 vs 改写的区别来自源码，不是感觉。"""
        import ast

        src = GEN.read_text(encoding="utf-8")
        # ⚠️ 判据用 AST 查**元组成员**，不用子串匹配。
        #    第一版写的是 `assert "RDS_SECRET_ARN" in src`，反向验证把条目
        #    改名成 `_RDS_SECRET_ARN` 之后**门禁挂靶 0** —— 子串照样匹配到了。
        #    这是本会话第 12 次「判据没照抄实现」，形态是**子串误配**。
        tree = ast.parse(src)
        dropped: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "DROP_ENV_FOR_PARAM_STORE_FALLBACK"
                for t in node.targets
            ):
                continue
            dropped = {
                e.value for e in node.value.elts if isinstance(e, ast.Constant)
            }
        assert "RDS_SECRET_ARN" in dropped, (
            f"DROP_ENV_FOR_PARAM_STORE_FALLBACK = {dropped or '（找不到这个常量）'} —— "
            "不删这个环境变量，应用就不会回落到韩国的参数存储"
        )
        assert "UPDATE_ADOPTION_URL" in dropped
        assert_contains(src, "应用**本来就支持**从 Parameter Store 取配置")

    def test_region_env_is_rewritten_not_dropped(self):
        """AWS_REGION 必须改写 —— 删掉会让回落读到东京的参数。"""
        src = GEN.read_text(encoding="utf-8")
        assert '"AWS_REGION": KOREA_REGION' in src
        assert_contains(src, "它决定 boto3 去哪个 region 读")

    def test_generated_manifest_has_no_tokyo_region_env(self):
        """产出物核对:清单里不许再有指向东京的 region 环境变量。"""
        import yaml

        out = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
        docs = [d for d in yaml.safe_load_all(out.read_text(encoding="utf-8")) if d]
        for d in docs:
            if d["kind"] != "Deployment":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                for e in c.get("env") or []:
                    if e.get("name") in ("AWS_REGION", "S3_REGION"):
                        assert e.get("value") == "ap-northeast-2", (
                            f"{d['metadata']['name']}/{c['name']} 的 {e['name']} "
                            f"还是 {e.get('value')}"
                        )
                    assert e.get("name") not in ("RDS_SECRET_ARN", "UPDATE_ADOPTION_URL"), (
                        f"{d['metadata']['name']} 还带着 {e['name']} —— "
                        "应用不会回落到韩国的参数存储"
                    )


class TestBackendResourcesAreIdleCheap:
    def test_dynamodb_is_pay_per_request(self, stack: dict):
        """守夜灯平时零流量 —— 预置容量要一直付钱。"""
        t = stack["Resources"]["PetAdoptionsTable"]["Properties"]
        assert t["BillingMode"] == "PAY_PER_REQUEST"

    def test_key_schema_matches_tokyo(self, stack: dict):
        """键结构照东京实测:HASH pettype + RANGE petid。"""
        t = stack["Resources"]["PetAdoptionsTable"]["Properties"]
        assert [(k["AttributeName"], k["KeyType"]) for k in t["KeySchema"]] == [
            ("pettype", "HASH"),
            ("petid", "RANGE"),
        ]

    def test_bucket_blocks_public_access(self, stack: dict):
        b = stack["Resources"]["PetAdoptionsBucket"]["Properties"]
        pab = b["PublicAccessBlockConfiguration"]
        assert all(pab[k] is True for k in pab), "灾备桶不该有任何公开访问口子"
        assert b["BucketEncryption"]

    def test_stateful_resources_are_retained(self, stack: dict):
        """删栈不该带走数据。"""
        for name in ("PetAdoptionsTable", "PetAdoptionsBucket"):
            assert stack["Resources"][name].get("DeletionPolicy") == "Retain"


class TestRecordKeepsWhatWorksAndWhatDoesNot:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_page_titles_are_the_criterion(self, record: str):
        """判据是页面标题，不是状态码 —— 错误页也返回 200。"""
        assert_contains(record, "判据是页面标题，不是状态码")
        assert_contains(record, "Pet Adoption List")
        assert_contains(record, "Pet Food Store")

    def test_checkout_still_broken_is_recorded(self, record: str):
        """还不能用的那个必须写明，不许只报好消息。"""
        assert_contains(record, "/Checkout` 仍渲染错误页")
        assert_contains(record, "StepFnlambdastep")

    def test_unused_target_state_explained(self, record: str):
        """`unused` 是正确状态 —— 不解释会被当成故障。"""
        assert_contains(record, "`unused` —— 那是**正确**的")

    def test_fast_failure_improvement_noted(self, record: str):
        assert_contains(record, "快速失败比静默挂住好得多")

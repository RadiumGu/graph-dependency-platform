"""
test_100_korea_ingress_contract.py — 韩国入口链路的契约。

## 这个文件守三件在 2026-09-25 付了代价才知道的事

### ① 目标组 ARN 是 region 专属的

TargetGroupBinding 里写东京的目标组 ARN **不会报错**:清单过 YAML 校验、
pod 起得来、controller 也不抱怨,**只是永远没有流量**。
这正是手册第五节② 说「静态检查发现不了」的那个缺陷,所以这里用门禁补上。

### ② 东京的 petsite 健康检查配置是错的,不能照抄

    东京 Servic-PetSi-7JEWC19HNKSR   健康检查 = GET / 期望 200
    实测                             0/2 healthy，Target.ResponseCodeMismatch

原因是 petsite 在 `/` 上做会话分配重定向（`Location: /?userId=…`），永远不是 200。
韩国刻意改用 `/health/status`。**这处偏离必须带着理由留在模板里**,
否则下一个人会「修正」成与东京一致,然后韩国的目标永远 unhealthy。

### ③ 缩容到零会死锁 30 分钟

coredns 的 PDB（maxUnavailable=1，2 副本）在只剩一个节点时永远无法满足,
节点卡在 `Terminating:Wait` 直到 1800 秒钩子超时。
因果验证过:降到 1 副本后 2 分钟内就终止了。

## 判据纪律

中文断言走 `zh_text.assert_contains`（标点归一化）—— 手抄标点已经害过两次。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cfn_yaml import load_cfn, param_default
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
ALB_STACK = ROOT / "infra" / "dr-korea" / "12-korea-alb.yaml"
LBC_VALUES = ROOT / "infra" / "dr-korea" / "13-lbc-values.yaml"
INGRESS = ROOT / "infra" / "dr-korea" / "14-petsite-korea-ingress.yaml"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"

KOREA = "ap-northeast-2"
TOKYO = "ap-northeast-1"


@pytest.fixture(scope="module")
def alb() -> dict:
    return load_cfn(ALB_STACK)


@pytest.fixture(scope="module")
def alb_text() -> str:
    return ALB_STACK.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ingress_docs() -> list[dict]:
    """14-… 是多文档 YAML（Service + TargetGroupBinding）。"""
    import yaml

    return [d for d in yaml.safe_load_all(INGRESS.read_text(encoding="utf-8")) if d]


class TestTargetGroupArnIsKoreaNotTokyo:
    def test_tgb_uses_korea_target_group(self, ingress_docs: list[dict]):
        """**本文件最重要的一条。**

        照抄东京的 ARN 不会报错，只会永远没有流量。
        """
        tgb = next(d for d in ingress_docs if d["kind"] == "TargetGroupBinding")
        arn = tgb["spec"]["targetGroupARN"]
        assert f":{KOREA}:" in arn, f"目标组 ARN 不是韩国的:{arn}"
        assert f":{TOKYO}:" not in arn, (
            "TargetGroupBinding 指向东京的目标组 —— 清单会通过校验、pod 会起来，"
            "但永远没有流量。这是静态检查发现不了的那类缺陷。"
        )
        assert "dr-korea-petsite-tg" in arn

    def test_tgb_target_type_is_ip(self, ingress_docs: list[dict]):
        tgb = next(d for d in ingress_docs if d["kind"] == "TargetGroupBinding")
        assert tgb["spec"]["targetType"] == "ip"

    def test_service_ref_port_is_service_port_not_target_port(
        self, ingress_docs: list[dict]
    ):
        """serviceRef.port 写的是 Service 的端口（80），不是目标组端口（8080）。

        写成 8080 的表现是 controller 找不到 Endpoints —— 目标组永远是空的。
        """
        svc = next(d for d in ingress_docs if d["kind"] == "Service")
        tgb = next(d for d in ingress_docs if d["kind"] == "TargetGroupBinding")
        svc_port = svc["spec"]["ports"][0]["port"]
        assert tgb["spec"]["serviceRef"]["port"] == svc_port == 80
        # 而 targetPort 是容器端口
        assert svc["spec"]["ports"][0]["targetPort"] == 8080

    def test_manifest_warns_about_region_specific_arn(self):
        text = INGRESS.read_text(encoding="utf-8")
        assert_contains(text, "目标组 ARN 是 region 专属的")
        assert_contains(text, "只是永远没有流量")


class TestHealthCheckDeviatesFromTokyoOnPurpose:
    def test_petsite_health_check_is_not_root(self, alb: dict):
        """不能是 `/` —— 东京那份实测 0/2 healthy。"""
        tg = alb["Resources"]["PetsiteTargetGroup"]["Properties"]
        assert tg["HealthCheckPath"] == "/health/status", (
            f"健康检查路径是 {tg['HealthCheckPath']} —— 用 `/` 会因为 petsite "
            "的会话重定向（Location: /?userId=…）永远 ResponseCodeMismatch"
        )

    def test_deviation_is_documented_with_evidence(self, alb_text: str):
        """偏离必须带理由，否则会被「修正」回东京那份错的。"""
        assert_contains(alb_text, "**不照抄东京的 `/`**")
        assert_contains(alb_text, "0/2 healthy")
        assert_contains(alb_text, "Target.ResponseCodeMismatch")

    def test_warns_that_healthy_target_is_not_a_working_app(self, alb_text: str):
        """「目标 healthy」不等于「应用能服务」—— 这一条必须留着。"""
        assert_contains(alb_text, "不证明应用能服务用户")

    def test_both_target_groups_are_ip_type(self, alb: dict):
        for name in ("PetsiteTargetGroup", "PethistoryTargetGroup"):
            assert alb["Resources"][name]["Properties"]["TargetType"] == "ip"


class TestAlbStaysInternal:
    def test_scheme_is_internal(self, alb: dict):
        """用户明确「韩国先不暴露公网」。改成 internet-facing 要先问。"""
        assert alb["Resources"]["Alb"]["Properties"]["Scheme"] == "internal"

    def test_ingress_cidr_is_vpc_not_world(self, alb: dict):
        sg = alb["Resources"]["AlbSecurityGroup"]["Properties"]
        for rule in sg["SecurityGroupIngress"]:
            cidr = str(rule.get("CidrIp", ""))
            assert "0.0.0.0/0" not in cidr, "internal ALB 放行了公网"
        assert param_default(alb, "VpcCidr") == "10.20.0.0/16"

    def test_security_group_descriptions_are_ascii(self, alb: dict):
        """EC2 安全组的描述字段只接受 ASCII，而且不含 `>`。

        实测两次部署失败:
          GroupDescription → Character sets beyond ASCII are not supported
          rule Description → Invalid rule description（允许集不含 `>`）
        """
        res = alb["Resources"]
        fields = [res["AlbSecurityGroup"]["Properties"]["GroupDescription"]]
        for r in res["AlbSecurityGroup"]["Properties"]["SecurityGroupIngress"]:
            fields.append(r.get("Description", ""))
        for k in ("AllowAlbToPetsitePods", "AllowAlbToPethistoryPods"):
            fields.append(res[k]["Properties"].get("Description", ""))

        allowed = set(" a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*") | set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        for f in fields:
            assert f.isascii(), f"描述字段含非 ASCII:{f!r}"
            bad = [c for c in f if c not in allowed]
            assert not bad, f"描述字段含不允许的字符 {bad}:{f!r}"


class TestControllerMatchesTokyoVersion:
    def test_chart_version_pinned_to_tokyo(self):
        """灾备站点跑不同版本的 controller，等于在演练另一个东西。"""
        text = LBC_VALUES.read_text(encoding="utf-8")
        assert_contains(text, "aws-load-balancer-controller-3.0.0")
        assert_contains(text, "chart 版本刻意与东京一致")

    def test_service_account_not_created_by_chart(self):
        """create: false —— chart 建的那个不带角色注解，表现是每次调用 403。"""
        vals = load_cfn(LBC_VALUES)
        assert vals["serviceAccount"]["create"] is False
        assert vals["serviceAccount"]["name"] == "alb-ingress-controller"

    def test_region_scoped_values_are_korea(self):
        vals = load_cfn(LBC_VALUES)
        assert vals["clusterName"] == "dr-korea-petsite"
        assert vals["region"] == KOREA
        assert vals["vpcId"] == "vpc-0238efd50c0bf0dac"


class TestRecordKeepsTheHardWonFindings:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_status_code_also_lies(self, record: str):
        """4.21 记的是「健康探针会骗人」，4.23 发现状态码也会。"""
        assert_contains(record, "状态码也会骗人")
        assert_contains(record, "Error - Observability PetAdoptions")

    def test_root_cause_chain_is_recorded(self, record: str):
        """根因要具体到哪个参数，不能只说「配置缺失」。"""
        assert_contains(record, "/petstore/searchapiurl")
        assert_contains(record, "ParameterNotFound")

    def test_serve_question_is_now_answered(self, record: str):
        """4.21 记的 inconclusive 现在有答案了 —— 必须写明是「不能」。"""
        assert_contains(record, "不再是 inconclusive")

    def test_redirect_is_explained(self, record: str):
        """那个 302 的来源要写清，否则下一个人会再查一遍。"""
        assert_contains(record, "Location: /?userId=user88001")
        assert_contains(record, "会话分配行为")

    def test_pdb_deadlock_recorded_with_causal_proof(self, record: str):
        """缩容死锁 + **因果**验证（不是相关性）。"""
        assert_contains(record, "缩容到零会死锁 30 分钟")
        assert_contains(record, "因果验证")
        assert_contains(record, "Terminating:Proceed")
        assert_contains(record, "HeartbeatTimeout=1800")

    def test_stale_target_at_rest_recorded(self, record: str):
        """静息状态的陈旧目标 —— 又一个分不清两种状态的信号。"""
        assert_contains(record, "静息状态有个陈旧目标")
        assert_contains(record, "controller 自己也在那个被排空的节点上")

    def test_unverified_claim_is_marked_as_such(self, record: str):
        """扩容时能否 reconcile 掉陈旧目标**尚未验证** —— 不许写成已验证。"""
        assert_contains(record, "尚未验证")

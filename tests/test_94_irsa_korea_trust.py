"""
test_94_irsa_korea_trust.py — 韩国 IRSA 的契约。

## 守的是什么

petsite 的 7 个业务服务全走 IRSA。而 IRSA 的失效方式极其隐蔽:

    pod 能起来 → 能过健康检查的前半段 → 每一次 AWS 调用都 403

**切换前做静态检查完全看不见这个缺陷**,只在真切换时暴露。
2026-09-25 实测 `list-open-id-connect-providers`:一个 `ap-northeast-2` 都没有。

这个文件守两件事:
① 建 provider 的模板不许退化(尤其不许写死 thumbprint)
② 改生产角色信任策略的脚本不许丢掉它的三条自保措施
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OIDC = ROOT / "infra" / "dr-korea" / "11-irsa-korea-oidc.yaml"
MAPPING = ROOT / "infra" / "dr-korea" / "irsa-korea-mapping.json"
SCRIPT = ROOT / "scripts" / "add_korea_irsa_trust.py"


@pytest.fixture(scope="module")
def oidc() -> dict:
    class _L(yaml.SafeLoader):
        pass

    def _any_node(loader, node):
        """CFN 短标签可能是标量、序列或映射 —— 要按节点类型分发。

        ⚠️ 第一版只写了 `loader.construct_scalar(node)`,于是
        `!Select [1, !Split ['https://', …]]` 直接 ConstructorError:
        「expected a scalar node, but found sequence」。

        本项目其他门禁(test_83 / test_92 / test_93)用的是只处理标量的版本,
        它们能过只是因为那几个模板刚好没用带序列参数的短标签 ——
        **那不是「写对了」,是「还没踩到」。**
        """
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        return loader.construct_scalar(node)

    for tag in ("!Sub", "!Ref", "!GetAtt", "!Join", "!Select", "!Split"):
        _L.add_constructor(tag, _any_node)
    return yaml.load(OIDC.read_text(encoding="utf-8"), Loader=_L)


@pytest.fixture(scope="module")
def mapping() -> dict:
    return json.loads(MAPPING.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


class TestOidcProviderTemplate:
    def test_resource_type(self, oidc: dict):
        types = [r["Type"] for r in oidc["Resources"].values()]
        assert "AWS::IAM::OIDCProvider" in types

    def test_thumbprint_is_not_hardcoded(self):
        """⚠️ 最重要的一条:不许写死指纹。

        `CreateOpenIDConnectProvider` 的 required 只有 `['Url']`（查 API 模型得到），
        AWS 会自己取指纹。写死的值会随 CA 轮换而过期，而**过期的表现也是 403**，
        和「provider 没注册」长得一模一样 —— 那会让下一次排查走进死胡同。
        """
        text = OIDC.read_text(encoding="utf-8")
        # 模板里可以在注释里**提到**那个值（说明为什么不写死是有价值的），
        # 但不许出现在 ThumbprintList 属性里。
        assert "ThumbprintList:" not in text, (
            "不许写死 ThumbprintList —— AWS 会自己取，"
            "写死的值过期后表现为 403，与「没注册」无法区分"
        )

    def test_client_id_is_sts(self):
        text = OIDC.read_text(encoding="utf-8")
        assert "sts.amazonaws.com" in text, "IRSA 的 audience 固定是 sts.amazonaws.com"

    def test_provider_is_retained(self, oidc: dict):
        """删掉 provider 会让 7 个角色的韩国语句指向不存在的 principal。

        IAM 不会报错 —— 只是永远拒绝。所以必须 Retain。
        """
        for name, r in oidc["Resources"].items():
            if r["Type"] != "AWS::IAM::OIDCProvider":
                continue
            assert r.get("DeletionPolicy") == "Retain", f"{name} 缺 DeletionPolicy: Retain"

    def test_url_is_korea_region(self, oidc: dict):
        defaults = " ".join(
            str(p.get("Default", "")) for p in oidc.get("Parameters", {}).values()
        )
        assert "oidc.eks.ap-northeast-2.amazonaws.com" in defaults


class TestMappingCameFromLiveCluster:
    def test_seven_business_service_accounts(self, mapping: dict):
        sas = mapping["service_accounts"]
        assert len(sas) == 7, f"应当是 7 个业务 SA，实际 {len(sas)}"

    def test_excludes_non_irsa_and_chaos_accounts(self, mapping: dict):
        sas = mapping["service_accounts"]
        # default 没有 role-arn 注解；fis-service-account 是混沌实验用的，
        # 不是 petsite 业务链路的一环。
        assert "default" not in sas
        assert "fis-service-account" not in sas

    def test_documents_how_to_regenerate(self, mapping: dict):
        """映射是活集群读出来的快照 —— 必须写明怎么重新生成。

        SA 或角色变动后这份快照就过期了，而过期的表现是 403（又是那个形状）。
        """
        comment = " ".join(mapping.get("_comment", []))
        assert "重新生成" in comment
        assert "role-arn" in comment

    def test_provider_arn_matches_host(self, mapping: dict):
        """ARN 和 Condition 键用的 host 必须指向同一个 issuer。

        两者不一致时 IAM 不报错，只是永远拒绝 —— 这种错最难查。
        """
        assert mapping["korea_oidc_host"] in mapping["korea_provider_arn"]


class TestScriptKeepsItsSafeguards:
    def test_default_is_dry_run(self, script: str):
        assert '"--apply"' in script
        assert "action=\"store_true\"" in script

    def test_apply_requires_backup_dir(self, script: str):
        """改生产角色不留备份是不可接受的 —— 这条要在代码里强制，不是靠自觉。"""
        assert "if args.apply and not args.backup_dir:" in script

    def test_is_append_only_not_regenerate(self, script: str):
        """`UpdateAssumeRolePolicy` 替换整个文档 —— 必须只追加。"""
        assert 'new_doc["Statement"].append(' in script
        assert "只追加" in script

    def test_verifies_original_statements_survived(self, script: str):
        """改后必须逐字核对原有语句还在。

        这是这个脚本能不能被信任的关键：它动的是东京生产站点的角色。
        """
        assert "原有语句被改动了" in script, "要有「原有语句被改动」的失败分支"
        assert "json.dumps(st, sort_keys=True) not in after_norm" in script, (
            "核对要逐字比对（排序后序列化），不是只数条数"
        )

    def test_is_idempotent(self, script: str):
        assert "def has_korea(" in script
        assert "已有韩国那条，跳过" in script

    def test_scopes_by_sub_not_only_aud(self, script: str):
        """韩国那条必须同时限定 :aud 和 :sub。

        东京那条（CDK 生成的）只限定 :aud，等于该集群里任何 SA 都能 assume。
        韩国这条不该复制那个宽松度。
        """
        i = script.index("def korea_statement(")
        seg = script[i : i + 1200]
        assert ":aud" in seg and ":sub" in seg
        assert "system:serviceaccount:{NAMESPACE}:{sa}" in seg, (
            "sub 的格式是 system:serviceaccount:<ns>:<sa> —— "
            "写错不会报错，只会永远拒绝"
        )

    def test_does_not_suppress_stderr(self, script: str):
        """错误文本往往就是答案 —— 本项目已两次因抑制/截断 stderr 而误判。"""
        assert "2>/dev/null" not in script
        assert "stderr" in script, "失败时要把 aws 的 stderr 带出来"

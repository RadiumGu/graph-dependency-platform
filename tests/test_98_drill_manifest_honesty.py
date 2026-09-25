"""
test_98_drill_manifest_honesty.py — 演练清单不许被当成「能用的配置」。

## 这个文件守的是本会话最贵的一条发现

2026-09-25 在韩国真起了一次 petsite。结果:

    pod 状态        Running     ✅
    restarts        0           ✅
    readiness 探针   通过        ✅
    /health/status  200 "Alive" ✅

**而那个 petsite 读到的是零配置** —— 韩国的 SSM `/petstore` 前缀下有 0 个参数
（东京有 41 个），SDK 解析到的 region 是 ap-northeast-2。

`/health/status` 返回的是硬编码的 `"Alive"`（5 字节），**完全不碰配置**。
所以 **k8s 层面的灾备就绪检查在一个什么都没配好的 petsite 上是全绿的。**
这比崩溃坏得多 —— 崩溃会告警，而这个不会。

## 还守一条方法论

同一次演练里，pod proxy 打出 `GET / → 302`、`/adoptionlist → 404`。
我本来要写「韩国的 petsite 起不来」——**做了东京对照组才发现东京一模一样**。
没有对照组，一个错误结论就会进灾备手册。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cfn_yaml import load_cfn

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / "infra" / "dr-korea" / "petsite-korea-drill.yaml"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def drill() -> dict:
    # 这是 k8s 清单而不是 CFN，但同一个宽松 loader 也能读
    # （它只是把未知标签当数据，标准 YAML 本来就读得了）。
    return load_cfn(DRILL)


@pytest.fixture(scope="module")
def drill_text() -> str:
    return DRILL.read_text(encoding="utf-8")


class TestDrillManifestSaysItIsNotProduction:
    def test_it_is_a_deployment(self, drill: dict):
        assert drill["kind"] == "Deployment"
        assert drill["metadata"]["namespace"] == "petadoptions"

    def test_declares_it_cannot_serve(self, drill_text: str):
        """清单必须在开头就说明「这不是一份能让 petsite 服务的配置」。

        少了这句话，下一个人会把它当成「韩国侧的 petsite 部署清单」直接用 ——
        而它起来之后 k8s 层面全绿，没有任何东西会提示他配置是空的。
        """
        assert "不是**一份能让 petsite 真正服务的清单" in drill_text, (
            "要在清单里明写它不能服务 —— 判据照抄清单里的真实文字"
        )

    def test_lists_the_region_local_config_gap(self, drill_text: str):
        """要列出那些 region 内的依赖，而不只是说「需要配置」。"""
        # 这些是实测清点出来的参数名，不是举例。
        for name in ("rdssecretarn", "queueurl", "dynamodbtablename", "dataprotection"):
            assert name in drill_text, f"缺了 {name} —— 依赖清单不完整就估不出工作量"
        assert "41" in drill_text or "0 个参数" in drill_text, (
            "要写明东京有多少、韩国有多少"
        )

    def test_says_copying_values_does_not_help(self, drill_text: str):
        """最关键的一句：照搬参数值没用，因为值指向东京的资源。"""
        assert "等于让韩国去连一堆不存在的后端" in drill_text

    def test_deviations_from_production_are_enumerated(self, drill_text: str):
        """与生产清单的每处差异都要写清为什么。

        否则下一个人会以为这份就是生产清单的等价物。
        """
        assert "都是刻意的" in drill_text
        for why in ("prune", "topologySpreadConstraints", "startupProbe"):
            assert why in drill_text, f"没说明为什么改了 {why}"


class TestDrillUsesKoreaImage:
    def test_image_is_korea_region(self, drill: dict):
        img = drill["spec"]["template"]["spec"]["containers"][0]["image"]
        assert "ap-northeast-2" in img.split("/")[0], (
            "镜像必须从韩国 ECR 拉 —— 这次演练的核心目的就是验证这条路"
        )

    def test_repo_name_keeps_the_tokyo_suffix(self, drill: dict):
        """仓库名里仍带 ap-northeast-1 —— 复制不支持改名，那是名字的一部分。"""
        img = drill["spec"]["template"]["spec"]["containers"][0]["image"]
        repo = img.split("/", 1)[1].split(":")[0]
        assert repo.endswith("ap-northeast-1"), (
            f"仓库名是 {repo} —— CDK asset 仓库名里的 ap-northeast-1 是名字的一部分，"
            "改掉会让 ECR 复制落到另一个仓库"
        )

    def test_uses_the_existing_irsa_service_account(self, drill: dict):
        sa = drill["spec"]["template"]["spec"]["serviceAccountName"]
        assert sa == "petsite-sa"


class TestRecordKeepsTheControlGroupLesson:
    def test_record_documents_the_control_group(self):
        """部署记录必须留下「对照组救了我」这件事。

        这是本会话最容易被后人丢掉的一条经验：一个只在单侧观察到的现象，
        在没有对照之前不能当成差异。
        """
        text = RECORD.read_text(encoding="utf-8")
        assert "对照组救了我" in text
        assert "东京一模一样" in text

    def test_record_says_result_is_inconclusive(self):
        """结论必须是 inconclusive，不能写成「通过」或「失败」。

        现有探针在两个 region 上没有区分力 —— 按判据纪律，
        「没有区分力的测量」只能是 inconclusive。
        """
        text = RECORD.read_text(encoding="utf-8")
        # ⚠️ 照抄记录里的**真实**标点。第一版我按习惯写了全角逗号「，」，
        # 而记录里用的是半角「,」—— 断言在文档完全正确时挂掉。
        # 这是本会话第九次「判据没照抄实现」，而这一次连标点都算。
        assert "没有证明 petsite 在韩国能服务用户,也没有证明它不能" in text
        assert "**inconclusive**,不是「通过」也不是「失败」" in text

    def test_record_warns_that_k8s_green_is_not_dr_ready(self):
        text = RECORD.read_text(encoding="utf-8")
        assert "在一个什么都没配好的 petsite 上是全绿的" in text

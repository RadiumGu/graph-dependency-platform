"""
test_99_switchover_runbook_honesty.py — 切换手册不许被改成「一切就绪」。

## 为什么需要门禁守一份文档

`docs/runbooks/dr-korea-switchover.md` 的价值全在它**诚实**:它说清了现在切换
会得到一个「基础设施完好、应用层不能服务」的站点。

一份灾备手册最危险的退化方式不是写错命令，而是**随着基础设施逐步完善，
有人把「还差什么」那几节删掉，却没有真的把它们做完**。删掉之后手册读起来像
「已就绪」，而下一个人会在真出事的时候才发现不是。

所以这里守的不是格式，是**那些必须留在手册里的坏消息**。

## 门禁与被守护缺陷不共享失效通道

这些断言只读文件文本，不依赖任何 AWS 调用、不依赖 conftest 的 fixture，
所以在离线 CI 里也真的执行（`test_91` 那次教训：12 条门禁全进了 skipped）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains, contains

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "runbooks" / "dr-korea-switchover.md"
MAPPING = ROOT / "infra" / "dr-korea" / "irsa-korea-mapping.json"


@pytest.fixture(scope="module")
def book() -> str:
    assert RUNBOOK.exists(), f"{RUNBOOK} 不在了 —— 灾备手册不能删"
    return RUNBOOK.read_text(encoding="utf-8")


class TestHeadlineStaysHonest:
    def test_says_app_layer_cannot_serve(self, book: str):
        """开头那一句结论是整份手册最重要的内容。"""
        assert_contains(
            book,
            "基础设施层完好、应用层不能服务用户的站点",
            "一句话结论被改了 —— 如果应用层真的能服务了，"
            "请连同第三节的能力矩阵与第五节的缺口一起更新，而不是只改这一句",
        )

    def test_warns_no_automatic_criterion_catches_it(self, book: str):
        """最关键的警告：没有自动判据会告诉你应用层不行。"""
        assert_contains(book, "没有任何自动判据会告诉你")


class TestBadNewsSectionsMustSurvive:
    """第五节那五个缺口，每一个都必须还在。"""

    def test_workload_gap(self, book: str):
        assert_contains(book, "东京跑 7 个 Deployment，不是 1 个")
        # 七个 Deployment 的名字都要列出来 —— 这是实测读活集群得到的
        for dep in (
            "list-adoptions",
            "pay-for-adoption",
            "petfood",
            "pethistory-deployment",
            "petsite-deployment",
            "search-service",
            "traffic-generator",
        ):
            assert_contains(book, dep, "工作负载清单不完整就估不出工作量")

    def test_ingress_gap_and_why_static_checks_miss_it(self, book: str):
        """入口缺口 + 为什么静态检查发现不了。"""
        assert_contains(book, "静态看清单永远发现不了的缺口")
        assert_contains(book, "TargetGroupBinding")
        assert_contains(book, "那些目标组 ARN 是 region 专属的，照搬到韩国无效")
        # 真实的 ALB 名字，实测读出来的
        assert_contains(book, "Servic-PetSi-by0kpyBtxswj")

    def test_eighth_irsa_consumer(self, book: str):
        """漏掉的第 8 个 IRSA 消费者 —— 这是写手册时才发现的。"""
        assert_contains(book, "漏掉的第 8 个 IRSA 消费者")
        assert_contains(book, "alb-ingress-controller")
        assert_contains(book, "ServicesEks2-LoadBalancerServiceAccountB6807779")
        # 必须说清失效表现，否则读者不知道该找什么
        assert_contains(book, "装上了、pod 起来了、一个 ALB 也不建")

    def test_addon_gap(self, book: str):
        assert_contains(book, "韩国 1 个，东京 5 个")
        assert contains(book, "aws-load-balancer-controller") or contains(
            book, "eks-pod-identity-agent"
        )

    def test_backend_dependency_gap(self, book: str):
        assert_contains(book, "等于让韩国去连一堆不存在的后端")
        assert_contains(book, "独立的工程项，不是配置复制")
        for name in ("rdssecretarn", "queueurl", "dynamodbtablename", "dataprotection"):
            assert_contains(book, name)


class TestIrsaGapMatchesTheMappingFile:
    def test_mapping_still_lacks_the_lb_controller(self):
        """手册说映射里漏了 LB controller —— 核实这句话此刻仍然为真。

        ⚠️ 这条门禁**会在缺口被修好时挂掉**，而那是设计意图：
        补上 alb-ingress-controller 的人必须同时来改手册第五节③，
        否则手册就在说一件已经不成立的事。
        """
        import json

        m = json.loads(MAPPING.read_text(encoding="utf-8"))
        sas = m["service_accounts"]
        assert len(sas) == 7, f"映射现在有 {len(sas)} 项 —— 请同步更新手册第五节③"
        assert "alb-ingress-controller" not in sas, (
            "LB controller 的 IRSA 已经补上了 —— 请更新手册第五节③，"
            "把它从「缺口」改成「已覆盖」，并把第三节能力矩阵的入口一行一起改"
        )


class TestTrapTableMustSurvive:
    """第六节的陷阱清单是踩出来的，一条都不能少。"""

    @pytest.mark.parametrize(
        "trap",
        [
            "是硬编码字符串，不碰配置",
            "查 k8s API 数 `Ready=True`",
            "查 ASG `LifecycleState`",
            "必须有对照组",
            "安全组静默丢包",
            "要真起一次 pod",
            "进程可能还拿着内存里的旧模块",
            "零流量与健康在指标上**无法区分**",
        ],
    )
    def test_trap_present(self, book: str, trap: str):
        assert_contains(book, trap, "陷阱清单里的每一条都是踩过的，不能删")


class TestDangerousStepsAreMarkedAsNeedingApproval:
    def test_database_promotion_needs_approval(self, book: str):
        assert_contains(book, "会让东京主库从 writer 变 reader，属于必须先问用户的操作")

    def test_promotion_never_actually_done(self, book: str):
        """权限验过 ≠ 提升过。这个区别必须留着。"""
        assert_contains(book, "从未真提升")

    def test_dns_needs_approval(self, book: str):
        assert_contains(book.split("切 DNS")[1][:200], "属于必须先问用户的操作")

    def test_three_arns_warning(self, book: str):
        """漏掉当前主集群那个 ARN 的坑 —— 报错不会告诉你少了哪个。"""
        assert_contains(book, "漏掉中间那个「当前主集群」会让调用被拒")
        assert_contains(book, "不会说少了哪个 ARN")

    def test_mutually_exclusive_flags(self, book: str):
        assert_contains(book, "`--switchover` 与 `--allow-data-loss` **互斥**")


class TestPodProbeGuidanceIsConcrete:
    def test_gives_the_real_proxy_path(self, book: str):
        """pod proxy 的路径格式要写全 —— 这是不能猜的东西。"""
        assert_contains(book, "/api/v1/namespaces/{ns}/pods/{pod}:{port}/proxy/{path}")

    def test_three_preconditions_with_distinct_symptoms(self, book: str):
        """三个前置条件 + 每个缺失的**不同**表现。

        表现不同这件事才是有用的部分：超时、403、调不通是三种不同的诊断入口。
        """
        assert_contains(book, "每个缺失的表现都不一样")
        assert_contains(book, "不是 refused")
        assert_contains(book, "**403**")

    def test_eventual_consistency_warning(self, book: str):
        assert_contains(book, "IAM/EKS 授权是最终一致的")
        assert_contains(book, "等 20–25 秒")

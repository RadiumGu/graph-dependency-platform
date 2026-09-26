"""
test_111_temporal_ui_entry.py — Temporal Web UI 公网入口的记录契约。

## 为什么这个文件只检查记录

这套入口的资源大部分在**东京**（公网 ALB 的监听器规则、目标组、跨 region 对等、
Cognito 回调），而本仓库的 `infra/dr-korea/` 管的是韩国，监听器本身还被东京的
CDK 部分纳管。所以它们是 CLI 建的，仓库里能守的是**判断与边界不被改写**。

## 完整重建命令（CLI 建的东西必须留下重建路径）

    # ① 跨 region 对等
    aws ec2 create-vpc-peering-connection --region ap-northeast-1 \\
      --vpc-id vpc-010ab37a3f9f74725 \\
      --peer-vpc-id vpc-0238efd50c0bf0dac --peer-region ap-northeast-2
    aws ec2 accept-vpc-peering-connection --region ap-northeast-2 \\
      --vpc-peering-connection-id <pcx>

    # ② 路由（东京 ALB 两个子网 → 10.20.0.0/16；韩国实例子网 → 11.0.0.0/16）
    aws ec2 create-route --region ap-northeast-1 --route-table-id <rtb> \\
      --destination-cidr-block 10.20.0.0/16 --vpc-peering-connection-id <pcx>
    aws ec2 create-route --region ap-northeast-2 --route-table-id rtb-01927e9d10d2763df \\
      --destination-cidr-block 11.0.0.0/16 --vpc-peering-connection-id <pcx>

    # ③ 安全组:只放行东京 ALB 的两个子网，**不要**放行整个 VPC
    aws ec2 authorize-security-group-ingress --region ap-northeast-2 \\
      --group-id sg-07dc94b7e77b88757 --ip-permissions \\
      'IpProtocol=tcp,FromPort=8080,ToPort=8080,IpRanges=[{CidrIp=11.0.0.0/24},{CidrIp=11.0.1.0/24}]'

    # ④ 目标组（AvailabilityZone=all 是跨 VPC/跨 region IP 目标必需的）
    aws elbv2 create-target-group --region ap-northeast-1 --name dr-temporal-ui-tg \\
      --protocol HTTP --port 8080 --vpc-id vpc-010ab37a3f9f74725 --target-type ip \\
      --health-check-path / --matcher HttpCode=200-399
    aws elbv2 register-targets --region ap-northeast-1 --target-group-arn <tg> \\
      --targets Id=10.20.1.10,Port=8080,AvailabilityZone=all

    # ⑤ 监听器规则（host-header，动作 1 authenticate-cognito → 动作 2 forward）
    #    Cognito 配置照抄 /streamlit 那条，但 SessionCookieName 必须独立

    # ⑥ Cognito 回调:先 describe-user-pool-client 读全量再回写（整体替换！）

## 守的四件事

1. **「ALB IP 目标不支持跨 region」这个错误结论的订正**不许被删 ——
   它驱动过一次错误的方案选择。
2. **用主机名而不是路径的理由** —— `/temporal` 返回 200 是 SPA catch-all，
   是「状态码骗人」的又一例。
3. **Cognito client 整体替换的坑** —— 漏字段会把所有现有入口的登录一起弄坏。
4. **侧门已关** —— 8080 不许再放行整个 VPC CIDR。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.35")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTheWrongPremiseIsCorrected:
    def test_the_correction_is_recorded(self, section: str):
        """**本文件最重要的一条。**

        我把一个没查过的技术限制当既定事实反复使用，并让它驱动了方案选择。
        """
        assert_contains(
            section, "我说「ALB 的 IP 目标不支持跨 region」是**错的**"
        )
        assert_contains(section, "**same Region or different Region**")

    def test_why_this_kind_of_error_is_expensive(self, section: str):
        """用错误前提排除掉正确方案，不会留下任何失败痕迹 —— 这句话是重点。"""
        assert_contains(
            section,
            "而「用错误前提排除掉正确方案」不会留下任何失败痕迹",
        )

    def test_the_collapsed_design_is_recorded(self, section: str):
        assert_contains(section, "塌缩成「在现有监听器上加一条规则」")


class TestHostBasedNotPathBased:
    def test_path_prefix_200_is_a_lie(self, section: str):
        """/temporal 返回 200 是 SPA catch-all —— 又一次「状态码骗人」。"""
        assert_contains(section, "那是 **SPA 的 catch-all**")
        assert_contains(section, "**又一次「状态码骗人」。**")

    def test_the_asset_path_mechanism_is_explained(self, section: str):
        """资源引用是根绝对路径 → 被默认规则转给 petsite。"""
        assert_contains(section, "资源引用是根绝对路径")
        assert_contains(section, "被默认规则转给 petsite")

    def test_no_base_path_option_was_verified(self, section: str):
        """「UI 没有 base-path 配置」是实测的，不是假设。"""
        assert_contains(section, "实测容器环境变量只有")
        assert_contains(section, "ALB 的 forward 动作**不做 URL 重写**")


class TestCognitoClientWholeReplacementTrap:
    def test_the_trap_is_recorded(self, section: str):
        assert_contains(section, "`update-user-pool-client` 不是补丁")
        assert_contains(section, "**全部清空**")

    def test_the_blast_radius_is_named(self, section: str):
        """漏字段会把 petsite / /graph / /streamlit 的登录一起弄坏。"""
        assert_contains(section, "的登录**一起坏掉**")

    def test_the_fix_is_read_then_write_back(self, section: str):
        assert_contains(section, "读全量,追加后把**所有**字段回写")


class TestSideDoorClosed:
    def test_the_old_wide_rule_is_recorded_as_removed(self, section: str):
        """8080 原先放行整个 VPC —— 任何 pod 都能终止工作流。"""
        assert_contains(section, "改前   8080 ← 10.20.0.0/16")
        assert_contains(section, "改后   8080 ← 11.0.0.0/24")

    def test_the_principle_is_stated(self, section: str):
        assert_contains(
            section, "**给正门上锁而侧门大开** 是这类活最容易留下的洞"
        )

    def test_why_auth_must_be_external(self, section: str):
        """UI 自己 Auth.Enabled=false 且写操作开启 —— 认证 100% 靠外层。"""
        assert_contains(section, "`Auth.Enabled=false` 且 `DisableWriteActions=false`")


class TestRemainingGapsNotHidden:
    def test_dns_record_is_recorded_as_pending(self, section: str):
        """本账号没有那个域名的托管区 —— DNS 只能由人加。"""
        assert_contains(section, "还缺一步,而且只能由人做")
        assert_contains(section, "temporal.rainmeadows.com   CNAME")

    def test_post_login_render_is_inconclusive(self, section: str):
        """登录后 UI 是否正常渲染没验证 —— 不许写成通过。"""
        assert_contains(section, "**登录后 UI 是否正常渲染我没法验证**")
        assert_contains(section, "这一条记 inconclusive")

    def test_cdk_deploy_would_wipe_this_rule(self, section: str):
        """与现有 Cognito 规则共享同一个脆弱点。"""
        assert_contains(
            section, "**所以 `cdk deploy ServicesEks2` 会把我这条规则一起抹掉**"
        )

    def test_bypass_header_not_copied(self, section: str):
        """没给 Temporal 规则加 X-Demo-Bypass —— 多一条旁路多一个绕过入口。"""
        assert_contains(section, "多一条旁路就多一个绕过认证的入口")

    def test_control_group_recorded(self, section: str):
        """改共享监听器后必须证明没弄坏现有入口。"""
        assert_contains(section, "**对照（现有入口没坏）**")

    def test_verification_bypassed_dns(self, section: str):
        """用 curl --resolve 绕开 DNS 完整验证 —— 这个手法值得留着。"""
        assert_contains(section, "**绕开 DNS**")


class TestDnsLandedAndPriorityConflict:
    """DNS 加好之后的验证，以及复核规则顺序才发现的优先级抢占。"""

    def test_grey_cloud_not_proxied_is_recorded(self, section: str):
        """解析出 AWS 地址而非 Cloudflare 地址 —— 灰云是可核验的判据，不是习惯。"""
        assert_contains(section, "所以是灰云(DNS-only)")

    def test_a_record_would_go_stale_with_evidence(self, section: str):
        """不用 A 记录的理由是观测到的地址轮换，不是「一般来说 ALB IP 会变」。"""
        assert_contains(section, "这个 ALB 的地址集已经轮换过")

    def test_the_priority_lesson_is_about_condition_overlap(self, section: str):
        """教训不是「我填错了数字」，而是判据本身错了：只看排在谁前面不够。"""
        assert_contains(section, "它与所有更小优先级规则的条件交集")

    def test_bypass_cannot_reach_temporal_ui(self, section: str):
        """放在旁路规则之后是刻意的 —— 提到 1 之前会让旁路头直达无认证 UI。"""
        assert_contains(section, "那个旁路到不了 Temporal UI")

    def test_the_fix_effect_is_marked_unverifiable(self, section: str):
        """两条规则都 302，从外面分不出命中哪条 —— 不许写成已测量。"""
        assert_contains(section, "结论来自 ALB 规则优先级语义(首个匹配)")

    def test_recorded_priority_is_four_not_fifteen(self, section: str):
        """资源清单里的优先级必须与线上一致，否则重建会重新引入抢占。"""
        assert_contains(section, "优先级 4，host-header temporal.rainmeadows.com")
        assert_contains(section, "主机规则被路径规则抢走:优先级 15 → 4")

"""
test_95_failover_and_restart_contract.py — 数据库提升的契约 + 代码变更必须生效。

## 两组判据,来自 2026-09-25 的实测

### 一、提升动作必须显式说明语义

`FailoverGlobalCluster` 的 `AllowDataLoss` 与 `Switchover` 是**互斥**的,
而文档说「不传 `AllowDataLoss` 时默认 switchover」。

原实现的有序分支**不传任何参数**,靠这个默认值。那恰好违背这一步的设计前提:
「有序 vs 丢数据」必须是一个**明确的裁决**,不能落在某个 API 默认上。

### 二、改了代码必须真的生效

`provision-worker.sh` 原来只在 systemd 单元变化时重启,应用代码变了不重启 ——
**Python 进程还拿着内存里的旧模块**。而它的核实判据(「worker 在队列上接单」)
在两种情况下都通过:

    磁盘 md5 与本地一致    ✅ 看起来部署成功
    worker 在队列上接单    ✅ 核实判据通过
    实际跑的还是旧代码      ❌

**又一个「分不出来」的判据 —— 本项目同类问题第五次。**
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cfn_yaml import load_cfn

ROOT = Path(__file__).resolve().parents[1]
ACT = ROOT / "dr-plan-generator" / "worker" / "activities.py"
PROV = ROOT / "dr-plan-generator" / "worker" / "provision-worker.sh"
PERM = ROOT / "infra" / "dr-korea" / "07-worker-permissions.yaml"


@pytest.fixture(scope="module")
def act() -> str:
    return ACT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prov() -> str:
    return PROV.read_text(encoding="utf-8")


# 解析走 tests/cfn_yaml.py 的单一来源（原来每个文件各有一份，
# 其中三份只处理标量节点，读不了带序列参数的短标签）。
_load_cfn = load_cfn


class TestPromotionSemanticsAreExplicit:
    """提升的两个变体都必须显式带标志，不许依赖 API 默认值。

    2026-10-05 起提升由人执行：命令原文由 runbook_draft 生成、经人审核后写进
    计划。判据从「activity 里的 kwargs」移到「给人的命令原文」—— 但要守的
    东西没变：有序 vs 丢数据必须是一个**明确的裁决**，不能落在某个默认值上。
    """

    @pytest.fixture(scope="class")
    def draft(self) -> str:
        return (ROOT / "dr-plan-generator" / "worker" / "runbook_draft.py").read_text(encoding="utf-8")

    def test_both_branches_pass_a_flag(self, draft: str):
        assert "--switchover" in draft, "有序分支必须显式带 --switchover"
        assert "--allow-data-loss" in draft

    def test_mutual_exclusivity_is_documented(self, draft: str):
        i = draft.index('id="promote-aurora"')
        assert "互斥" in draft[i : i + 2400], "要写明这两个参数互斥（API 约束）"

    def test_decision_precedes_promotion(self, draft: str):
        assert draft.index('id="aurora-mode"') < draft.index('id="promote-aurora"')

    def test_no_default_decision(self, draft: str):
        i = draft.index('id="aurora-mode"')
        seg = draft[i : i + 900]
        assert 'kind="decision"' in seg and "abort" in seg
        assert "default" not in seg.lower()


class TestFailoverPermissionRevoked:
    """2026-10-05：rds:FailoverGlobalCluster 已从 worker 角色收回。

    原来这组守的是「只有那一个 action、Resource 是三个具体 ARN」。执行模型改为
    人执行之后，正确的边界是 worker **根本没有**这条权限。三个 ARN 的教训
    （官方样例要求带上当前主集群，漏掉只报没权限）记在 runbook_draft 的
    命令旁边 —— 现在是人用自己的凭证执行，那条约束对人同样成立。
    """

    def test_action_absent_from_template(self):
        text = PERM.read_text(encoding="utf-8")
        assert "- rds:FailoverGlobalCluster" not in text
        assert "- eks:UpdateNodegroupConfig" not in text


class TestCodeChangeTriggersRestart:
    def test_restart_condition_includes_code_change(self, prov: str):
        """重启条件必须是「单元变化 OR 代码变化」。

        只看单元会让新代码永远不生效，而磁盘 md5 和「队列上有 poller」
        这两个判据都分辨不出这种情况。
        """
        assert 'if [ "$UNIT_CHANGED" = 1 ] || [ "$CODE_CHANGED" = 1 ]; then' in prov, (
            "重启条件要同时看单元与代码 —— 照抄脚本里的真实写法"
        )

    def test_code_fingerprint_is_content_not_mtime(self, prov: str):
        """指纹取内容哈希，不用 mtime。

        `s3 sync` 会重写 mtime 而内容可能没变 —— 用 mtime 会导致无谓重启，
        而无谓重启会打断正在跑的切换。
        """
        assert "sha256sum | cut -d' ' -f1" in prov
        assert "不用目录 mtime" in prov, "要写明为什么不用 mtime"

    def test_documents_the_stale_module_trap(self, prov: str):
        """要写明「Python 拿着内存里的旧模块」这个陷阱。

        少了这段说明，下一个人会觉得「同步了文件就等于部署了」而把重启去掉。
        """
        assert "内存里的旧模块" in prov
        assert "分不出来" in prov, "要写明那个核实判据分辨不出这种情况"

    def test_still_avoids_pointless_restart(self, prov: str):
        """代码没变时不许重启 —— 无谓重启会打断正在跑的切换。"""
        assert "单元与代码都未变，不重启" in prov

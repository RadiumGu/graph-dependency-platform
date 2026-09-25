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


def _load_cfn(path: Path) -> dict:
    class _L(yaml.SafeLoader):
        pass

    def _any_node(loader, node):
        # 按节点类型分发 —— 短标签的参数可能是序列或映射。
        # 只处理标量的版本会在 !Select [1, !Split […]] 上 ConstructorError。
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        return loader.construct_scalar(node)

    for tag in ("!Sub", "!Ref", "!GetAtt", "!Join", "!Select", "!Split"):
        _L.add_constructor(tag, _any_node)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_L)


class TestPromotionSemanticsAreExplicit:
    def test_both_branches_pass_a_parameter(self, act: str):
        """两个分支都要显式传参，不许依赖 API 默认值。"""
        assert 'kwargs["Switchover"] = True' in act, (
            "有序分支必须显式传 Switchover=True。"
            "靠「不传 AllowDataLoss 就默认 switchover」等于把一个"
            "**明确的裁决**落在 API 默认值上"
        )
        assert 'kwargs["AllowDataLoss"] = True' in act

    def test_mutual_exclusivity_is_documented(self, act: str):
        i = act.index('kwargs["Switchover"] = True')
        seg = act[max(0, i - 1400) : i]
        assert "互斥" in seg, "要写明这两个参数互斥（API 约束）"

    def test_would_run_matches_real_call(self, act: str):
        """`would_run` 必须和真实调用是同一条命令。

        早先有序分支的 would_run 不带任何标志，而真调用传 Switchover ——
        dry_run 打印的命令照着跑**复现不出**真执行路径，
        而 would_run 的全部价值就在于「照着它跑能复现」。
        """
        assert '--switchover" if ordered else' in act
        assert "--allow-data-loss" in act

    def test_no_default_decision(self, act: str):
        """没有明确裁决就必须失败，不设默认值。"""
        assert "这一步不设默认值" in act


class TestFailoverPermissionScope:
    @pytest.fixture(scope="class")
    def perm(self) -> dict:
        return _load_cfn(PERM)

    def _stmt(self, perm: dict, sid: str) -> dict:
        for r in perm["Resources"].values():
            doc = (r.get("Properties") or {}).get("PolicyDocument") or {}
            for s in doc.get("Statement") or []:
                if s.get("Sid") == sid:
                    return s
        pytest.fail(f"找不到 Sid={sid}")

    def test_action_is_only_failover(self, perm: dict):
        st = self._stmt(perm, "PromoteSecondaryCluster")
        acts = st["Action"]
        acts = [acts] if isinstance(acts, str) else acts
        assert acts == ["rds:FailoverGlobalCluster"], f"实际 {acts}"

    def test_resource_is_not_wildcard(self, perm: dict):
        st = self._stmt(perm, "PromoteSecondaryCluster")
        res = st["Resource"]
        res = [res] if isinstance(res, str) else res
        assert "*" not in [str(x).strip() for x in res], (
            "这个 action **支持资源级权限**（官方样例策略就是限定的），"
            "所以没有理由用 *"
        )

    def test_resource_has_three_arns(self, perm: dict):
        """三个：全局集群 + **当前主集群** + 目标从集群。

        漏掉主集群会让调用被拒，而报错只说没权限，不会说少了哪个 ARN。
        """
        st = self._stmt(perm, "PromoteSecondaryCluster")
        res = st["Resource"]
        res = [res] if isinstance(res, str) else res
        assert len(res) == 3, f"应当是 3 个 ARN，实际 {len(res)}"

    def test_global_cluster_arn_has_no_region(self):
        """全局集群的 ARN 没有 region 段 —— 两个冒号连着，不是笔误。"""
        text = PERM.read_text(encoding="utf-8")
        assert "arn:aws:rds::" in text, (
            "全局集群 ARN 形如 arn:aws:rds::<acct>:global-cluster:<name>"
        )
        assert "没有 region 段" in text, "要写明这不是笔误"


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

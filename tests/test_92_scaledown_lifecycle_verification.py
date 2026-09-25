"""
test_92_scaledown_lifecycle_verification.py — 缩容核实必须看 ASG 生命周期。

## 守的判据来自 2026-09-24 的活环境实测

缩到 `desired=0` 之后，两层看到的东西不一样：

    ASG LifecycleState:  {'Terminating:Wait': 1}      ← 真相
    EC2 State:           i-001ffcffddbe4b08b running  ← 分不出来

节点组的排空钩子 `Terminate-LC-Hook` 的 `HeartbeatTimeout=1800`（30 分钟）、
`DefaultResult=CONTINUE`。所以**一台正在优雅排空的实例和一台缩容失败卡住的
实例，在 EC2 那一层长得完全一样** —— 都是 `running`。

用 EC2 State 做判据，会把「正常排空中」报成「缩容失败」，
或者更糟：把「卡住了」报成「还在排空，再等等」。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cfn_yaml import load_cfn

ROOT = Path(__file__).resolve().parents[1]
ACT = ROOT / "dr-plan-generator" / "worker" / "activities.py"
PERM = ROOT / "infra" / "dr-korea" / "07-worker-permissions.yaml"


@pytest.fixture(scope="module")
def act() -> str:
    return ACT.read_text(encoding="utf-8")


class TestLifecycleCounting:
    def test_function_exists(self, act: str):
        assert "def count_asg_by_lifecycle(" in act

    def test_reads_lifecycle_state_not_ec2_state(self, act: str):
        i = act.index("def count_asg_by_lifecycle(")
        seg = act[i : i + 3800]
        assert 'inst.get("LifecycleState"' in seg, (
            "必须读 ASG 的 LifecycleState —— EC2 的 State 分不出 "
            "Terminating:Wait 与缩容失败"
        )
        assert "describe_auto_scaling_groups" in seg

    def test_asg_name_is_looked_up_not_hardcoded(self, act: str):
        i = act.index("def count_asg_by_lifecycle(")
        seg = act[i : i + 3800]
        # 名字形如 eks-<nodegroup>-<uuid>，uuid 在节点组创建时生成，
        # 节点组重建就变。必须现查。
        assert "autoScalingGroups" in seg, (
            "ASG 名要从 describe_nodegroup 的 resources.autoScalingGroups 读"
        )
        assert "eks-dr-korea-workers-" not in seg, (
            "ASG 名不许写死 —— 那个 uuid 后缀会随节点组重建而变"
        )

    def test_unknown_is_none_not_empty_dict(self, act: str):
        i = act.index("def count_asg_by_lifecycle(")
        seg = act[i : i + 3800]
        # 空字典会被读成「一台都没有了」，而实际是什么都没测到。
        assert "return None, f\"查 ASG 生命周期失败" in seg
        assert "空字典" in seg, "要写明为什么不能返回空字典"

    def test_terminating_wait_is_documented(self, act: str):
        i = act.index("def count_asg_by_lifecycle(")
        seg = act[i : i + 3800]
        assert "Terminating:Wait" in seg
        # 钩子的超时值决定了「等多久才算卡住」，写死在判据里会过期，
        # 但必须在文档里出现，否则下一个人不知道为什么要等这么久。
        assert "1800" in seg or "30 分钟" in seg


class TestAsgReadPermissionIsJustified:
    @pytest.fixture(scope="class")
    def perm(self) -> dict:
        # 解析走 tests/cfn_yaml.py 的单一来源。
        # 这里原来是一份只处理标量节点的本地实现 —— 它读不了
        # `!Select [1, !Split […]]` 那样带序列参数的短标签，
        # 而失败方式是 pytest **ERROR**（fixture 就挂了），
        # 整组测试被跳过，门禁变成什么都没守。
        return load_cfn(PERM)

    def _stmt(self, perm: dict, sid: str) -> dict:
        for r in perm["Resources"].values():
            doc = (r.get("Properties") or {}).get("PolicyDocument") or {}
            for s in doc.get("Statement") or []:
                if s.get("Sid") == sid:
                    return s
        pytest.fail(f"找不到 Sid={sid} 的语句")

    def test_asg_read_is_list_only(self, perm: dict):
        st = self._stmt(perm, "ReadAsgLifecycleState")
        acts = st["Action"]
        acts = [acts] if isinstance(acts, str) else acts
        assert acts == ["autoscaling:DescribeAutoScalingGroups"], (
            f"这条只该有那一个读动作，实际 {acts}"
        )
        # 一个写动作混进来就是越界：它能改缩放、能终止实例。
        assert not any(
            a.startswith("autoscaling:") and "Describe" not in a for a in acts
        )

    def test_wildcard_resource_is_justified_in_template(self):
        """`Resource: '*'` 必须在模板里写清为什么 —— 这是例外，不是惯例。

        服务授权参考（list_autoscaling.html 的 Actions 表）里
        `DescribeAutoScalingGroups` 的「资源类型」列是**空的**，也没有任何
        condition key，对比同页 `DeleteAutoScalingGroup` 是 `autoScalingGroup*`。
        空列意味着不支持资源级权限 —— 只能写 `*`。

        判据是「理由在不在模板里」而不是「值是不是 `*`」：
        直接禁 `*` 会让这条权限根本写不出来，而不留理由则会让下一个人
        把它当成「这里可以随便用 `*`」的先例。
        """
        text = PERM.read_text(encoding="utf-8")
        i = text.index("ReadAsgLifecycleState")
        # 理由写在 Sid 之前的注释块里。
        seg = text[max(0, i - 1600) : i]
        assert "资源类型" in seg or "资源级" in seg, (
            "Resource: '*' 缺少理由。要写明这个 action 在服务授权参考里"
            "没有资源类型，所以不支持资源级权限。"
        )
        assert "DescribeAutoScalingGroups" in seg

    def test_other_statements_still_not_wildcard(self, perm: dict):
        """这个例外不许扩散到别的语句上。"""
        for r in perm["Resources"].values():
            doc = (r.get("Properties") or {}).get("PolicyDocument") or {}
            for s in doc.get("Statement") or []:
                if s.get("Sid") == "ReadAsgLifecycleState":
                    continue
                res = s.get("Resource")
                res = [res] if isinstance(res, str) else (res or [])
                assert "*" not in [str(x).strip() for x in res], (
                    f"语句 {s.get('Sid')} 用了 Resource: '*'。"
                    "ReadAsgLifecycleState 的例外是文档硬约束，不是通用许可。"
                )

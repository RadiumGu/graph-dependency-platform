"""
test_87_ready_node_verification.py — 扩容核实必须看 Ready 的 k8s 节点。

守的判据来自 2026-09-24 的扩容演练:原来的核实是**数 EC2 实例**,那只证明
ASG 扩容成功,**不证明集群获得了可调度容量**。对灾备切换来说差别极大:
你可能有两台 EC2 和零个可调度节点,而步骤会报 `verified=True` 继续往下走。

实测确认过三件前置条件缺一不可(都不是推测):
  访问条目      AuthenticationMode=API 时调 k8s API 要有条目
  控制面 443    集群安全组原本只放行来自自己的流量，表现是 curl 超时
  自定义 RBAC   AmazonEKSViewPolicy 实测 403（资源表里没有 nodes）；
                AmazonEKSAdminViewPolicy 是 */* 且含 Secrets
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
ACT = W / "activities.py"
RBAC = W / "k8s-node-reader-rbac.yaml"
STACK = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "dr-korea"
    / "08-worker-k8s-readonly.yaml"
)


@pytest.fixture(scope="module")
def act() -> str:
    return ACT.read_text(encoding="utf-8")


class TestVerificationUsesReadyNodes:
    def test_ready_count_function_exists(self, act: str):
        assert "def count_ready_nodes(" in act

    def test_scale_step_verifies_on_ready_not_ec2(self, act: str):
        i = act.index('step="scale_up_nodegroup",\n        executed=True')
        seg = act[i : i + 1200]
        assert "ready_k8s_nodes" in seg, "detail 里要带 Ready 节点数"
        assert "verified=(None if ready is None else ready >= 2)" in seg, (
            "verified 必须基于 Ready 的 k8s 节点数，而不是 EC2 实例数 —— "
            "数 EC2 只证明 ASG 扩了，不证明集群有可调度容量"
        )
        # 不许退回成只看 EC2。
        assert "verified=found >= 2" not in seg

    def test_unknown_is_none_not_zero(self, act: str):
        i = act.index("def count_ready_nodes(")
        seg = act[i : i + 4200]
        # 查不到必须返回 None。返回 0 等于断言「没有 Ready 节点」，
        # 那是把「没测到」写成测量值 —— 本项目已犯四次的缺陷。
        assert "return None, f\"查 k8s 节点失败" in seg
        assert "不等于" in seg, "要写明「查不到 ≠ 没有 Ready 节点」"

    def test_ready_condition_string_not_bool(self, act: str):
        i = act.index("def count_ready_nodes(")
        seg = act[i : i + 4200]
        # Ready 条件的 status 是字符串 "True"/"False"/"Unknown"，不是布尔。
        assert 'c.get("status") == "True"' in seg, (
            'Ready 条件的 status 是字符串，写成 is True 会永远不匹配'
        )
        assert "Unknown" in seg, "要说明 Unknown（kubelet 失联）不算可调度容量"

    def test_tls_is_verified(self, act: str):
        i = act.index("def count_ready_nodes(")
        seg = act[i : i + 4200]
        assert "cafile=ca_path" in seg, "要用集群自己的 CA 校验 TLS"
        assert "verify_mode" not in seg and "CERT_NONE" not in seg, (
            "不许跳过 TLS 校验"
        )


class TestRbacIsMinimal:
    def test_rbac_file_exists(self):
        assert RBAC.exists()

    def test_only_nodes_and_only_read(self):
        text = RBAC.read_text(encoding="utf-8")
        assert 'resources: ["nodes"]' in text
        assert 'verbs: ["get", "list"]' in text
        # watch 会让一个只读身份长期占着 API server 的连接。
        assert '"watch"' not in text
        # 不许顺手加上 pods / secrets。
        assert "secrets" not in text
        assert 'resources: ["pods"]' not in text

    def test_binds_to_group_not_username(self):
        text = RBAC.read_text(encoding="utf-8")
        assert "kind: Group" in text
        # 访问条目的 username 含 {{SessionName}}，运行时才定，绑不住。
        assert "SessionName" in text, "要写明为什么不绑用户名"

    def test_group_name_matches_access_entry(self):
        rbac = RBAC.read_text(encoding="utf-8")
        stack = STACK.read_text(encoding="utf-8")
        m = re.search(r"name:\s*(dr-node-readers)", rbac)
        assert m, "RBAC 里找不到组名"
        assert m.group(1) in stack, (
            "组名与访问条目的 KubernetesGroups 不一致。"
            "不一致的表现是 403 —— 看起来像 ClusterRole 写错，而不像组名对不上"
        )


class TestStackDoesNotUseOverbroadPolicy:
    def test_no_overbroad_policy_is_actually_attached(self):
        text = STACK.read_text(encoding="utf-8")
        # ⚠️ 判据是「有没有真的挂上」，不是「字符串有没有出现」。
        #
        # 第一版写成 `assert "AmazonEKSSecretReaderPolicy" not in text` 就挂了 ——
        # 模板里提到它是在**说明「未给它」**的注释里。按字符串出现与否判断，
        # 等于禁止文档解释自己为什么不用某样东西。
        # 这是本会话第四次「判据与实现不对齐」，改成只看 PolicyArn 行。
        attached = re.findall(r"PolicyArn:\s*(\S+)", text)
        for bad in (
            "AmazonEKSAdminViewPolicy",       # */* 且官方文档明确写着含 Secrets
            "AmazonEKSSecretReaderPolicy",    # 直接读 Secret
            "AmazonEKSClusterAdminPolicy",    # 只在一次性引导时临时关联过
        ):
            assert not any(bad in a for a in attached), (
                f"{bad} 被真的挂上了。worker 只需要知道「节点 Ready 了没」"
            )

    def test_reason_for_not_using_managed_policies_is_documented(self):
        text = STACK.read_text(encoding="utf-8")
        # 理由要留下，否则下一个人会觉得「用托管策略更省事」而改回去。
        assert "403" in text, "要写明 AmazonEKSViewPolicy 实测 403"
        assert "Secrets" in text or "Secret" in text

    def test_control_plane_ingress_is_sg_scoped(self):
        text = STACK.read_text(encoding="utf-8")
        assert "SourceSecurityGroupId" in text, (
            "控制面 443 的来源要用安全组而不是 CIDR —— "
            "CIDR 会把整个子网段都放进来"
        )
        assert "CidrIp" not in text

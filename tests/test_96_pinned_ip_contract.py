"""
test_96_pinned_ip_contract.py — 固定私有 IP 的契约。

## 守的是什么

AgentCore runtime 的 `TEMPORAL_ADDRESS` 里写着 Temporal 实例的私有 IP。
实例一旦被替换,IP 就变,而 AgentCore 不会跟着改 —— 表现是 temporal-mcp 的
每次调用都超时,**而控制面显示 READY、日志里也没有明显报错**。

2026-09-25 把 IP 固定进模板消除了这条漂移。这个文件防的是它被退回去,
以及两个栈之间那条**手工耦合**被写错。
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPORAL = ROOT / "infra" / "dr-korea" / "02-temporal.yaml"
AGENTCORE = ROOT / "infra" / "dr-korea" / "06-agentcore-runtime.yaml"
COUPLING = ROOT / "scripts" / "check_dr_ip_coupling.py"


def _load_cfn(path: Path) -> dict:
    class _L(yaml.SafeLoader):
        pass

    def _any_node(loader, node):
        # 按节点类型分发 —— 只处理标量的版本会在 !Select [1, !Split […]] 上崩。
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        return loader.construct_scalar(node)

    for tag in ("!Sub", "!Ref", "!GetAtt", "!Join", "!Select", "!Split", "!ImportValue"):
        _L.add_constructor(tag, _any_node)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_L)


def _default(doc: dict, name: str) -> str:
    return str((doc.get("Parameters") or {}).get(name, {}).get("Default", ""))


@pytest.fixture(scope="module")
def temporal() -> dict:
    return _load_cfn(TEMPORAL)


@pytest.fixture(scope="module")
def agentcore() -> dict:
    return _load_cfn(AGENTCORE)


class TestIpIsPinned:
    def test_instance_has_private_ip(self, temporal: dict):
        text = TEMPORAL.read_text(encoding="utf-8")
        assert "PrivateIpAddress: !Ref PrivateIp" in text, (
            "实例必须固定私有 IP —— 否则 AgentCore 的 TEMPORAL_ADDRESS "
            "会在每次实例替换后过期，而过期的表现是调用超时，"
            "看起来像网络故障，唯独不像「地址过期」"
        )

    def test_pinned_ip_is_valid_and_in_subnet(self, temporal: dict):
        ip = _default(temporal, "PrivateIp")
        addr = ipaddress.ip_address(ip)
        net = ipaddress.ip_network("10.20.1.0/24")
        assert addr in net, f"{ip} 不在私有子网 a 的 CIDR {net} 里"
        # AWS 保留每个子网的前四个和最后一个地址。
        reserved = {net[0], net[1], net[2], net[3], net[-1]}
        assert addr not in reserved, f"{ip} 是 AWS 保留地址"

    def test_createonly_consequence_is_documented(self, temporal: dict):
        """必须写明这是 createOnly 属性、改它会替换实例。

        少了这句话，下一个人会以为改 IP 是个小改动。
        """
        text = TEMPORAL.read_text(encoding="utf-8")
        assert "createOnlyProperties" in text
        assert "替换实例" in text

    def test_documents_why_not_reuse_current_ip(self, temporal: dict):
        """必须写明「不能沿用旧实例正占着的 IP」。

        CFN 替换资源是先建新再删旧 —— 沿用旧 IP 会让新实例创建时
        因地址被占用而失败，且失败发生在栈更新中途。
        这个陷阱不写下来，下一个人几乎必然会踩。
        """
        text = TEMPORAL.read_text(encoding="utf-8")
        assert "先建新再删旧" in text
        assert "地址被占用" in text


class TestCrossStackCouplingStaysConsistent:
    def test_agentcore_address_matches_pinned_ip(self, temporal: dict, agentcore: dict):
        """两个栈之间的手工耦合：06 的地址必须与 02 固定的 IP 一致。

        ⚠️ 判据是「两个模板的默认值对得上」，而不是「等于某个字面量」。
        写成字面量的话，以后换 IP 要改三处（两个模板 + 门禁），
        而漏掉门禁那处会让它在错误的值上继续通过。
        """
        ip = _default(temporal, "PrivateIp")
        addr = _default(agentcore, "TemporalAddress")
        assert ip, "02 里没有 PrivateIp 的默认值"
        assert ip in addr, (
            f"06 的 TemporalAddress（{addr}）与 02 固定的 IP（{ip}）不一致。"
            "这条耦合是手工的：改了 02 的 PrivateIp 必须同时改 06。"
        )

    def test_agentcore_address_uses_http_api_port(self, agentcore: dict):
        """7243 是 HTTP API；7233 是 gRPC，8080 是 UI。

        temporal-mcp 走 HTTP API，端口写错的表现同样是「连不上」。
        """
        addr = _default(agentcore, "TemporalAddress")
        m = re.search(r":(\d+)", addr.split("//")[-1])
        assert m, f"取不到端口：{addr}"
        assert m.group(1) == "7243", (
            f"端口是 {m.group(1)}，应为 7243（HTTP API）。"
            "7233 是 gRPC（worker 用），8080 是 UI。"
        )

    def test_coupling_check_still_exists(self):
        """固定了 IP 不等于可以撤掉耦合检查。

        耦合仍然是**手工**的：改了 02 的 PrivateIp 就必须改 06。
        2026-09-25 那次替换里，这个脚本准确报出了 mismatch。
        """
        assert COUPLING.exists()
        text = COUPLING.read_text(encoding="utf-8")
        # 三态必须都在 —— 「查不到」不能被当成「一致」或「不一致」。
        for state in ("ok", "mismatch", "inconclusive"):
            assert state in text, f"三态里缺了 {state}"

    def test_agentcore_documents_the_manual_coupling(self, agentcore: dict):
        text = AGENTCORE.read_text(encoding="utf-8")
        assert "手工耦合" in text, (
            "要写明这仍是两个栈之间的手工耦合 —— "
            "固定 IP 消除了「漂移」，但没消除「要同时改两处」"
        )

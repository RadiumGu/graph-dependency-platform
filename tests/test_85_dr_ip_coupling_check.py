"""
test_85_dr_ip_coupling_check.py — IP 耦合检查脚本的门禁。

守的核心判据是**三态**:`ok` / `mismatch` / `inconclusive`。

为什么这一条最要紧:拿不到某一侧的值时若判成 `mismatch`,就会制造假告警;
而假告警会让人开始忽略这个检查 —— 那时它就等于不存在了。本项目已经有
同类缺陷四次(把未测量写成测量值),这里不能再犯第五次。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_dr_ip_coupling.py"
)


@pytest.fixture(scope="module")
def mod():
    assert SCRIPT.exists(), f"{SCRIPT} 不存在"

    # ⚠️ 必须先清掉可能过期的 .pyc。
    #
    # 2026-09-24 实测踩到:Python 判断 pyc 是否过期用的是「源文件 mtime + 大小」。
    # 做反向验证时我先用 sed 把 `HTTP_API_PORT = 7243` 改成 `8080`(编译出 pyc),
    # 再用 cp 还原 —— 两次操作在**同一秒内**,而 `7243` 与 `8080` **长度相同**,
    # 于是 mtime 与 size 都对得上,那个 8080 版本的 pyc 被当成有效,
    # 加载出来的模块常量还是 8080,而磁盘上明明是 7243。
    #
    # 后果比一条测试挂掉严重:**任何「改一下 → 跑测试 → 还原」的反向验证
    # 流程都可能被它骗过**,让人以为守卫有效或无效。所以这里主动清。
    import importlib

    pycache = SCRIPT.parent / "__pycache__"
    for stale in pycache.glob(f"{SCRIPT.stem}.*.pyc"):
        stale.unlink()
    importlib.invalidate_caches()

    spec = importlib.util.spec_from_file_location("check_dr_ip_coupling", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules["check_dr_ip_coupling"] = m
    spec.loader.exec_module(m)
    return m


class TestPortIsHttpApi:
    def test_uses_7243_not_ui_or_grpc(self, mod):
        # 7243 是 HTTP API；8080 是 Web UI；7233 是 gRPC（worker 走那个）。
        assert mod.HTTP_API_PORT == 7243, (
            f"HTTP API 端口应是 7243，实际 {mod.HTTP_API_PORT}"
        )

    def test_port_choice_is_documented(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert "8080" in text and "7233" in text, (
            "要写清 7243 与另两个端口的区别，否则下一个人会改错"
        )


class TestThreeStateVerdict:
    """查不到 ≠ 不一致。"""

    def _run(self, mod, monkeypatch, ip, ip_err, addr, addr_err, capsys):
        monkeypatch.setattr(mod, "instance_ip", lambda: (ip, ip_err))
        monkeypatch.setattr(mod, "agentcore_address", lambda: (addr, addr_err))
        monkeypatch.setattr(sys, "argv", ["check", "--json"])
        code = mod.main()
        import json

        return code, json.loads(capsys.readouterr().out)

    def test_consistent_is_ok(self, mod, monkeypatch, capsys):
        code, out = self._run(
            mod, monkeypatch, "10.20.1.125", None, "http://10.20.1.125:7243", None, capsys
        )
        assert out["verdict"] == "ok"
        assert code == 0

    def test_real_mismatch_is_reported(self, mod, monkeypatch, capsys):
        # 实例换了 IP，AgentCore 还指着旧的 —— 这才是真正需要处置的情况。
        code, out = self._run(
            mod, monkeypatch, "10.20.2.7", None, "http://10.20.1.125:7243", None, capsys
        )
        assert out["verdict"] == "mismatch"
        assert code == 1
        # 报告里要说清「超时看起来像什么」，否则排查的人会绕远。
        assert "超时" in out["detail"]

    @pytest.mark.parametrize(
        "ip,ip_err,addr,addr_err",
        [
            (None, "查实例 IP 失败：AccessDenied", "http://10.20.1.125:7243", None),
            ("10.20.1.125", None, None, "查 AgentCore 地址失败：AccessDenied"),
            (None, "实例不存在", None, "runtime 不存在"),
        ],
    )
    def test_missing_side_is_inconclusive_not_mismatch(
        self, mod, monkeypatch, capsys, ip, ip_err, addr, addr_err
    ):
        code, out = self._run(mod, monkeypatch, ip, ip_err, addr, addr_err, capsys)
        assert out["verdict"] == "inconclusive", (
            "拿不到某一侧的值时必须是 inconclusive。判成 mismatch 会制造假告警，"
            "而假告警会让人开始忽略这个检查 —— 那时它就等于不存在了。"
        )
        assert code == 2, "inconclusive 的退出码要与 mismatch 区分开"
        assert out["verdict"] != "mismatch"

    def test_inconclusive_carries_the_reason(self, mod, monkeypatch, capsys):
        _, out = self._run(
            mod, monkeypatch, None, "查实例 IP 失败：Throttling", "http://x:7243", None, capsys
        )
        # 无法判断时必须说明为什么，空着等于让读的人自己猜。
        assert "Throttling" in out["detail"]


class TestReadsLiveStateNotStackOutput:
    def test_uses_describe_instances_not_stack_output(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert "describe_instances" in text, (
            "要查实例当下的 IP，而不是读栈的 Output —— Output 是上次更新时的值"
        )
        assert "Output 是栈上次更新时的值" in text or "才是当下的事实" in text

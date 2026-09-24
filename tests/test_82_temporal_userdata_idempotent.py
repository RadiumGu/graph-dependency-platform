"""
test_82_temporal_userdata_idempotent.py — Temporal 引导脚本的门禁。

守两条实测踩出来的判据:

1. **口令生成必须幂等。** PostgreSQL 的数据是 bind mount 到
   `/opt/temporal/pgdata` 的,库一旦用旧口令初始化过,再生成一个新口令写进
   `.env` 就会让 Temporal 连不上自己的库 —— 而且报的是**认证失败**,
   看起来像配置写错而不像「口令被换了」。

   触发条件不是假设:更新 UserData 时 CloudFormation 会**重启**这台实例
   (根卷是 EBS;instance store 才是替换)。

2. **生成秘密的代码段必须在 `set +x` 里。** 引导脚本开了 `set -x` 且把输出
   tee 到日志文件,2026-09-24 第一次部署就把 PostgreSQL 口令明文落盘了。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TPL = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "dr-korea"
    / "02-temporal.yaml"
)


@pytest.fixture(scope="module")
def tpl() -> str:
    assert TPL.exists(), f"{TPL} 不存在"
    return TPL.read_text(encoding="utf-8")


def _password_block(text: str) -> str:
    """取出生成口令那一段（openssl 前后各若干行）。"""
    i = text.find("openssl rand")
    assert i > 0, "找不到口令生成语句"
    return text[max(0, i - 1200) : i + 500]


class TestPasswordGenerationIsIdempotent:
    def test_guarded_by_env_existence_check(self, tpl: str):
        block = _password_block(tpl)
        # 必须有「.env 已存在就不生成」的守卫。
        assert re.search(r"if\s+\[\s+!\s+-f\s+/opt/temporal/\.env\s+\]", block), (
            "口令生成没有幂等守卫。UserData 重跑会生成新口令覆盖 .env，"
            "而 PostgreSQL 已用旧口令初始化（数据 bind mount 在 "
            "/opt/temporal/pgdata），结果是 Temporal 连不上自己的库，"
            "且报认证失败 —— 看起来像配置写错而不像口令被换了。"
        )

    def test_reason_is_documented(self, tpl: str):
        # 判据留在模板里，否则下一个人会把守卫删掉。
        block = _password_block(tpl)
        assert "幂等" in block
        assert "pgdata" in block or "bind mount" in block


class TestSecretNotTraced:
    def test_openssl_is_inside_set_plus_x(self, tpl: str):
        block = _password_block(tpl)
        i_off = block.rfind("set +x")
        i_gen = block.find("openssl rand")
        assert i_off >= 0, "找不到 set +x —— 生成秘密前必须关掉 trace"
        assert i_off < i_gen, (
            "openssl rand 出现在 set +x 之前。引导脚本开了 set -x 并把输出 "
            "tee 到 /var/log/temporal-bootstrap.log，口令会明文落盘。"
        )

    def test_trace_is_turned_back_on(self, tpl: str):
        # 关掉就不开回来，会让后面的引导过程失去可追溯性。
        block = _password_block(tpl)
        after = block[block.find("openssl rand") :]
        assert "set -x" in after, "set +x 之后要把 trace 开回来"

    def test_env_file_permissions_are_tight(self, tpl: str):
        block = _password_block(tpl)
        assert "chmod 600" in block or "umask 077" in block


class TestVersionsAreRealTags:
    """镜像 tag 必须是真实存在的版本 —— 本任务编造过两个。"""

    def test_no_known_nonexistent_tags(self, tpl: str):
        # 1.29.0 与 2.42.0 都不存在（实测 Docker Hub tags API）。
        for bad in ("1.29.0", "2.42.0"):
            assert f"Default: {bad}" not in tpl, (
                f"{bad} 是不存在的 tag（实测 Docker Hub 确认）。"
                "temporalio/ui 的版本线与 server 不同步，不能按 server 版本推。"
            )

    def test_defaults_are_the_verified_versions(self, tpl: str):
        assert "Default: 1.29.7" in tpl, "auto-setup 实测可用版本是 1.29.7"
        assert "Default: 2.54.1" in tpl, "ui 实测可用版本是 2.54.1"

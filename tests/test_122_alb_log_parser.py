"""test_122_alb_log_parser.py — 守住「日志解析必须按字段下标，报告必须报覆盖率」。

## 为什么需要这个门禁

2026-10-03 第一版 bypass_survey 用「锚定 chosen_cert_arn 再取下一个 token」
的正则取 matched_rule_priority。行形状一变就错位，实测把优先级解析成了
ACM ARN 本身、密码套件名、"session-reused"，还凭空造出一堆 prio=-1。

更糟的是那份报告说「共 1939 条请求」而只分类了 932 条 —— **一个看起来像
证据的数字**。若按它得出「没人用旁路」的结论并据此删规则，是在用一个
坏掉的度量做安全决策。

所以守两件事：解析按下标做，报告先报覆盖率。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"
SCRIPT = ROOT / "crons" / "petsite_watch.py"

# 一行真实的 ALB 访问日志（取自 2026-10-02 的实际投递，已把令牌/追踪 id 换掉）。
# user_agent 里刻意含空格 —— 这正是按空格数数会错位的地方。
REAL_LINE = (
    'https 2026-10-02T08:08:57.478189Z app/Servic-PetSi-by0kpyBtxswj/bbe5082588a126fc '
    '179.167.183.210:42667 - -1 -1 -1 302 - 883 897 "GET https://rainmeadows.com:443/ HTTP/1.1" '
    '"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/123.0.6312.122 Safari/537.36" ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 '
    'arn:aws:elasticloadbalancing:ap-northeast-1:926093770964:targetgroup/Servic-PetSi-7JEWC19HNKSR/6046ee3e74aa7bbf '
    '"Root=1-6abf6699-0000000000000000" "rainmeadows.com" '
    '"arn:aws:acm:ap-northeast-1:926093770964:certificate/ce750241-29aa-4e22-8b5a-b98d7364ea7a" '
    '0 2026-10-02T08:08:57.468000Z "waf,authenticate" "-" "-" "-" "-" "-" "-" '
    'TID_0000000000000000 "-" "-" "-" 13.192.100.57 "-" "-"'
)


@pytest.fixture(scope="module")
def mod():
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib

    import petsite_watch

    return importlib.reload(petsite_watch)


@pytest.fixture(scope="module")
def section() -> str:
    txt = RECORD.read_text(encoding="utf-8")
    start = txt.index("### 4.48")
    return txt[start : txt.index("## 六、待记录", start)]


class TestTokenizerOnRealLine:
    def test_parses_a_real_line(self, mod):
        assert mod._tokenize(REAL_LINE) is not None

    def test_priority_is_zero_not_a_cipher_or_arn(self, mod):
        """第一版把这一行的优先级解析成了 ACM ARN。"""
        f = mod._tokenize(REAL_LINE)
        assert f[mod.F_PRIORITY] == "0"
        assert "arn:aws:acm" not in f[mod.F_PRIORITY]
        assert "ECDHE" not in f[mod.F_PRIORITY]

    def test_actions_and_type_and_client(self, mod):
        f = mod._tokenize(REAL_LINE)
        assert f[mod.F_ACTIONS] == "waf,authenticate"
        assert f[mod.F_TYPE] == "https"
        assert f[mod.F_CLIENT].startswith("179.167.183.210")

    def test_user_agent_with_spaces_does_not_shift_fields(self, mod):
        """UA 里有 5 个空格 —— 按空格数数就会错位到别的字段。"""
        assert REAL_LINE.count("Mozilla/5.0 (Windows NT 10.0;") == 1
        f = mod._tokenize(REAL_LINE)
        assert "Mozilla" in f[mod.F_REQUEST + 1]

    def test_short_line_returns_none_rather_than_guessing(self, mod):
        assert mod._tokenize("https 2026-10-02T08:08:57Z app/x 1.2.3.4:1") is None


class TestNoBrittleAnchorRegex:
    def test_does_not_anchor_on_cert_arn(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert '"arn:aws:acm:[^"]+"' not in src, (
            "这正是 2026-10-03 被证伪的取法：锚定 cert ARN 再取下一个 token"
        )

    def test_uses_named_field_indexes(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for name in ("F_TYPE", "F_PRIORITY", "F_ACTIONS", "F_REQUEST", "F_CLIENT"):
            assert f"{name} = " in src


class TestReportMustStateCoverage:
    def test_reports_parse_failures(self):
        src = SCRIPT.read_text(encoding="utf-8")
        assert "解析失败" in src
        assert "结论不完整" in src

    def test_separates_plaintext_listener(self):
        """http:80 不可能命中 443 规则 —— 不能算进分母也不能算成检查过。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "与旁路无关" in src
        assert '("https", "h2")' in src

    def test_does_not_claim_an_unchecked_denominator(self, section: str):
        assert_contains(section, "**毛病一:分母是假的。**")


class TestConclusionPreconditions:
    def test_targets_verified_alive(self, section: str):
        """「0 次使用」只有在目标健康时才是真信号。"""
        assert_contains(section, "三个目标组全部健康")

    def test_minus_one_explained(self, section: str):
        assert_contains(section, "**WAF 在规则评估前就拦掉了它们**")

    def test_prio3_unique_function_named(self, section: str):
        assert_contains(section, "整站免认证访问")

    def test_delete_not_narrow_with_reason(self, section: str):
        assert_contains(section, "只会让它变成 prio 1/2 的重复")

    def test_rollback_anchor_recorded(self, section: str):
        assert_contains(section, "alb-443-prio3-rule.json")

    def test_decision_left_to_human(self, section: str):
        assert_contains(section, "留给人拍")

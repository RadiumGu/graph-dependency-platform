"""
test_97_cfn_loader_single_source.py — CFN 解析必须只有一份实现。

## 守的是什么

2026-09-25 之前，「读 CFN 模板」这件事在 6 个门禁文件里各写了一遍
（test_83 / 92 / 93 / 94 / 95 / 96），而**其中三份是错的**：

    Loader.add_constructor(tag, lambda l, n: l.construct_scalar(n))

只处理标量节点，读不了带序列参数的短标签：

    !Select [1, !Split ['https://', !Ref Url]]
    → ConstructorError: expected a scalar node, but found sequence

**这个缺陷的失败方式特别坏**：pytest 报 ERROR 而不是 FAIL（fixture 阶段就挂），
于是整组测试被跳过 —— 一个本该守着某件事的门禁变成什么都没守，
而且它「挂了」的样子看起来像环境问题而不是判据问题。

那三份能长期绿，只是因为它们读的模板刚好没用带序列参数的短标签 ——
**那不是「写对了」，是「还没踩到」**。

所以这个文件守两件事：
① 解析实现只有一份（重复本身就是那三份错误能长期存在的原因）
② 那一份能读所有形状的短标签
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cfn_yaml import load_cfn, param_default

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
HELPER = TESTS / "cfn_yaml.py"


class TestOnlyOneImplementation:
    def test_helper_exists(self):
        assert HELPER.exists()

    def test_no_test_file_rolls_its_own(self):
        """除了共享辅助，没有别的文件自己装 CFN 构造器。

        判据照抄真实写法：`add_constructor` / `add_multi_constructor`。
        """
        offenders = []
        for f in sorted(TESTS.glob("test_*.py")):
            if f.name == Path(__file__).name:
                # 本文件的注释里引用了那两个名字当反例 —— 排除自己。
                # （「禁止某个模式」类门禁的通病：它自己得能谈论那个模式。
                #   test_91 已经踩过一次。）
                continue
            text = f.read_text(encoding="utf-8")
            for bad in ("add_constructor", "add_multi_constructor"):
                # 只看真正的调用，注释里提到是允许的。
                if re.search(rf"^\s*\w*\.?{bad}\(", text, re.MULTILINE):
                    offenders.append(f"{f.name} → {bad}")
        assert not offenders, (
            f"这些文件自己装了 CFN 构造器：{offenders}。"
            "改用 `from cfn_yaml import load_cfn` —— "
            "6 份重复里曾有 3 份是错的，重复本身就是那些错误能存在的原因。"
        )

    def test_helper_documents_why_it_exists(self):
        text = HELPER.read_text(encoding="utf-8")
        assert "还没踩到" in text, (
            "要写明那三份错误为什么能长期绿 —— 否则下一个人会觉得"
            "「本地写一份也没坏过」"
        )
        assert "ERROR 而不是 FAIL" in text, "要写明那个缺陷的失败方式"


class TestHelperHandlesEveryNodeShape:
    """共享实现必须能读三种节点形状。

    这一组用**真实的 YAML 文本**驱动，不是检查源码文本 ——
    「判据照抄实现」的另一面是：能直接验行为的就别验文本。
    """

    def _parse(self, body: str) -> dict:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(body)
            path = f.name
        try:
            return load_cfn(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_scalar_tag(self):
        d = self._parse("Outputs:\n  A:\n    Value: !Ref SomeParam\n")
        assert d["Outputs"]["A"]["Value"] == "SomeParam"

    def test_sequence_tag(self):
        """这是那三份错误实现挂掉的形状。"""
        d = self._parse(
            "Outputs:\n  A:\n    Value: !Select [1, !Split ['https://', !Ref U]]\n"
        )
        v = d["Outputs"]["A"]["Value"]
        assert isinstance(v, list), f"应当读成 list，实际 {type(v).__name__}"
        assert v[0] == 1

    def test_mapping_tag(self):
        d = self._parse(
            "Outputs:\n  A:\n    Value: !FindInMap\n      Key: v\n"
        )
        assert isinstance(d["Outputs"]["A"]["Value"], dict)

    def test_unknown_tag_does_not_crash(self):
        """用 add_multi_constructor 而非逐个列举，所以不认识的标签也能读。

        逐个列举的写法要求事先知道模板用了哪些短标签 —— 漏一个就崩，
        而「漏一个」不会在写的时候暴露，只在某天有人用了 !Cidr 时暴露。
        """
        d = self._parse("Outputs:\n  A:\n    Value: !Cidr ['10.0.0.0/16', 6, 5]\n")
        assert isinstance(d["Outputs"]["A"]["Value"], list)

    def test_still_rejects_plain_yaml_errors(self):
        """宽松只针对短标签 —— 真正的 YAML 语法错误仍要报出来。

        否则这个辅助会把「模板写坏了」也一起吞掉。
        """
        with pytest.raises(yaml.YAMLError):
            self._parse("Outputs:\n  A:\n   - x\n  B: [unclosed\n")


class TestParamDefaultHelper:
    def test_missing_param_returns_empty_string(self):
        """取不到返回 '' 而不是 None。

        调用方基本都拿它做 `in` / 字符串比较，None 会变成 TypeError，
        而那个报错指向调用处、看不出是「参数不存在」。
        """
        assert param_default({}, "Nope") == ""
        assert param_default({"Parameters": {}}, "Nope") == ""

    def test_reads_real_default(self):
        doc = {"Parameters": {"P": {"Default": "10.20.1.10"}}}
        assert param_default(doc, "P") == "10.20.1.10"

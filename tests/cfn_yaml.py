"""tests/cfn_yaml.py — CloudFormation 模板解析的**单一来源**

覆盖测试清单：共享基础设施，不对应具体用例编号

## 为什么需要这个文件

CFN 的短标签（`!Ref` / `!Sub` / `!GetAtt` / `!Select` …）不是标准 YAML，
`yaml.safe_load` 直接报 `ConstructorError`。所以每个读模板的门禁都要装一个
宽松 loader。

2026-09-25 之前，这件事在 **6 个文件里各写了一遍**
（test_83 / 92 / 93 / 94 / 95 / 96），而其中**有三份是错的**：

    Loader.add_constructor(tag, lambda l, n: l.construct_scalar(n))

只处理标量节点。遇到带序列参数的短标签就崩：

    !Select [1, !Split ['https://', !Ref Url]]
    → ConstructorError: expected a scalar node, but found sequence

更糟的是它的失败方式：pytest 报 **ERROR 而不是 FAIL**（fixture 阶段就挂了），
于是整组测试被跳过 —— 一个本该「守着某件事」的门禁变成了「什么都没守」。

而那三份能一直绿，只是因为它们读的模板刚好没用带序列参数的短标签 ——
**那不是「写对了」，是「还没踩到」**。第一个真写了 `!Select [1, !Split […]]`
的模板一出现，三个文件同时中招。

## 为什么不是把正确版本复制到那 6 个文件

那会把「3 处错」换成「6 处重复」。`tests/paths.py` 开头记的就是同一类教训：
改一次要改 N 处，而漏掉的那处会在错误的行为上继续通过。

## 实现上的两个选择

1. **`add_multi_constructor("!", …)` 而不是逐个 `add_constructor`。**
   逐个列举的写法要求你事先知道模板用了哪些短标签 —— 漏一个就崩，
   而「漏一个」不会在写的时候暴露，只会在某天有人用了 `!Cidr` 时暴露。
2. **按节点类型分发。** 短标签的参数可以是标量（`!Ref Foo`）、
   序列（`!Select [1, …]`）或映射，三种都要能读。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class LooseCfnLoader(yaml.SafeLoader):
    """把 CFN 短标签当普通数据读的 loader。

    ⚠️ 它**不求值**短标签 —— `!Ref Foo` 读出来就是字符串 `'Foo'`，
    `!Sub 'a${B}c'` 读出来是原始模板串。门禁关心的是「模板里写了什么」，
    不是「部署后会变成什么」，所以这样正好。

    需要断言最终值的场景不该用它 —— 那要么查线上真实资源，
    要么在模板里断言那个 `!Ref` 指向的参数的 `Default`。
    """


def _any_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


LooseCfnLoader.add_multi_constructor("!", _any_tag)


def load_cfn(path: str | Path) -> dict:
    """读一个 CFN 模板成 dict。短标签被当成普通数据，不求值。"""
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=LooseCfnLoader)


def param_default(doc: dict, name: str) -> str:
    """取某个参数的 Default，取不到返回空字符串。

    刻意返回 `''` 而不是 `None`：调用方基本都是拿它做 `in` / 字符串比较，
    `None` 会变成 `TypeError`，而那个报错指向调用处、看不出是「参数不存在」。
    """
    return str((doc.get("Parameters") or {}).get(name, {}).get("Default", ""))


def statements(doc: dict, resource_type: str = "AWS::IAM::ManagedPolicy") -> list[dict]:
    """取出某类资源的 PolicyDocument.Statement（合并所有同类资源）。"""
    out: list[dict] = []
    for r in (doc.get("Resources") or {}).values():
        if r.get("Type") != resource_type:
            continue
        pol = (r.get("Properties") or {}).get("PolicyDocument") or {}
        out.extend(pol.get("Statement") or [])
    return out

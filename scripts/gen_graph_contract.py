#!/usr/bin/env python3
"""从 profiles/graph_contract.yaml 生成 Lambda 层数据产物
infra/lambda/shared/python/graph_contract_data.py。

为什么要生成一个 .py 而不是让 Lambda 直接读 YAML：
    四个 ETL 是独立打包的 Lambda，profiles/ 不在它们的部署包里。
    生成纯 Python 字面量后，Lambda 侧零运行时依赖（不需要 pyyaml，
    也不需要把 profiles 打进每个包）。

数据与 API 分离：
    graph_contract_data.py —— 本脚本生成，只含数据字面量
    graph_contract.py      —— 手工维护，提供门禁 API
    这样改 API 不必重新生成，且漂移测试只需比对数据文件。

用法：
    python3 scripts/gen_graph_contract.py --write     # 落盘
    python3 scripts/gen_graph_contract.py --check     # 只校验是否最新（CI/测试用）

--check 的退出码：0 = 最新，1 = 已过期（需要重新生成）。
"""
from __future__ import annotations

import argparse
import pathlib
import pprint
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / 'profiles' / 'graph_contract.yaml'
OUT = REPO / 'infra' / 'lambda' / 'shared' / 'python' / 'graph_contract_data.py'

BANNER = '''"""图谱契约数据 —— 自动生成，请勿手工编辑。

来源：profiles/graph_contract.yaml
生成：python3 scripts/gen_graph_contract.py --write
校验：tests/test_35_graph_contract.py 断言本文件与来源一致（--check）

改契约请改 profiles/graph_contract.yaml 然后重新生成。
直接改本文件会在下一次 --check 时被测试打回。
"""
'''


def render(contract: dict) -> str:
    pp = pprint.PrettyPrinter(indent=4, width=96, sort_dicts=True)
    parts = [BANNER]
    parts.append(f"CONTRACT_VERSION = {contract['version']!r}\n")
    parts.append(f"TIMESTAMP_FIELD = {contract['timestamp_field']!r}\n")
    parts.append(
        "# 历史遗留的时间戳字段名。读取侧要兼容，写入侧只写 TIMESTAMP_FIELD。\n"
        f"TIMESTAMP_LEGACY_ALIASES = {tuple(contract['timestamp_legacy_aliases'])!r}\n"
    )
    parts.append(f"SOURCES = frozenset({sorted(contract['sources'])!r})\n")
    parts.append(
        "# 写一次属性：边上这些属性只由**首个发现者**写入，后续任何源都不得覆盖。\n"
        "# source 记录的是谁首先发现了这条依赖 —— 被覆盖等于抹掉发现史。\n"
        f"EDGE_WRITE_ONCE_ATTRS = frozenset({sorted(contract['edge_write_once_attrs'])!r})\n"
    )
    parts.append(
        "# 节点属性的权威来源。列表外的来源不得覆盖该属性。\n"
        f"NODE_ATTR_AUTHORITY = {pp.pformat(contract['node_attr_authority'])}\n"
    )
    parts.append(f"NODE_TYPES = {pp.pformat(contract['node_types'])}\n")
    parts.append(f"EDGE_TYPES = {pp.pformat(contract['edge_types'])}\n")
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--write', action='store_true')
    g.add_argument('--check', action='store_true')
    args = ap.parse_args()

    contract = yaml.safe_load(SRC.read_text())
    body = render(contract)

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(body)
        print(f"已写 {OUT}  ({len(body.splitlines())} 行, "
              f"{len(contract['node_types'])} 节点类型 / {len(contract['edge_types'])} 边类型)")
        return

    if not OUT.exists():
        print(f"产物不存在: {OUT}", file=sys.stderr)
        sys.exit(1)
    current = OUT.read_text()
    if current == body:
        print("产物为最新")
        sys.exit(0)
    print("产物已过期 —— profiles/graph_contract.yaml 改了但没重新生成。"
          "\n    修复: python3 scripts/gen_graph_contract.py --write", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()

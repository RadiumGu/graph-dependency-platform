#!/usr/bin/env python3
"""
graph-ask.py - 命令行工具：自然语言查询 Neptune 图谱。

用法:
    python3 graph-ask.py '你的问题'
    python3 graph-ask.py "petsite 依赖哪些数据库？"
    python3 graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
"""
import json
import os
import sys

# 将 rca/ 目录加入 path，使 neptune.* 等模块可直接 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from neptune.nl_query import NLQueryEngine


def main() -> None:
    """CLI 入口：解析参数，执行 NL 查询，格式化输出结果。"""
    if len(sys.argv) < 2:
        print("Usage: python3 graph-ask.py '你的问题'")
        print()
        print("示例:")
        print("  python3 graph-ask.py 'petsite 依赖哪些数据库？'")
        print("  python3 graph-ask.py 'AZ ap-northeast-1a 有哪些 Tier0 服务？'")
        print("  python3 graph-ask.py '哪些服务从未做过混沌实验？'")
        print("  python3 graph-ask.py '最近发生了几次 P0 故障？'")
        sys.exit(1)

    question = ' '.join(sys.argv[1:])
    engine = NLQueryEngine()
    result = engine.query(question)

    if 'error' in result:
        print(f"ERROR: {result['error']}")
        cypher = result.get('cypher', 'N/A')
        if cypher and cypher != 'N/A':
            print(f"  Generated Cypher: {cypher}")
        sys.exit(1)

    print(f"Question: {result['question']}")
    print(f"Cypher:   {result['cypher']}")
    print(f"Results ({len(result['results'])} rows):")
    print(json.dumps(result['results'], indent=2, ensure_ascii=False, default=str))
    print()
    print(f"Summary: {result['summary']}")


if __name__ == '__main__':
    main()

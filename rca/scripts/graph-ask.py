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

# 将 rca/ 与**项目根**都加入 path。
#
# 只加 rca/ 是不够的：`neptune/neptune_client.py` 有 `from shared import get_region`，
# 而 `shared/` 在项目根、不在 rca/ 下 —— 所以这个 CLI 在 shared 被引入之后
# 一直是**跑不起来**的，一执行就 `ModuleNotFoundError: No module named 'shared'`。
# 2026-09-20 修。（同一个缺失也让 gp-window-flush 的部署包挂过一次，
# 见 infra/lambda/rca_window_flush/build.sh 里的说明。）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..'))          # rca/
sys.path.insert(0, os.path.join(_HERE, '..', '..'))    # 项目根（shared/ 在这里）

# 走 factory 而不是直接 import 某个实现 —— 引擎选择归 factory
# （env NLQUERY_ENGINE，默认 strands），CLI 不该绑死具体实现。
from engines.factory import make_nlquery_engine


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
    engine = make_nlquery_engine()
    result = engine.query(question)

    # ⚠️ 判据必须是 `.get('error')` 而不是 `'error' in result`。
    #
    # strands 引擎的 _pack() **总是**带 error 这个 key（成功时值为 None），
    # 而 direct 只在出错时才放。用 `in` 检查 key 存在的话，切到 strands 后
    # 每一次成功查询都会被判成失败、打印 ERROR 并 sys.exit(1) ——
    # 实测确认过：两个引擎都成功、error 都是 None，但
    #     strands: 'error' in result = True
    #     direct : 'error' in result = False
    # 这类判据看的是「字段在不在」，而该看的是「值有没有」。
    if result.get('error'):
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

#!/usr/bin/env python3
"""
gen_association.py — 生成 DevOps Agent 的 associate-service 配置。

为什么要生成而不是手写：MCP 工具层是**独立于 IAM 的第二个权限平面**，
必须按工具名显式白名单。手写这份 24 项清单一定会与目录漂移——
目录里加一条查询、白名单忘了加，那条工具就静默不可用。

用法：
    python3 mcp/gen_association.py > mcp/devops-agent-association.json
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> None:
    from neptune.query_catalog import QUERY_CATALOG  # type: ignore

    names = sorted(QUERY_CATALOG)
    payload = {
        "mcpserver": {
            "tools": names,
            # 全部只读：本 server 没有任何写图或发起故障注入的能力，
            # 但仍显式声明——不能只依赖「实现上做不到」，权限要写在配置里。
            "toolDetails": [
                {"name": n, "toolClassification": "READ_ONLY"} for n in names
            ],
        }
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    print(f"# 共 {len(names)} 个工具，全部 READ_ONLY", file=sys.stderr)


if __name__ == "__main__":
    main()

"""
main.py — AgentCore Runtime「直接代码部署」的入口。

为什么有这个文件而不是直接把 agentcore_app.py 当入口：
直接代码部署把 zip 解开后按 entryPoint 执行，zip 根目录是 sys.path[0]。
本仓的模块（rca/neptune、profiles、shared）在 zip 里是平铺的兄弟目录，
需要先把根目录挂上 sys.path 再导入——放在这里做，agentcore_app.py 就能
保持「本地直接跑也行」的形态。

部署形态对比（都不需要改代码）：
  直接代码部署  zip → S3 → codeConfiguration(PYTHON_3_12, ["python","main.py"])
  容器部署      mcp/Dockerfile → ECR → containerConfiguration
本次用前者：沙箱里 sudo 被 no-new-privileges 挡住，装不了 Docker，
而直接代码部署本来就更省事。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# AgentCore 的硬要求：0.0.0.0:8000，POST /mcp
os.environ.setdefault("PORT", "8000")
os.environ.setdefault("BIND_HOST", "0.0.0.0")  # noqa: S104
os.environ.setdefault("MCP_PATH", "/mcp")

from agentcore_app import main  # noqa: E402

if __name__ == "__main__":
    main()

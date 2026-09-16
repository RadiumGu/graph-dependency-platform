# 图谱 MCP server —— Amazon Bedrock AgentCore Runtime 容器镜像。
#
# 构建上下文是**仓库根目录**（server 要 import rca/、profiles/）：
#   docker build --platform linux/arm64 -f mcp/Dockerfile -t graphdp-mcp:<tag> .
#
# AgentCore Runtime 要求 arm64。
#
# AgentCore 的协议契约（见 mcp/agentcore_app.py 头部注释）：
#   监听 0.0.0.0:8000，POST /mcp，stateless streamable-HTTP。
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    BIND_HOST=0.0.0.0 \
    MCP_PATH=/mcp \
    REGION=ap-northeast-1 \
    AWS_DEFAULT_REGION=ap-northeast-1

WORKDIR /app

COPY mcp/requirements.txt /app/mcp/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp/requirements.txt

# 只拷 server 真正需要的：查询库 + 契约 + 共享工具。
# 刻意不拷 chaos/、dr-plan-generator/ 里的执行侧代码——
# 这个 server 是**只读**的，不该有能发起故障注入的代码在镜像里。
COPY profiles/            /app/profiles/
COPY shared/              /app/shared/
COPY rca/neptune/         /app/rca/neptune/
COPY rca/__init__.py      /app/rca/__init__.py
COPY mcp/                 /app/mcp/

# dr-plan-generator 的 5 条 DR 查询由 query_catalog 用 importlib 按路径加载
COPY dr-plan-generator/graph/  /app/dr-plan-generator/graph/

RUN find /app -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 非 root 运行
RUN useradd -r -u 1000 -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ping',timeout=4).status==200 else 1)"

CMD ["python", "agentcore_app.py"]

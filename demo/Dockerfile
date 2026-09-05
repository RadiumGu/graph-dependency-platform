# 展示界面容器镜像。
#
# 构建上下文是 **仓库根目录**，不是 demo/ ——因为界面会 import
# rca/、chaos/code/、dr-plan-generator/、profiles/ 里的模块。
#
#   docker build -f demo/Dockerfile -t graphdp-demo:latest .
#
# 目标平台是 arm64：PetSite 集群跑的是 Graviton 节点组。
# 在 x86 机器上构建时加 --platform linux/arm64。
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REGION=ap-northeast-1 \
    AWS_DEFAULT_REGION=ap-northeast-1

WORKDIR /app

# 先装依赖，利用层缓存
COPY demo/requirements.txt /app/demo/requirements.txt
RUN pip install --no-cache-dir -r /app/demo/requirements.txt

# 界面依赖的仓内模块
COPY profiles/            /app/profiles/
COPY rca/                 /app/rca/
COPY shared/              /app/shared/
COPY chaos/code/          /app/chaos/code/
COPY dr-plan-generator/   /app/dr-plan-generator/
COPY demo/                /app/demo/

# 去掉可能带进来的字节码缓存
RUN find /app -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

WORKDIR /app/demo

EXPOSE 8501

# baseUrlPath=streamlit 由 .streamlit/config.toml 提供，与 ALB 的
# /streamlit* 规则和目标组健康检查路径 /streamlit/_stcore/health 对齐。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/streamlit/_stcore/health',timeout=4).status==200 else 1)"

CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]

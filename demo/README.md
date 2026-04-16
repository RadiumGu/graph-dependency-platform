# Graph Dependency Platform — Demo

基于 Streamlit 的多页面 Demo 应用，用于向客户展示 Graph Dependency Platform 的核心能力。

## 功能页面

| 页面 | 功能 |
|------|------|
| 🏠 首页 | 平台架构概览、关键指标、Schema 浏览 |
| 🕸️ Graph Explorer | Neptune 拓扑可视化（pyvis 交互图） |
| 💬 Smart Query | 自然语言 → openCypher 查询（聊天 UI） |
| 🔍 根因分析 RCA | Graph RAG 故障根因分析报告 |
| 💥 混沌工程 | 混沌实验历史与服务韧性评估 |
| 🛡️ DR 计划 | 灾备切换计划生成（AZ/Region/Service） |

## 快速启动

### 1. 安装依赖

```bash
cd /home/ubuntu/tech/graph-dependency-platform
pip install -r demo/requirements.txt
```

### 2. 配置 AWS 凭证

应用需要访问：
- **Amazon Neptune** — 图谱查询（需要 VPC 内访问）
- **Amazon Bedrock** — Claude Sonnet 4.6（Smart Query / RCA 功能）

```bash
# 方式一：使用 AWS CLI 配置
aws configure

# 方式二：使用 IAM Role（推荐生产环境）
# 确保当前 EC2/ECS 实例有以下权限：
#   - neptune-db:*
#   - bedrock:InvokeModel
```

### 3. 设置环境变量（可选，已有默认值）

```bash
export NEPTUNE_ENDPOINT="petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com"
export REGION="ap-northeast-1"
export BEDROCK_MODEL="global.anthropic.claude-sonnet-4-6"
```

### 4. 启动应用

```bash
cd /home/ubuntu/tech/graph-dependency-platform/demo
streamlit run app.py --server.port 8501
```

浏览器访问：http://localhost:8501

## 目录结构

```
demo/
├── app.py                          # 主入口（首页）
├── requirements.txt                # Python 依赖
├── README.md                       # 本文件
└── pages/
    ├── 1_Graph_Explorer.py         # Neptune 图谱可视化
    ├── 2_Smart_Query.py            # NL 图查询
    ├── 3_Root_Cause_Analysis.py    # RCA 分析
    ├── 4_Chaos_Engineering.py      # 混沌工程
    └── 5_DR_Plan.py                # DR 计划生成
```

## 网络要求

Neptune 需要通过 VPC 内网访问。如果在 VPC 外运行 Demo，需要：

```bash
# 通过 SSH 隧道转发 Neptune 端口
ssh -L 8182:<neptune-endpoint>:8182 ubuntu@<bastion-host>

# 然后修改环境变量使用 localhost
export NEPTUNE_ENDPOINT="localhost"
```

## 离线模式

部分页面在 Neptune / Bedrock 不可达时提供降级展示：

- **DR 计划**: 自动展示本地预生成示例（`dr-plan-generator/examples/`）
- **Graph Explorer**: 显示连接错误和配置说明
- **Smart Query**: 显示错误信息和排查提示

## 依赖的项目模块

Demo 应用通过 `sys.path` 导入以下模块：

| 模块路径 | 用途 |
|---------|------|
| `rca/neptune/neptune_client.py` | Neptune openCypher 查询 |
| `rca/neptune/neptune_queries.py` | Q1–Q18 预定义查询 |
| `rca/neptune/nl_query.py` | NL → openCypher 引擎 |
| `rca/core/graph_rag_reporter.py` | Graph RAG RCA 报告 |
| `dr-plan-generator/` | DR 计划生成 |
| `chaos/code/neptune_sync.py` | 混沌实验数据 |

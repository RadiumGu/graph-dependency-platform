# Graph Dependency Platform — 展示界面

基于 Streamlit 的多页应用，用于向观众展示平台能力。

线上地址：https://rainmeadows.com/streamlit/ （ALB + Cognito 认证后）

---

## 设计原则

这三条是 2026-09-05 改造时定下的，改动界面时请遵守：

1. **数字一律现算，不硬编码。** 节点/边类型数从 `profiles/graph_contract.yaml` 派生，
   查询数从 `rca/neptune/query_catalog.py` 的 `QUERY_CATALOG` 派生，故障数从
   `chaos/code/runner/fault_catalog.yaml` 派生。
   改造前首页写着「22 种节点类型 / 19 种边类型 / 18 个查询」，实际是 39 / 29 / 22，
   而且边类型那个 19 和它自己下面只列 16 行的表格互相矛盾。
2. **无凭证也要能看。** Neptune 不可达时回退到 `fixtures/` 里的**真实数据快照**，
   并在页面上**明确标注是快照**——不伪装成实时。一个讲「数据可信」的项目，
   界面上不能自己造假数据。
3. **不内嵌集群端点。** `NEPTUNE_ENDPOINT` 从环境变量取；缺失即进离线模式。
   改造前这个端点在 6 个文件里各硬编码了一遍。

---

## 页面

页面顺序刻意按「先讲最强的、先给最容易上手的」排：

| 页面 | 内容 | 无凭证可用 | 需要 |
|---|---|---|---|
| `app.py` 首页 | 主张、实时验证计分板、四种业界范式对照、证据卡 | ✅ 完全可用 | — |
| `1_Edge_Verification` | **核心**：依赖边的 confirmed / refuted / inconclusive / untested、置信度、判定规则 | ✅ 真实快照 | Neptune（可选） |
| `2_Query_Catalog` | 预置查询浏览器，选查询→填参→执行。**不经过 LLM** | ✅ 8 条查询有真实结果快照 | Neptune 才能实跑 |
| `3_Graph_Explorer` | pyvis 拓扑图，节点类型从契约动态生成，被证伪的边画成红色虚线 | ✅ 真实快照 | Neptune（可选） |
| `4_Smart_Query` | 自然语言 → openCypher；可**Strands ReAct 工具调用链可逐轮展开 引擎**（token / ReAct 轮数 / 工具调用链） | ⚠️ 展示契约 30 组 few-shot 问题→Cypher 对照 | Neptune + Bedrock |
| `5_Agent_Dependencies` | agent 域 6 类节点、5 类边、孤岛问题与唯一桥接路径 | ✅ 真实快照 | Neptune（可选） |
| `6_Root_Cause_Analysis` | **证据面板**（9 条图查询，不需要 AI）+ Graph RAG 报告 | ✅ 证据面板有 3 个服务的真实快照 | 证据要 Neptune；报告要 Bedrock |
| `7_Chaos_Engineering` | 故障目录（现算）、实验规格、运行器 6 阶段说明 | ✅ 目录与规格可看 | Neptune 看历史 |
| `8_DR_Plan` | DR 切换计划，示例指标从 JSON 现算 | ✅ 有 fixture 回退 | Neptune 才能新生成 |

---

## 本地启动

```bash
cd <repo>/demo
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 离线模式（不需要任何 AWS 凭证，展示 fixtures/ 里的真实快照）
streamlit run app.py

# 实时模式（需要能访问 Neptune 的网络位置 + 读权限）
NEPTUNE_ENDPOINT=<cluster-endpoint> REGION=ap-northeast-1 streamlit run app.py
```

访问 http://localhost:8501/streamlit/ （`baseUrlPath` 在 `.streamlit/config.toml` 里设为 `streamlit`）。

---

## 环境变量

| 变量 | 作用 | 不设时 |
|---|---|---|
| `NEPTUNE_ENDPOINT` | Neptune 集群端点（**不要**带 `:8182`） | 进离线快照模式 |
| `REGION` | AWS 区域 | `ap-northeast-1` |
| `BEDROCK_MODEL` | Smart Query / RCA 用的模型 | `global.anthropic.claude-sonnet-4-6` |
| `NLQUERY_ENGINE` | `strands` | 由 `rca/engines/factory.py` 构造。2026-09-20 起只有 strands 一种实现，direct 已删除、回退分支已去掉 —— 不可用时直接抛异常 |
| `DEMO_ALLOW_INJECTION` | 设为 `1` 才在混沌页显示真实注入入口 | **不显示**（线上刻意不设） |

> ⚠️ `DEMO_ALLOW_INJECTION` 是安全闸门。改造前混沌页有一个 `subprocess.Popen`
> 直接对生产发起故障注入的按钮，只隔了一个提示框。这个页面挂在公网入口后面，
> 风险与收益不成比例。

---

## 离线快照

`fixtures/*.json` 是从活图谱抓的**真实数据**快照，不是编的：

| 文件 | 内容 |
|---|---|
| `graph_stats.json` | 各标签节点数、各类型边数、总计 |
| `verification.json` | 依赖边验证状态汇总 + 已判定边的明细（含实验 ID、退化幅度、判定理由） |
| `agent_graph.json` | agent 域节点与边 |
| `sample_topology.json` | Graph Explorer 用的拓扑子集 |
| `query_samples.json` | 8 条无参数查询的真实执行结果 |
| `rca_evidence.json` | 3 个服务（petsite / petsearch / payforadoption）各 9 条证据查询的真实结果 |
| `services.json` | Microservice 清单（含 tier / az） |
| `edge_sources.json` | 边按数据源的分布 |

刷新（需要能访问 Neptune）：

```bash
cd demo
NEPTUNE_ENDPOINT=<cluster-endpoint> python3 fixtures/refresh_fixtures.py
```

脚本只执行 `MATCH` 查询，不写图。每份快照带 `captured_at`，界面会把这个时间显示给观众。

> ⚠️ **验证判定会随时间变化。** 实测同一天内 `refuted` 从 3 条变成 0 条——
> 因为 2026-09-05 引入了「独立证据门禁」：只要该边被任何独立观测源看到过
> （如 DeepFlow 调用计数非零），就永不得判 refuted，那 3 条被主动撤销成
> `inconclusive`。所以界面上任何数字都不能硬编码，`refuted = 0` 也不代表
> 这套机制没在跑。

---

## 部署（现状）

线上是**手工维护的 systemd 服务**，不在任何 IaC 里：

| 项 | 值 |
|---|---|
| 主机 | `i-022fb7c32b71c72d9`（`openclaw-instance-v2`，10.1.2.198） |
| 主机 VPC | `vpc-06731f30388b57818` —— **不是** PetSite VPC，跨 VPC peering 访问 Neptune |
| 代码路径 | `/home/ubuntu/tech/graph-dependency-platform/demo` |
| 服务 | `streamlit-demo.service`（`User=ubuntu`，`~/.local/bin/streamlit run app.py`） |
| 入口 | ALB `Servic-PetSi-by0kpyBtxswj` :443 规则 priority 20（`/streamlit`、`/streamlit/*`）→ `streamlit-demo-tg`:8501，含 `authenticate-cognito` |
| 健康检查 | `/streamlit/_stcore/health` |

更新流程（2026-09-24 实测走通，从本地经 SSM 下发，**不需要 SSH**）：

```bash
# 1) 本地改完先推远端 —— 服务器只从 origin/main 拉，不接受手工改代码
git push origin main

# 2) 服务器拉取 + 重启（经 SSM，instance-id 见上表）
aws ssm send-command --region ap-northeast-1 \
  --instance-ids i-022fb7c32b71c72d9 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "sudo -u ubuntu git -C /home/ubuntu/tech/graph-dependency-platform pull --ff-only origin main",
    "systemctl restart streamlit-demo",
    "sleep 12",
    "curl -s -o /dev/null -w \"health=%{http_code}\\n\" http://127.0.0.1:8501/streamlit/_stcore/health"
  ]'
```

两个**必须照做**的细节，都是踩过才知道的：

- **`sudo -u ubuntu` 不能省。** SSM 以 root 执行，而仓库属 `ubuntu`，
  root 直接跑 git 会被拒：`fatal: detected dubious ownership in repository at ...`。
  git 建议的 `git config --global --add safe.directory` **不要用** ——
  那是把检查关掉，而这里真正该做的是用文件属主的身份操作。
- **`--ff-only` 不能换成裸 `git pull`。** 服务器上的工作树必须始终是
  origin/main 的镜像。`--ff-only` 在有人直接改了服务器代码时会**失败**，
  而裸 `git pull` 会合并出一个只存在于这台机器上的版本 —— 那之后线上跑的
  就不再是任何一个提交，出问题时无从复现。

依赖变了才需要补一步（streamlit 是 user 安装，在 `~/.local`）：
`sudo -u ubuntu pip install --user -r demo/requirements.txt`

回滚：把 `--ff-only origin main` 换成 `reset --hard <上一个 sha>` 再重启。
拉取前先记下 `git rev-parse HEAD` 作为回滚点。

> ALB 监听规则是手工加的、不在 CloudFormation 里（见
> `todo/deploy-result_20260830-1530.md` 记录的监听子树漂移）。改动 ALB 时要知道这一点。
>
> ⚠️ 同一路径上还有一条 **priority 1** 的规则：`/streamlit*` +
> header `X-Demo-Bypass: <token>` → 直接 forward，**不过 Cognito**。
> 它优先级高于上表那条 priority 20，带上该头即可免认证访问。
> 属演示期残留的授权旁路，清理演示环境时应一并删除。

**更好的落位方案**（Neptune 与 EKS PetSite 同 VPC，可去掉跨 VPC 一跳并改用 IRSA）
见 `todo/webui/02-落位方案_20260905-0530.md` 与 `infra/k8s/streamlit-demo.yaml`。
该清单 **尚未生效**（2026-09-24 核实：PetSite 集群上没有任何 streamlit 部署，
清单里的 image 仍是 `:REPLACE-TAG` 占位符）—— 本节描述的 systemd 形态才是线上现状。

---

## 共享模块 `_common.py`

改造前 5 个页面各自重复 `sys.path` 注入 + `os.environ.setdefault` 三件套。现在统一走：

```python
import _common as C

C.page_setup("页面名", icon="🎯")   # 必须在任何其他 st.* 之前
C.sidebar()                          # 导航 + 契约摘要 + 连接状态

C.schema_counts()          # 节点/边/来源数量（现算）
C.dependency_edge_labels() # 契约里标 dependency: true 的边类型
C.verification_rubric()    # 证据权重与阈值
C.query_catalog_info()     # 查询条目
C.fault_catalog_counts()   # 故障目录按后端拆分

C.neptune_online()         # 可达性探测（缓存 120s）
C.gquery(cypher)           # 执行查询，永不抛异常，返回 {"results"} 或 {"error"}
C.graph_stats()            # → (data, mode)  mode ∈ live/snapshot/none
C.verification_data()      # → (data, mode)
C.mode_badge(mode)         # 把 live/snapshot 明确告诉观众
```

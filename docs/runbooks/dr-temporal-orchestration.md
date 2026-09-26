# 灾备编排链交接文档

状态截至 **2026-09-26 14:40 UTC**。本文只写**已验证**的事实，推测与未验证项单独标注。

---

## 1. 这条链现在能做什么

一条可审计的容灾切换编排链，跑在韩国（ap-northeast-2）自托管 Temporal 上：

```
东京图谱（Neptune）
   ↓ 受审查询目录（graph_dependency_mcp，MCP over AgentCore，Cognito JWT）
图快照 → S3  ← Schedule dr-graph-snapshot，6h 一次
   ↓
DrPlanWorkflow：draft → approved → drilling → drilled → executing → executed
   ↑ 全部修改经 temporal-mcp 的 update_workflow（受 validator 保护）
   ↓
DrFailoverWorkflow + 四个原子步骤子 workflow
```

### 已实测通过的

| 项 | 证据 |
|---|---|
| Temporal Server 1.32.0 | `cluster health` SERVING、HTTP 7243 → 200、UI 8080 → 200 |
| 命名空间保留期 | 720h（归档刻意未开，见 §4） |
| worker 双队列 | `dr-plan-queue` 与 `dr-snapshot-queue` 各有 poller，PID 一致 |
| 8 个自定义 Search Attribute | worker 启动时 `ensure_search_attributes` 报「齐备（8 个）」 |
| 图谱 MCP 路径 | 在韩国 EC2 上以**实例角色**跑 `probe_graph_mcp.py` → 退出码 0 |
| 第一份真快照 | `snapshots/graph-ap-northeast-1-01a0de24.json`，60905 字节，120 行，契约版本 1 |
| `DrPlanWorkflow` | `plan_state` 查到 `state=draft`，正文写入 `plans/real/20260926-1440/v1.md` |
| 健康 cron | 「dr-plan-queue 有 4 个 poller，720h。图快照：最新快照 0.0 小时前，共 1 份」 |

### 快照内容（第一份的实际数字）

```
q15_critical_path            34      q12_service_dependency_tree  65（3 个去重服务）
q13_data_layer_topology       9      q2_tier0_status               6
q16_single_point_of_failure   6      q14_cross_region_resources    0 ← 见 §3
cross_region_replication：petsite-global，ap-northeast-1 writer / ap-northeast-2 fwd=disabled
```

---

## 2. 五个已定案的决定（不要重新论证）

### ① 不直连 Neptune，走受审的查询目录

理由是**查询质量**，不是网络更干净。直连意味着 worker 自己写 openCypher，
那样快照记录的不是「图谱事实」而是「某次临时查询的偶然结果」。
`graph_dependency_mcp` 暴露固定 `QUERY_CATALOG`（24 个工具），每条结果带
`_provenance`（`source` / `graph_contract_version` / `query` / `params` /
`queried_at` / `determinism`）。

**缺什么事实应当往目录里加一条**（一次对版本化契约的受审改动），
不要在 worker 里写临时查询 —— 那会抹掉这个决定的全部意义。

`network-korea-to-neptune.sh`（2 条路由 + 1 条安全组规则）**已不是推荐路径**，
建议 `--revert`。同理 `iam-grants.sh --revert neptune-read`。

### ② 用现有 Cognito m2m client，不另建 SigV4 runtime

两条都可行（韩国 `temporal_mcp` 就是 `authorizerConfiguration: null` 即 SigV4）。
选现有这条的决定性理由：**另建 runtime 会造出同一个事实源的两份副本**。
2026-09-26 在 petsearch 上已经被这个咬过 —— 两份 `SearchController.java`，
改了不被构建的那份，修复静默无效。两个 MCP runtime 同时提供「图谱事实」，
后果相同：有人更新目录，灾备快照继续读旧的，**一切看起来正常**。

附带：JWT 路径由 Bearer token 授权，worker **不需要**
`bedrock-agentcore:InvokeAgentRuntime` —— 授权面比另建 runtime 更小。

### ③ 事实来源分工

| 问题 | 权威来源 |
|---|---|
| 什么依赖什么、什么顺序切 | 图谱（东京拓扑） |
| 灾备侧存在吗/健康吗/切过去了吗 | AWS API（`rds:Describe*` / `eks:Describe*`，当下） |

图谱是单区域的（31 种边标签里没有 `ReplicatedTo`，韩国 Aurora 从集群不在图里）。
**这不是缺陷，ETL 不必改**：东京的样子就是韩国该建什么的规格说明，
韩国的状态是计划的输出而不是输入。

更强的理由：灾备侧状态**主动不该**从图谱取。图谱是周期刷新的 ETL 产物，
一个 6 小时前的「从库健康吗」视图**比没有视图更糟** —— 它读起来正常而实际是错的。

### ④ 唯一的硬闸门是版本三等

真执行要求 `drilled_version == approved_version == current_version`。
做成硬闸门是因为它**可验证**（三个版本号都是 workflow 自己的状态）。

**四眼原则刻意不做成闸门**：`author` / `approver` 都是调用方自报，
AgentCore 不透传身份，Temporal 的 `identity` 同样自报。
所以**记录而不阻止** → `plan_state` 返回 `same_person_revised_and_approved`
与 `identity_is_authenticated: false`。

原则：**可验证的强制，不可验证的记录。**

### ⑤ 发布与部署分在两台机器上

实例角色对 `worker/*` 只有 `GetObject`。**不要给它 `PutObject`。**
一个能改写自己下次要执行什么的进程，它的「已审核、已演练」结论一文不值。
`deploy-worker.sh` 因此必须显式指定 `--publish`（操作者机器）或
`--provision`（worker 主机）。

---

## 3. 一个必须知道的陷阱

`q14_cross_region_resources` 返回 **0 行**，而这不是「缺一个事实」，
是**一个读起来正好相反的事实**。

问「什么跨区复制了？」得到空答案 → 直白的生成器会推出「没有任何复制，
所以要建立复制」。而实测 `petsite-global` 已经横跨两区且 `available`。

代码里已用 `EMPTINESS_READS_AS_OPPOSITE` 标注，快照里也写进 `emptiness_caveats`。
**复制关系的权威来源是 RDS API，不是这条查询。**

---

## 4. 已知未完成 / 刻意不做

| 项 | 状态与原因 |
|---|---|
| 归档（archival） | **刻意未开**。服务端配的是 `filestore` 指向 `/tmp/temporal_archival` —— 写到单点 EC2 的 `/tmp` 比不开更糟 |
| serverless worker | 三项前置：网络可达 ✓ 已核实；`sts:AssumeRole` 与 invocation role **不该现在做** —— 模板要求 `AgentRuntimeARNs` 指向一个 worker Runtime，而那样的 runtime 还不存在。填通配就是过宽授权 |
| `pgdata.old-20260926-111554` | **先别删**。它是 EBS 快照 `snap-0566ddf3da1b75996` 之外唯一能就地回到 1.29.7 数据的东西 |
| temporal-mcp 的写通道边界 | `signal_workflow` 没有 validator，今天就能让 agent 自己投递数据库提升裁决 —— 而那个决策点存在的全部理由是「必须由人裁决」。建议部署时配 `TEMPORAL_DENY_TOOLS` 挡掉 `signal_workflow` / `terminate_workflow` / `cancel_workflow` / `delete_schedule`，让裁决只走 `update_workflow` |
| 真执行 | 从未真跑过。只跑过 `dry_run` 与冒烟 |

---

## 5. 假判据清单 —— 这是最该带走的东西

本工作线撞上的每一个严重缺陷都是同一个形状：**一个判据在成功和失败时给出相同输出。**

| 假判据 | 分不出什么 | 怎么暴露的 |
|---|---|---|
| 「worker 在队列上接单」 | 新代码 / 旧代码（进程还拿着旧模块） | 改了 `would_run`，部署后 md5 对得上而返回值是旧的 |
| 「安全组规则存在」 | 通 / 不通 | 规则加上了但路由缺回程 |
| 「`simulate-principal-policy` 报 allowed」 | 策略里有这条 / 真实请求会被放行 | `neptune-db` 是字面匹配，`/database` 报 implicitDeny 而 `/*` 报 allowed |
| 「secret 建好了」 | 值对 / 值空、键名错、scope 不符 | 只有真换一次 token 才知道 |
| 「git 提交里有这个修复」 | 改的是被构建的那份源码 / 不被构建的副本 | `Dockerfile` 只 COPY 上一层，两份文件连 SDK 版本都不同 |
| 「`py_compile` 通过」 | 语法对 / 名字都有定义 | pyflakes 报出 3 个未定义名字 |
| 「管道后面 `echo $?` 是 0」 | python 成功 / **`head` 成功** | 崩掉的探针报了退出码 0 |
| 「`workflow list` 显示 Running」 | 在推进 / **activation 无限失败** | 只有 `TemporalReportedProblems` 搜索属性露底 |
| 「发布脚本报同步成功」 | 发布了新代码 / **CDN 缓存里的旧代码** | `provision` 随后说「应用代码未变」，看起来像「已最新」 |
| 「`raise ApplicationError` 会明确失败」 | 报告失败 / **报告失败的代码自己崩** | `ApplicationError` 不在 `temporalio.workflow` 里 |

最后一条最值得记：它在 `DrFailoverWorkflow` 里，位置是「等人工裁决超时」。
**真实灾难中没人裁决时，切换不会明确失败，而是永远 RUNNING。**
而它旁边的注释恰恰声称「明确失败，且把为什么失败写进异常」。
**注释声称的性质，代码并不具备** —— 这是最难发现的一类缺陷，
因为读代码的人会相信注释。

对应的工程纪律，已落进所有脚本：验证做成**三态**（成功 / 明确失败 /
**无法判断**），第三种以退出码 2 结束，**不当成通过**。

---

## 6. 常用操作

各脚本在哪台机器上跑见 `infra/dr-korea/temporal-1.32/README.md` 开头那张表。

```bash
# 发布 worker 代码（操作者机器）
# 用 commit SHA 而不是分支名：SHA 是内容寻址的，绕开 raw.githubusercontent 的 CDN 缓存
SHA=$(git rev-parse origin/main)
DR_CODE_BUCKET=dr-korea-agentcore-926093770964-ap-northeast-2 \
  bash deploy-worker.sh --publish --ref "$SHA"

# 部署（韩国 EC2，经 SSM）
sudo bash deploy-worker.sh --provision

# 验证图谱 MCP 那条路（在韩国 EC2 上跑才证明 worker 能走通）
/opt/dr-worker/venv/bin/python /opt/dr-worker/app/probe_graph_mcp.py

# Schedule（子命令是 toggle，不是 unpause）
$TC schedule toggle --schedule-id dr-graph-snapshot --unpause --reason '...'
$TC schedule trigger --schedule-id dr-graph-snapshot
```

`$TC` 必须走 **admin-tools 容器** —— `server:1.32.0` 镜像里没有 `temporal` CLI：

```bash
C=$(cd /opt/temporal && docker compose ps -q temporal | head -1)
NET=$(docker inspect "$C" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
TC="docker run --rm --network $NET temporalio/admin-tools:1.32.0 temporal --address temporal:7233 --namespace default"
```

---

## 7. 环境坐标

| 项 | 值 |
|---|---|
| Temporal EC2 | `i-09380e417a0177ed4`（10.20.1.10，无公网 IP，经 SSM） |
| 实例角色 | `dr-korea-temporal-TemporalRole-MbW4WYmjoR8J` |
| 端口 | gRPC 7233 / HTTP 7243 / UI 8080 |
| 队列 | `dr-plan-queue`（切换）、`dr-snapshot-queue`（快照） |
| 桶 | `dr-korea-agentcore-926093770964-ap-northeast-2`（`plans/` `snapshots/` `worker/`） |
| 图谱 MCP | `graph_dependency_mcp-12Vg2Z9XXu`（ap-northeast-1，Cognito JWT） |
| Cognito | 池 `ap-northeast-1_Dwd1wVX7j`，域 `graphdp-mcp-1788589178`，client `graphdp-mcp-m2m`，scope `graphdp-mcp/invoke` |
| 凭证 | Secrets Manager `dr-graph-mcp-m2m` @ **ap-northeast-1**（与发它的 Cognito 池同区） |
| 回滚快照 | `snap-0566ddf3da1b75996`（根卷 `vol-05fc30731946934cd`） |

角色上的五条内联策略，每条一个理由，可分别撤销：
`dr-plan-write`（计划正文）、`dr-snapshot-write`（快照）、
`dr-graph-mcp-secret-read`（MCP 凭证）、`dr-neptune-read`（**已不需要，建议撤**）、
`dr-orchestrator-readonly`（只读探查）。

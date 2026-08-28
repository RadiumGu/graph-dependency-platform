# 东京区环境现状 — 变更后更新

> 更新时间:2026-08-28 16:30 UTC
> 前置文档:`tokyo-vpc-deployment-status_20260828-1440.md`(**只读盘点**,14:40 快照)
> 本文档记录 15:30–16:30 之间对**生产环境的实际变更**及其验证结果
> 账号 926093770964 · ap-northeast-1 · 身份 IAM user `Radium`
>
> ⚠️ 与前置文档的关系:前置文档是**带时间戳的只读证据**,不做原地修改;
> 凡结论已被本次变更推翻的,在下方「结论修订」一节列出。

---

## 0. 一句话总结

前置文档第 2 条「**分析侧完全静默**」已解除:告警 → 缓冲 → 窗口聚合 → RCA → 图谱回写 → 决策
**全链路首次贯通并可重复验证**。过程中发现并修复 **11 个此前从未被触发过的缺陷**,
其中 9 个是静默失败(日志级别过低或异常被吞),静态审查发现不了。

**S3 Vectors 仍然必需** —— 它是两条语义检索路径里唯一会自我积累的一环(详见 3.5 节)。

---

## 1. 结论修订(推翻前置文档的部分)

| 前置文档结论 | 修订后 | 依据 |
|---|---|---|
| #2 分析侧完全静默,7 天 0 次调用 | **已解除**。`gp-window-flush` 有史以来首次成功执行;当日新增 Incident 由 0 → 8 | flush 日志 `WindowFlush complete: processed=1 failed=0`;图谱 Incident 计数 |
| #7 相关:`neptune-client-base` layer「架构绑定,迁 ARM 需重建」 | **判断错误,已纠正**。该 layer 是**纯 Python**(仅 `python/neptune_client_base.py`,71 行,零编译产物),**架构无关**,无需重建 | 下载线上 layer v2 解包核实:2 个文件,无 `.so` |
| README「全栈 ARM64」 | **Lambda 层此前为假**。9 个函数中此前 0 个 arm64;现 3 个已迁移,6 个仍 x86_64 | `ListFunctions` 实测 |
| 「三条语义检索路径全断」(我 16:15 的说法) | **过度悲观**。其中 Bedrock KB 路径的**代码是好的**,只是环境变量指向了一个已死的 KB;改一个变量即恢复 | 实测活跃 KB 返回 2 条命中 |

---

## 2. 生产变更清单(全部已执行)

### 2.1 IAM(6 条新增内联策略,全部**增量**添加,未改写原有策略,便于单独回滚)

| 角色 | 新增策略 | 解决的问题 |
|---|---|---|
| `petsite-rca-lambda-role` | `rca-layer2-probes` | Layer2 探针 6 个全部 AccessDenied → 现 6/6 正常 |
| `petsite-rca-lambda-role` | `rca-neptune-delete` | `SET` 覆盖已有属性需 `DeleteDataViaQuery`(Neptune 授予「查询可能执行的动作全集」)→ 403 消除 |
| `petsite-rca-lambda-role` | `rca-vector-index-access` | `s3vectors:*` + Titan embeddings 调用 |
| `gp-window-flush-lambda-role` | `gp-window-flush-buffer-access` | **CDK 只给了 `Query`,代码用的是 `scan()`** → scan 失败被吞成「窗口无告警」,缓冲的告警静默丢弃 |
| `gp-window-flush-lambda-role` | `gp-window-flush-probes-and-neptune` | 探针 + `eks:DescribeCluster` + Neptune 属性覆盖 |
| `gp-window-flush-lambda-role` | `rca-vector-index-access` | 同上 |

### 2.2 Lambda 架构迁移(x86_64 → arm64)

| 函数 | 变更 | 说明 |
|---|---|---|
| `petsite-rca-engine` | arm64,SHA `bDVUO2sl…` | 与 EKS 数据面(t4g.large / Graviton2)一致 |
| `gp-window-flush` | arm64,SHA `RtppY8he…` | 同上 |
| `petsite-rca-engine-canary` | arm64(临时验证用,**待删除**) | — |

**迁移的真实动因**:生产包内含 4 个 **aarch64 编译产物**(`_yaml`、`charset_normalizer`、mypyc)
却运行在 x86_64 上 —— 架构不符时 Python 静默退化成纯 Python 回退实现。
根因是 `deploy.sh` 在 aarch64 构建机上直接 `pip install`,未做平台定向。
即架构与二进制**长期不一致**,迁 ARM 是把两者对齐,而非单纯为省 20% 成本。

剩余 6 个 x86_64:`neptune-etl-from-aws` / `-cfn` / `-deepflow` / `neptune-etl-trigger` /
`petsite-ops-slack-notifier` / `petsite-rca-interaction`。前三者挂 `neptune-client-base` layer
—— 已确认该 layer 纯 Python、架构无关,故迁移成本比原先估计的低得多。

### 2.3 环境变量修正

| 函数 | 键 | 原值 | 新值 | 后果 |
|---|---|---|---|---|
| `petsite-rca-engine` | `BEDROCK_KB_ID` | `0RWLEK153U`(已死) | `5P8D3WZMR0` | KB 检索由必然报错变为可用 |
| `gp-window-flush` | `BEDROCK_KB_ID` | **空串** | `5P8D3WZMR0` | KB 路径此前被完全跳过 |
| `gp-window-flush` | `SLACK_WEBHOOK_URL` | **空串** | (同 engine) | **分析完成但无人收到通知** |
| `gp-window-flush` | `SLACK_CHANNEL` | 键不存在 | `C0AHMLPCD7U` | — |
| `gp-window-flush` | `EKS_CLUSTER` | 键不存在 | `PetSite` | `action_executor` 拿空串 → 半自动动作无法定位集群 |

两个函数最终均为 **12 个环境变量**,其余键逐一保留(用 `GetFunctionConfiguration` 读全量后回写,
未使用 `deploy.sh`,因其 Step 3 会按 `.env` 重写全部 12 个变量)。

后三项是 **Fix C 暴露的配置回归**:RCA 的实际执行位置从 `petsite-rca-engine` 移到了
`gp-window-flush`,而后者从未被调用过,其配置缺口一直没有被暴露。

### 2.4 Bedrock Knowledge Base 清理

| 项 | 状态 |
|---|---|
| `0RWLEK153U` `petsite-rca-incident-kb` | **已删除** |
| `5P8D3WZMR0` `petsite-rca-incident-kb-rds` | ACTIVE,保留,现为唯一 KB |

删除前先按 AWS 的失败提示把 data source 的 `dataDeletionPolicy` 由 `DELETE` 改为 `RETAIN`
(改时必须把 `vectorIngestionConfiguration` 原样回传,否则报
`chunkingConfiguration cannot be updated once created`),再删 data source、再删 KB。

**该 KB 卡在 `DELETE_UNSUCCESSFUL` 的根因**:它的向量存储
AOSS 集合 `huntyqbzardq9py0itcj` **早已被删除**(账号内 AOSS 集合数 = 0),
Bedrock 无法去一个已消失的库里删数据。集合是唯一计费主体,所以孤儿 KB **不产生任何费用**。

---

## 3. 成本核实(120 天账单)

| 服务 | 金额 | 说明 |
|---|---|---|
| Amazon OpenSearch Serverless | **账单中完全不出现($0)** | 集合早已删除,证实孤儿 KB 零成本 |
| Amazon Bedrock | $0.01/月 | 可忽略 |
| Amazon RDS | 06 月 $136.89 → 07 月 $141.07 → **08 月 $269.18** | 见下 |

**RDS 单月接近翻倍**,时间与 `sqlreplay-verify` 集群吻合(aurora-mysql,0.5–8 ACU,
创建于 **2026-08-27**)。与本次任务无关,但金额显著,单列备查。

活跃 KB 的向量存在 `serviceseks2-databaseb269d8bb-efjeyzicx2ak`,标签确认为
**PetSite 应用自己的主库**(`aws:cloudformation:stack-name=ServicesEks2`、`System=petsite`、
`Tier=tier0`、`Environment=prod`、`connected-service=pethistory-deployment`)。
该库无论如何都要运行,KB 仅额外占用 `rca_kb.bedrock_integration` 一张表,边际成本≈0 —— **不可删**。

---

## 3.5 两条语义检索路径的分工(S3 Vectors 是否仍需要)

系统里有**两套**"检索相似历史故障"的机制。它们**不冗余**,因为写入方式根本不同:

| | S3 Vectors | Bedrock KB `5P8D3WZMR0` |
|---|---|---|
| 存储 | `gp-incident-kb` / `incidents-v1`(1024 维 cosine) | RDS pgvector,寄居 PetSite 主库 |
| 写入 | **自动**。`incident_writer.py:177` 在每次写 Incident 后调 `index_incident()` | **无自动写入路径**。需 `StartIngestionJob` 批量摄取 |
| 代码里是否有写入方 | 有 | **零**。全仓库无 `StartIngestionJob`、无对其 S3 数据源桶的写入 |
| 实际语料 | 19 条向量(18 条 4 月 + 今日新增 1 条) | **1 篇 2 月手写种子 md** |
| 性质 | 会随运行自我积累的**活语料** | 冻结的**种子语料** |

数据源桶 `petsite-rca-incidents-926093770964` 实际只有两个对象:
`incidents/inc-2026-01-15-seed001.md`(2026-02-28)与一个无关的 `scripts/*.py`。

**结论:S3 Vectors 必须保留** —— 它是唯一会积累新知识的一环。
Bedrock KB 的价值在于种子案例里有**人写的 Five Whys 与处置方案**(机器 RCA 报告没有这部分),
两者互补:KB 提供人类经验模板,S3 Vectors 提供本系统的历史实例。

成本可忽略:19 条 × 1024 维 × 4 字节 ≈ 78 KB。

### 已验证:向量写入恢复

```
[INFO] 2026-08-28T16:28:36Z  Indexed 1 chunks for incident inc-2026-08-28-33f3e1
```

索引 18 → **19 条**,这是 **4 个多月来第一条新向量**。

### 新发现的第 10 个缺陷:KB 检索的 IAM 动作前缀写错

`petsite-rca-lambda-role` 的 `rca-permissions` 里写的是 **`bedrock-agent-runtime:Retrieve`**,
但 IAM 中不存在这个服务前缀 —— KB 检索的正确动作是 **`bedrock:Retrieve`**。
即该语句**从未生效过**。`gp-window-flush-lambda-role` 则完全没有相关语句。

用 `SimulatePrincipalPolicy` 验证(不靠推断):

| 角色 | 修复前 | 修复后 |
|---|---|---|
| `petsite-rca-lambda-role` | `bedrock:Retrieve` → **implicitDeny** | **allowed** |
| `gp-window-flush-lambda-role` | `bedrock:Retrieve` → **implicitDeny** | **allowed** |

已为两个角色新增内联策略 `rca-kb-retrieve`(限定到该 KB 的 ARN,非 `*`)。
这解释了为何 flush 日志里**从来没有任何 KB 相关输出** —— 调用被静默拒绝,
而 `_query_kb_similar_incidents` 的异常被 `except` 吞掉,只在提示词里留下
「无相似历史案例(知识库暂无数据)」这句误导性文本。

---

```
SNS petsite-rca-alerts 发布
  → petsite-rca-engine 缓冲          0.36 s   （此前同步跑完整 RCA 需 34 s）
  → 创建一次性 EventBridge 定时器     gp-flush-<window>
  → 定时器触发 gp-window-flush        窗口到期后
  → flush_window 取出告警 → TopologyCorrelator → EventGroup
  → fault_classifier → rca_engine → graph_rag_reporter → DecisionEngine
  → 图谱回写:subgraph_pattern + causal_weight×3 + MentionsResource
  → Incident 写入
  → DecisionEngine: P1 / conf=0.70 → semi_auto action=restart_pod
  → buffer 清空、定时器自删（ActionAfterCompletion: DELETE）
```

已重复验证 4 轮,每轮 `processed=1 failed=0`。
`DecisionEngine → semi_auto → restart_pod` 这一环**此前从未被执行过**。

**性能**:告警接收路径 34 s → 0.36 s(降 99%)。100 条告警不再触发 100 次完整 RCA。

---

## 5. 本次发现的 9 个缺陷

| # | 缺陷 | 类型 | 状态 |
|---|---|---|---|
| A | Layer2 探针 6 个全部缺 IAM 权限 | 权限 | ✅ 已修并验证 |
| B | Neptune 属性覆盖缺 `DeleteDataViaQuery` → 403 | 权限 | ✅ 已修并验证 |
| C | 无告警聚合,每条告警独立触发完整 RCA | 设计 | ✅ 已修并验证 |
| D | `alert_buffer.py` 用 `fingerprint`,表主键是 `alert_fingerprint` | **静默** | ✅ 已修 |
| E | 代码读 `WINDOW_FLUSH_LAMBDA_ARN`,实际设 `WINDOW_FLUSH_FUNCTION_ARN` | **静默**(debug 级) | ✅ 已修(兼容双名) |
| F | `gp-window-flush` 缺 `dynamodb:Scan`(CDK 只给 `Query`) | **静默**(异常被吞) | ✅ 已修 |
| G | `SLACK_WEBHOOK_URL` 空 → 分析完不通知 | **静默**(`if not webhook: return`) | ✅ 已修 |
| H | `EKS_CLUSTER` vs `EKS_CLUSTER_NAME` 命名不一致 | **静默** | ✅ 已修(兼容双名) |
| I | `embed` 模块缺失(硬编码 `/home/ubuntu/...` 开发机路径) | **静默**(non-fatal) | ✅ 已修(内联实现) |
| J | 两个角色缺 `s3vectors:*` 权限 | **静默**(non-fatal) | ✅ 已修并验证(新向量已写入) |
| K | KB 检索 IAM 动作前缀写成 `bedrock-agent-runtime:Retrieve`(不存在),正确是 `bedrock:Retrieve` | **静默**(异常被吞) | ✅ 已修,策略模拟验证 |

**11 个里有 9 个是静默失败**。`put_alert` 原先把真实写入失败和去重命中都返回 `False`,
两者无法区分 —— 已改为 `raise`,否则丢告警却报成功。

这批缺陷的共性:**全部只在链路真正跑起来时才暴露**。
`gp-window-flush` 此前从未被调用,其权限、环境变量、代码路径没有任何一处被验证过。

---

## 6. 仍未解决(按优先级)

### P0 — 反馈闭环未闭合

图谱内 **132 个 Incident 全部是 `investigating`,0 个 `resolved`**。
`resolve_incident()` 实现正确(会 `SET status='resolved'` 并算 MTTR),
但唯一生产调用方是 `handler.py:71` 的 `action == 'resolve'` 分支,需外部显式传入;
设计上由 Slack 交互触发,而 `petsite-rca-interaction` 只有 **1 KB**,基本是空壳。

连带影响:

- `q5_similar_incidents` 过滤 `WHERE inc.status = 'resolved'` → **永远返回空**
- `q17_incidents_by_resource` 返回的记录 `resolution` 字段永远为空
- Bedrock 打分时「历史」维度恒为 0(`confidence_breakdown.history = 0`)

**结论:修 embed 只是把水管接通,水源(resolved 先例)还没开。**

### P1 — K8s RBAC 未授权

`eks:DescribeCluster` 已授予,token 能取到,但随后
`K8s pod query (label=petsite): HTTP Error 401 Unauthorized` ——
集群侧未给 `gp-window-flush-lambda-role` 建立 access entry / aws-auth 映射。
影响:Pod 状态采集与 `restart_pod` 动作均无法真正执行。

### P1 — CDK 漂移

已用 `update-function-code` / `update-function-configuration` 直接改了 CDK 管理的
`gp-window-flush`。已同步修正 CDK 侧(见第 7 节),但**尚未 `cdk deploy` 验证**。
`slackWebhookUrl` 故意**不写入 `cdk.json`**(它是机密,该文件进 git),
需部署时以 `-c slackWebhookUrl=...` 传入,否则会再次退化成空串。

### P1 — 前置文档遗留项

`petsite.yaml` schema header 28→26;schema 正文补声明 `AffectedService`(28 条)+
`Involves`(8 条),否则 `schema_prompt.py` 使其无法被自然语言查询触达;
补 6 个实测服务的 `language`/`runtime`;修正 CDK 中 Neptune 集群名与引擎版本。

### P2

`rca-alerts` 订阅存在但无对应权限(删冗余订阅或补权限);
`chaos-petstatusupdater-sr-critical` 无告警动作而其 4 个同类有;
DeepFlow 采集过滤掉 `logs.*.amazonaws.com`(76% 噪声);
补 S3/ECR VPC Gateway 端点;EKS API 公网 CIDR 从 `0.0.0.0/0` 收窄。

---

## 7. 代码改动(均在工作区,**尚未提交**)

| 文件 | 改动 |
|---|---|
| `rca/handler.py` | 接入告警缓冲;按 SNS / 直接 invoke 分流(直接 invoke 必须保持同步,`chaos/code/runner/rca.py` 的 `RCATrigger` 依赖同步结果) |
| `rca/core/alert_buffer.py` | 修主键名;兼容双环境变量名;失败改 `raise`;静默跳过提升为 `warning` |
| `rca/embed.py` | **新增**。Titan Embeddings v2,1024 维,带磁盘缓存 |
| `rca/chunker.py` | **新增**。heading-aware + 递归切分,中英混排 token 估算 |
| `rca/search/incident_vectordb.py` | 移除硬编码 `/home/ubuntu/tech/s3-vector-skill/scripts` |
| `rca/actions/action_executor.py` | `EKS_CLUSTER` 兼容 `EKS_CLUSTER_NAME` |
| `rca/deploy.sh` | 补 `shared/`+`engines/`;加 pydantic;平台定向 pip(默认 arm64);修静默吞掉的 `add-permission` 失败 |
| `infra/lib/alert-buffer-stack.ts` | 声明 `architecture: ARM_64`;补 `EKS_CLUSTER`;`slackWebhookUrl` 加机密说明 |
| `infra/lib/neptune-etl-stack.ts` | layer 声明 `compatibleArchitectures: [X86_64, ARM_64]` |
| `infra/cdk.json` | 补 `bedrockKbId: 5P8D3WZMR0` |
| `infra/lambda/rca_window_flush/**` | 同步上述 rca 侧修复(该目录是 rca 源码的副本,是漂移根源) |
| `README.md` / `README_CN.md` | 节点 31 / 边 26 / Chaos Mesh 19 / FIS 37 / 测试 277 / DR 5 阶段 / Graviton2 / 集群名 `PetSite` |

`embed.py` 与 `chunker.py` 已用真实 Bedrock 验证:1024 维、L2 范数 1.000000、
缓存命中 0.0005 s;chunker 契约测试(`tokens <= chunk_size + overlap`、长文本多块)全部通过。

---

## 8. 待清理的测试产物

- Neptune 内约 **8 个合成 Incident**,均可由描述中的
  `[SYNTHETIC TEST]` / `[CANARY TEST]` / `[FINAL VERIFY]` 识别
- `petsite-rca-engine-canary` 函数(验证完成,可删)
- 已向生产 SNS 发送若干合成告警,产生了真实 Slack 通知

图谱规模:14:40 时 867 节点 / 1341 边 → 本次变更后 **874 节点 / 1355 边**。

---

## 9. 一次失败与回滚(留档)

首次部署直接打生产,连撞两个缺失依赖(`shared/` 模块、`pydantic`),
**已立即回滚**至备份包并校验 SHA 一致(`NX9rexikAgQggCbPSAc3GE0G2zUEIWr/7wZtwraQWAw=`)。
此后改为 **canary 先验证**,后续 6 个缺陷全部在 canary 上暴露,生产零影响。

回滚资产保留在 `$KIROCREW_SCRATCH/rca-deploy/rollback-rca-engine.zip`
(生产原版,1507941 字节,12 个环境变量)。

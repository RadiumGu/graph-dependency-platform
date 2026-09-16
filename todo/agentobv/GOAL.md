# GOAL —— PetSite + AgentCore 重建（目标循环的权威状态文件）

> 目标循环每轮读这个文件。**改状态就改这里**，不要只写在对话里（压缩会丢）。

## 目标

把上游 `aws-samples/one-observability-demo` 的更新（含 genai/agent）追平到本地 PetSite 集群，
部署 AgentCore 及其观测，并让图数据库依赖关系最终与实际系统一致。

## 硬约束（违反即回滚，优先于任何便利性）

1. **Neptune 图数据库绝对不能删** —— 存累积依赖历史、RCA 记录、confirmed/refuted 边判定，重建不出来。ETL **可改不可删**。
2. **可删可重建的只有 EKS 里的 petsite 应用本身**。EKS 集群、Aurora、DynamoDB、S3、ALB 都不动。
3. **ALB 上不得新增公网访问入口 —— 公司安全会告警。** 新服务（petfood-rs、agent）一律走 internal ALB `petsite-internal-lt` 或 ClusterIP。
4. 所有容器必须跑在现有 **arm64 EKS，不得用 ECS**。上游已把 4 个服务（pet-search / petfood / petlist-adoptions / pay-for-adoption）搬到 ECS —— 我们**只移植应用代码，继续用本地 `eks-service.ts`**。
5. agent 尽可能用 **AWS 托管 AgentCore**（上游本就如此，`waggle-ai-agents-runtime.ts` 44 处 agentcore 引用）。
6. **保留 demo 的 1% 故障注入**（上游仍有，实现方式已变；移植后**核对注入率是否仍为 1%**）。
7. **Transaction Search 的索引率必须保持 100%，不得下调 —— 它是 `etl_xray` 的隐性依赖。**
   1% 索引会**选择性地**让低频事件驱动组件（StepFn 三个 Lambda、StateMachine、STS、三个 ETL
   Lambda）从 `GetServiceGraph` 整体消失，而那正是 `etl_xray` 的价值所在；症状是边悄悄变少、
   不报错。要压 TS 成本只能动 **head sampling（`FixedRate`，现为 0.05）**，它等比减少所有服务的
   span。实测与差集见下方「✅ 已定论：X-Ray 索引率是 `etl_xray` 的隐性硬依赖」一节及 `05` 末节。

## 进度

| Stage | 内容 | 工时 | 状态 |
|---|---|---|---|
| 0 | Step 0 四项验证 | — | ✅ 完成（`06`） |
| 1 | 必须存活清单 + 差异清点 | 3h | ✅ 完成（`07`） |
| 2 | 图契约扩展 + ETL 改造 | 6h | ✅ 完成（`08`） |
| **3** | **PetSite 应用追平** | ~~10h~~ **31h**（实测修正） | ✅ **6/6 完成**（六个服务全部合并，五个已 arm64 构建验证：payforadoption-go / petfood-rs / petsite / petsearch-java，petstatusupdater 走 npm test；petlistadoptions 保 Go 未改源码）（`09`） |
| **4** | **AgentCore 部署** | 8h | ✅ **完成**：5 Runtime + Gateway + 5 target 全部 READY，KB 灌入 10/10，Memory/Guardrail/PolicyEngine 已建，8 个 `/petstore/agent/*` 有值（`10`） |
| 5 | 观测部署 | 4h | ✅ **完成**：agent 依赖边已与实际系统一致（Delegates 2 / InvokesTool 5 / Retrieves 1），PENDING 名单已按纪律清空且测试仍全绿（`11`） |
| 6 | 压测 + 图谱验收 | 4h | ⚠️ **压测已通过（100% / 注入生效）、孤岛已接通**；仅剩 petfood 与 petsite 部署待用户定夺（`12`） |

**旁路已完成，且 TS 对 `etl_xray` 的影响面已于 10:36Z 结案**：Transaction Search 已开（2026-09-04 08:49:33Z ACTIVE，**索引率现为 100%，见硬约束第 7 条**，head sampling 保持 0.05），基线 `snapshots/before.json`（52 Services / 49 Edges）。判定用的是**集合差集**而非 52/49 计数对比 —— 详见下方「✅ 已定论：X-Ray 索引率是 `etl_xray` 的隐性硬依赖」。Stage 6 仍保留「重建后预期服务是否全部出现在 `GetServiceGraph`」这条更强判据，用于重建后的验收。

## 已定的决策（勿翻）

- `petlistadoptions` **保 Go**，不跟上游改 Python —— keep-alive 修复实测 **74 倍**（1.94→143.64 请求/连接），上游已重写为 Python 故**无对应文件**，改过去等于丢掉已实测成果并重新引入风险。
- **不采用**上游 `src/` 目录布局 —— 要动所有 CDK 路径、部署机脚本、CI 引用，零功能收益。
- adoption agent 用 **`jp.anthropic.claude-haiku-4-5-20251001-v1:0`** 替代东京没有的 Llama 4 Maverick。
- **模型改区是配置活**：设 5 个环境变量即可（`ORCHESTRATOR_MODEL_ID` / `NUTRITION_MODEL_ID` / `ORDERING_MODEL_ID` / `ADOPTION_MODEL_ID` / `CONCIERGE_MODEL_ID`），**不动一行 agent 代码**。
- 反向采纳上游 `stages/containers.ts` 的**多架构镜像构建流水线**，替掉本地手工构建。

## Stage 2 已完成 —— 关键结果（详见 `08`）

契约 **33→39 节点 / 26→29 边 / sources 11→12**。三个测试文件全绿（32 passed / 2 skipped）。
新 ETL `infra/lambda/etl_agentcore/neptune_etl_agentcore.py`（511 行）已写并本地空跑验证。

**Stage 3/4 必须记住的三件事**：
1. **暂停 ETL 用 `scripts/etl_schedule_pause.py --pause`**（自动发现，实测是 **4 条规则**不是 2 条：
   还有 `neptune-etl-cfn-daily` 与 `neptune-etl-xray-hourly`）。**在 Stage 3 真正开始拆应用时才执行**，
   现在执行只会让图白白变陈旧。重建完 `--resume` 再跑干净全量。
2. **Stage 4 部署完 AgentCore 后**，要把已有实例的类型从 `tests/test_11_schema_consistency.py`
   的 `PENDING_FIRST_INSTANCE` / `PENDING_FIRST_EDGE` 里**移除** —— 留着等于放弃存在性检查。
3. `etl_agentcore` 尚未部署成 Lambda（ETL 走 CDK `infra/lib/neptune-etl-stack.ts`）。
   AgentCore 部署后才有意义，安排在 Stage 4。

## ✅ 已定论：X-Ray 索引率是 `etl_xray` 的**隐性硬依赖**，不得下调（2026-09-04 10:36）

**实测三点对照**（前提校验全部通过：压测机始终 `running`、head sampling 始终 `0.05`）：

| 时点 | destination | 索引率 | 1h 窗口 (Services/Edges) |
|---|---|---|---|
| 08:42Z | XRay | —（TS 未开，索引规则不适用） | **52 / 49** |
| 09:51Z | CloudWatchLogs | **1%** | **30 / 26** ← 丢 55% |
| 10:36Z | CloudWatchLogs | **100%** | **53 / 50** ← **完全恢复** |

6h 窗口在 100% 下是 **56 / 54（去重后）**，0 丢失且净增。
⚠️ 该窗口的**原始**计数是 73 / 84 —— 跨 08:49Z 切换点的 6h 窗口会返回**重复条目**
（同一 `name|type` 出现多次；纯 pre-TS 的 `before` 6h 为 52/52 无重复）。最可能的解释是
该窗口同时命中 XRay 与 CloudWatchLogs 两个后端的图片段。**过渡期任何直接引用
`service_count` 的计数都虚高**，判据一律取去重集合。`etl_xray` 按 name upsert，不产生重复节点。

**判据不是计数回到 52/49，而是集合差集**：`after → idx100` 的 1h 差集为
**丢失 0 / 新增 23 服务 24 边**，其中 **22 服务 23 边精确命中 pre-TS 基线集合**
（StepFn 三 Lambda 各 3 条目 = 9、StateMachine、STS 及两条 `-> STS` 边、三个 ETL Lambda 各 3 条目
= 9、`search-service` / `pethistory-service` 两个 remote 端点）。唯一「基线没有、现在有」的
`payforadoption -> 9dw5r2dqlb.execute-api…` 是新出现流量，与本实验无关。
**若根因是摄入模式切换本身，提索引率不会让任何东西回来** —— 这一步才是判别性的。

**结论**：开启 Transaction Search 后，`GetServiceGraph` 的覆盖面**由 trace summary 的索引率驱动**。
1% 索引时低频事件驱动组件（StepFn 三个 Lambda、S3、STS、StateMachine）从服务图消失，
100% 索引时全部回来。

> 这是**实测推翻官方文档间接暗示**的一例。官方只说「索引一定百分比的 span 作为 trace summary」
> 并强调「100% 摄入保证完整可见性」，从未明说 service map 也受索引率影响 ——
> 我当时在 `05` 里基于两条间接证据推断「不影响」，**并明确标了 NOT FOUND（无官方明文）**。
> 幸好没当成已证实，否则会在 14:49Z 之后静默丢边而不知原因。

### 处置（已执行）

- **索引率保持 100%**（`aws xray update-indexing-rule --name Default --rule '{"Probabilistic":{"DesiredSamplingPercentage":100}}'`）
- head sampling **保持 0.05 未动** —— 它与索引率是两个独立的旋钮，动它会同时改变拓扑
- **14:49Z 的截止点已解除**，无需改 `etl_xray` 从 `aws/spans` 读拓扑
- `etl_xray` 三轮实测产出**没有下滑，反而在涨**：08:00Z `32/25`（pre-TS 基线）→ 09:00Z `33/26`
  → 10:00Z `34/28`，`edges_write_failed` 恒 0、`edges_deactivated` 恒 0、`edges_corroborated`
  24→25→26。原因是 24h 回看窗口目前绝大部分仍是 pre-TS 数据 —— 这正是 14:49Z 截止点存在的
  理由，不是「问题不存在」的证据。`xray_unmapped_types` 从 4 类涨到 6 类，多出的
  `('Database::SQL','sql.conn.exec')` / `('Database::SQL','sql.conn.reset_session')` 是 TS 带来的
  **新类型标注**（同名条目此前只以 `remote` 出现），属待映射项，与丢边无关。

### 两项非阻塞复核

1. ✅ **已确认（10:55:43Z 快照 `idx100_clean`，1h 窗口纯 100% 索引）：52 / 49，与 pre-TS 基线逐位相同。**
   ⚠️ **更正**：先前把残留的「3 服务 / 4 边」判为 head sampling 单窗口抖动，**这个判断是错的**。
   纯净窗口差集显示它们是**同一批实体换成了资源级身份**，丢 3 必然伴随新增 3：
   `S3|AWS::S3` → `serviceseks2-s3bucketpetadoption…|AWS::S3::Bucket`（**泛化服务名 → bucket 名**）、
   `DynamoDB|AWS::DynamoDB` → `…ddbpetadoption…|AWS::DynamoDB::Table`、
   `sql.conn.*|remote` → `sql.conn.*|Database::SQL`。去重后总数恰好不变 —— 计数完全没动、内容变了 6 处，
   又一次印证「只看计数会漏掉内容变化」。
   🎁 **附带退掉一个长期硬障碍**：记忆里「X-Ray 的 S3 节点名就叫 `S3`、不是 bucket 名，写不出精确
   S3 边」不再成立，TS 直接给出 bucket 名，且 `XRAY_TYPE_TO_RESOURCE_NODE` 里
   `'AWS::S3::Bucket': 'S3Bucket'` 映射**早已存在**，无需改码即生效（与 `xray_edges_seen` 25→28 一致）。
   ⚠️ 尚未逐边核实 Neptune 落地，只能从 `edges_corroborated` 24→26 推断，待复核。
   🔧 **一个低优先级副作用待修**：`XRAY_OPAQUE_LABELS` 的拦截写在 `if xray_type == 'remote':`
   分支内（`neptune_etl_xray.py:499`），`sql.conn.exec` 现在 type 是 `Database::SQL`，**护栏被绕过**。
   当前仍落到 `:519` 的 `return None`，**没造假节点**（`edges_skipped_no_node` 1→2 即此），
   但它们从「刻意丢弃的不透明标签」变成了「未映射类型」，污染了 `xray_unmapped_types`
   这个「应当补映射」的信号。修法：把该判定按**名字**前移到 type 分支之外。
   `Database::SQL` **不要**映射成节点 —— Name 是操作名，X-Ray 仍未给出 Aurora 集群标识，粒度没变细。
2. 跨 08:49Z 切换点的 6h 窗口**重复条目仍在**（`idx100_clean` 6h 原始 73/84、去重 56/54）。
   是否随窗口滑过 08:49Z 而消失 —— 用原 cron `906a5393`（14:51Z）复核，
   该作业的原判定用途已失效（问题已定论），改作此复核点。

### ⚠️ 留给未来的警告

**任何人下调 X-Ray 索引率，都会静默削弱依赖图谱**，而且症状是「边悄悄变少」而非报错。
`etl_xray` 只调 `GetServiceGraph` 一个 API，没有第二个源可以交叉验证这件事。
如果将来为省成本要下调，必须先把 `etl_xray` 改成从 `aws/spans` 读拓扑
（已确认 spans 里有完整数据，包含 1% 索引时从服务图消失的那些组件）。

**这件事之所以能查出来，全靠开启 TS 之前那份含完整服务集合与边集合的基线快照**
（`snapshots/before.json`）。只存计数的话，无法区分「组件消失了」与「本来就没有」。

## 🔥 环境陷阱：构建机封了出站 80 端口，**只有 443 通**（2026-09-04 11:05 实测）

```
deb.debian.org HTTPS → HTTP=200
deb.debian.org:80    → connection timed out (146.75.114.132)
```

**这条对剩余每个服务的构建都适用。** 凡构建期要出网的都必须走 443。
已实测可用：`goproxy.io`（HTTPS）、Alpine `apk`、ECR Public、crates.io。
**警惕任何默认走 http:// 的镜像源。**

### 它在 petfood 上的表现，以及为什么极易误判

上游 petfood 的 runtime 阶段做 `apt-get install ca-certificates openssl curl unzip`，
在本环境**必然失败，且失败方式互锁**：

1. Debian 默认源用 `http://` → 80 不通 → `apt-get update` 取不到索引，**但仍返回 0**，
   于是四个包全报 `Unable to locate package` —— **报错指向包名而非网络，极易误判成镜像坏了**
2. 把源改成 `https://` → `Certificate verification failed: certificate issuer is unknown`，
   因为 `bookworm-slim` **不自带 `ca-certificates`**（实测 `dpkg -l` 是 `un`）
3. 死锁：**要 HTTPS 得先有证书，要证书得先能 apt**

**解法（已落地）**：runtime 阶段**完全不联网**，从 builder 拷 CA 证书 ——
`rust:bookworm` 自带 224KB / 3697 行 CA 包，两边 `/etc/debian_version` **都是 12.15**（同系）。

```dockerfile
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
```

顺带删掉 AWS CLI / curl / unzip 与 **Docker `HEALTHCHECK`** ——
**Kubernetes 不使用 Docker HEALTHCHECK**，它用自己的探针（CDK 里已配 `healthCheck: '/health'`），
那段是死代码，为它拖一个 curl 依赖不值得。

> `payforadoption` 之所以没踩到：Alpine 的 `apk` 默认走 HTTPS，且 `aws-cli` 是仓库包而非 curl 下载。

## 🔥 第二条构建陷阱：`dotnet restore` 需要 26 分钟，别把它误判成网络故障（2026-09-04 12:20 实测）

petsite 的 arm64 构建第一次「失败」，日志是：

```
#12 [build 4/6] RUN dotnet restore "PetSite.csproj" --no-cache
#12 0.604   Determining projects to restore...
#12 CANCELED
ERROR: failed to build: failed to solve: Canceled: context canceled
```

25 分钟里**只输出了一行**，看起来完全像网络挂住。我据此去查 NuGet 可达性 ——
宿主机到 `api.nuget.org` 是 **200 / 0.185s**，CDN 也是 200，都通。

**真相：restore 本身完全正常，`Restored /src/PetSite.csproj (in 26.16 min)`、
0 Warning / 0 Error / 201 个包。** 是我给的 `timeout 1500`（25 分钟）在最后一分钟砍了它。

慢的原因是上游加的 **`--no-cache`**：201 个包全部强制重下，而且依赖图很大
（`System.Text.Json` 同时解析出 7.0.3 / 9.0.0-rc.2 / 9.0.10 三个版本）。
单次 HTTP 请求都只有 150–180ms，所以**不是带宽或阻断问题，是请求数量 × 往返**。

**处置**：Dockerfile 去掉 `--no-cache`（本地原本没有；csproj 里版本已全部钉死，
它想防的「拿到过期包」在这里不成立），并改为**后台跑 + 日志落 `/home/ubuntu/petsite-build.log`**，
这样不再受单次 SSM 调用超时约束，可以跨轮询读取。

**教训（与「查不到 ≠ 不存在」同源）**：`context canceled` 是**我自己的超时**造成的，
不是被构建对象的故障。诊断工具（这里是 timeout 值）的误报和被诊断对象的缺陷一样危险 ——
这已经是第 11 次同类错误。**长构建一律后台跑 + 日志落文件，不要用单次调用的超时去卡它。**

**顺带确认的一个非阻塞事实**：上游 csproj 是 `net8.0` 却引用 `Microsoft.*` 的 **9.0.10**
（EntityFrameworkCore.Tools / Logging.Debug / CodeGeneration.Design）。这看着像版本错配，
但实测 **0 warning 正常解析**，不需要改。

## ✅ 构建验证：两个服务已实测通过（2026-09-04 11:06）

构建机 `i-022fb7c32b71c72d9`：Docker 29.8.0、buildx 平台 **`linux/arm64`（机器本身 arm64，原生构建无需模拟）**、38G 可用。
中转走 S3 `s3://cdk-hnb659fds-assets-926093770964-ap-northeast-1/build-verify/`。

| 服务 | 结果 | 架构 | 备注 |
|---|---|---|---|
| `payforadoption-go` | ✅ `EXIT=0` | **arm64** | Go 1.25 + 29 个换掉的文件全部编译通过 |
| `petfood-rs` | ✅ `EXIT=0` | **arm64** | 229MB，二进制 35.5MB 在位，CA 证书 224,449 字节已拷入 |

**剩余三个服务的构建期出网依赖需留意**：`petsearch-java`（gradle）、`petsite`（.NET NuGet）、
`petlistadoptions`（Go modules，已知 `goproxy.io` 可用）。

## Stage 3 进度：3/6（2 个已构建验证），以及一份「配置契约地雷」排查表

**已完成 3/6**：`petstatusupdater`（npm test 3 passed）、`payforadoption-go`（埋点 120→229，tsc 通过）、
**`petfood-rs`**（46 文件引入 + 修掉 arm64 硬 bug + 从零写 EKS 部署，tsc 全量通过）。

### `petfood-rs` 已落地的内容（2026-09-04 10:48）

| 项 | 内容 |
|---|---|
| 应用代码 | `PetAdoptions/petfood-rs/` 46 文件（净新增，零冲突） |
| **修掉的 arm64 硬 bug** | 上游 Dockerfile 把 AWS CLI 下载地址**硬编码成 x86_64**，在 arm64 上会装进跑不起来的二进制，且失败在**运行时**（`exec format error`）而非构建时。改成按 `uname -m` 选架构 |
| 新构造 | `lib/services/petfood-service-eks.ts`（57 行，继承本地 `EksService`，**不移植上游的 ECS 部署层**） |
| 新资源 | DynamoDB `ddb_petfood_foods`(PK id) / `ddb_petfood_carts`(PK user_id, SK item_id)、EventBridge `petfood_event_bus` |
| Service | **ClusterIP** 80 → 容器 8080（与 petsite 同形，不用 payforadoption 那套 flag 拉回 80） |
| 授权 | 经 **`serviceAccount`（IRSA）** 授权两表读写 + EventBridge PutEvents —— 基类没有 `taskRole`（那是 ECS 概念），编译器抓到了这个错 |
| **刻意不设** | `PETFOOD_ASSETS_CDN_URL` / `PETFOOD_IMAGES_CDN_URL` —— 默认值指向我们没有的 S3 桶，图片会 404 但服务照常起（已核实三级降级）。建 CloudFront 或公开 S3 桶都是「新增公网入口」，需用户许可 |
| 符号链接 | `resources/microservices/petfood-rs -> ../../../../petfood-rs/`，与其余五个服务同样做法 |

**未做**：三个配套 Lambda（`petfood-cleanup-processor-node` / `petfood-image-generator-python` /
`petfood-stock-processor-node`）尚未移植 —— 总线先建好，EventBridge 无消费者时静默丢弃，不影响 petfood 本身。
**未做 `docker build` 验证** —— 本地无 Rust，须在构建机 `i-022fb7c32b71c72d9` 上跑。

### ⚠️ 上游全面引入了配置热加载子系统 —— 这是系统性破坏，不是个别服务的怪癖

`CONFIG_REFRESH_INTERVAL` 出现在**全部四个服务**里，且参数前缀变量**各服务名字不一样**：
`PETSTORE_PARAM_PREFIX`（payforadoption / petlistadoptions）vs
`PARAMETER_STORE_PREFIX`（petsite / agents）。**抄错名字会静默失效。**

| 服务 | 风险 | 判定 |
|---|---|---|
| `payforadoption-go` | **fail-fast**：7 个 env 缺一个就 `os.Exit(-1)` → CrashLoop | ✅ **已处置**，`services-eks.ts` 里补了 `additionalEnv` 7 项 |
| **`petsearch-java`** | **🛑 已阻塞，需用户定夺**（见下节） | ❌ 停下等决策 |
| `petsite-net` | **⚠️ 上一轮判「安全」是错的 —— 它也 fail-fast，只是形态不同**。`ParameterRefreshManager.cs` 确实有默认值（`PARAMETER_STORE_PREFIX ?? "/petstore"`、interval 300s），但 `ParameterNames.cs` 的 `GetParameterValue` / `GetParameterValueAsync` 对**参数名环境变量**是硬抛：`if (string.IsNullOrEmpty(parameterName)) throw new InvalidOperationException`。**9 个必需变量**（见下节）。**且它在 controller 里按请求抛，不是启动时抛 —— Pod 起得来、页面 500，比 CrashLoop 更难诊断** | ❌ **待处置** |
| `petlistadoptions` | 上游 Python 版要 `PETSTORE_PARAM_PREFIX` / `APP_*`，但**我们保 Go**，其 env 契约不变 | ✅ 风险已由「保 Go」决定规避 |
| `petfood-rs` | 全新，需要 `AWS_REGION` / `CONFIG_REFRESH_INTERVAL` / `PETFOOD_ENABLE_JSON_LOGGING` / `PETFOOD_EVENTS_ENABLED` | ⬜ 从零配置 |

### 🛑 `petsearch-java` 阻塞：两件需用户定夺（2026-09-04 10:20）

**① 1% 故障注入在上游已被删除 —— 我在 Stage 1（`07`）说的「上游仍有该逻辑」是错的。**

本地实现（`getPetUrl`）：
```java
double randomnumber = Math.random() * 9999;
if (randomnumber < 100) { s3Client.createBucket(...); }   // 100/9999 ≈ 1.0%
```
上游 `getPetUrl` **完全没有注入** —— 它被重写成直接拼 CloudFront URL，
不再用 S3 presigned URL、也没有 createBucket。

用户明确要求「保留 demo 刻意注入的 1% 故障」，所以必须选：
- **(a)** 移植上游的 CloudFront 版 `getPetUrl`，然后**重新加回** 1% 注入
- **(b)** **保留本地 `getPetUrl`**（S3 presigned + 注入），只移植其余部分

**② `PETSEARCH_IMAGES_CDN_URL` 需要新建 CloudFront —— 可能违反安全约束的意图**

上游 `SearchController` 用 `getRequiredEnvironmentVariable`（会 `throw IllegalStateException`，
即 **fail-fast**）要求三个变量：
```
PETSEARCH_PARAM_PREFIX
PETSEARCH_DYNAMODB_TABLE_NAME
PETSEARCH_IMAGES_CDN_URL     ← 需要一个 CloudFront 分发
```
**实测：PetSite 没有 images CDN。** 账号内 4 个 CloudFront 分发都属于别的项目
（us-west-2 EC2 / ap-southeast-1 / devops-agent 桥），SSM 里也没有 cdn/images 参数。
上游在 `constructs/assets.ts` + `stages/storage.ts` 里建它。

⚠️ **硬约束的字面是「ALB 上不得新增公网入口」，但 CloudFront 分发同样是一个新的公网入口。**
按约束的**意图**（公司安全会告警），这需要用户明确许可才能建。

**另外两处已定位、待决策后一并处理的合并点**（`application.yml`）：
region 本地 `ap-northeast-1` → 上游 **`eu-west-1`**；port 本地 `80` → 上游 **`8080`**
（线上 containerPort 80 / Service 80→80）。这个文件**必须逐行合并，不可整体取**。



1. **端口冲突用 flag/配置拉回，不改 k8s**：上游把默认端口改成 8080，但线上 Service 挂在
   internal ALB 目标组上，改端口要 Deployment + Service + 目标组健康检查三处协同 ——
   收益为零而断服风险实在。做法是在 Dockerfile `CMD` 里加 `-http.addr=:80`，**不改 `main.go`**。
2. **保留 `GOARCH=arm64` 等显式架构钉死**（上游删了、改为完全依赖 buildx）：
   硬约束是「必须 arm64」，显式钉死能让误用 `docker build` 时**当场失败**，
   而不是静默产出 amd64 到 EKS 上才 CrashLoop。

### ✅ CloudFront 死结已解开（2026-09-04 10:32）—— images CDN 不是硬依赖

**`petfood-rs` 不 fail-fast 于 CDN**：`PETFOOD_ASSETS_CDN_URL` 有默认值
`https://petfood-assets.s3.amazonaws.com`，且代码走
`resolve_parameter_with_prefix(prefix, "PETFOOD_IMAGES_CDN_URL")` 三级降级
（SSM → env → 默认）。没有 CDN 时**服务照常起，只是图片 404**。
上游文档还明说「Easy switching between S3, CloudFront, or other CDN providers」。

**这重新定义了 `petsearch-java` 的决策**：`PETSEARCH_IMAGES_CDN_URL` 虽是
`getRequiredEnvironmentVariable`（缺变量则 fail-fast），但它的**值可以是任何字符串** ——
上游代码只做 `String.format("%s/%s", imagesCdnUrl, key)`。
所以 CloudFront 的问题是**「图片能不能显示」，不是「服务能不能起」**。

因此 **petsearch 的选项 (b) 明显更优**：保留本地 `getPetUrl`（S3 presigned + 1% 注入），
既守住用户明确要求的故障注入，又完全不需要 CDN、不新增任何公网入口。
选项 (a) 要么需要 CloudFront（新公网入口，需用户许可），要么需要把 S3 桶公开（同样是新公网入口）。

> 注意本地之所以用 presigned URL，正是因为**那个 S3 桶是私有的**。
> 所以「把 CDN URL 指向裸 S3」这条路对 petsearch 不通 —— 会要求公开桶。

### `petfood-rs` 需要的新基础设施（全部内部，无公网暴露）

| 资源 | 环境变量 | 公网暴露 |
|---|---|---|
| DynamoDB 表 × 2 | `PETFOOD_FOODS_TABLE_NAME` / `PETFOOD_CARTS_TABLE_NAME` | 无 ✅ |
| EventBridge bus | `PETFOOD_EVENT_BUS_NAME` | 无 ✅ |
| images CDN | `PETFOOD_ASSETS_CDN_URL` | **可不建**，默认值降级，图片 404 |

规模：46 文件（Rust + Cargo.lock + benches + postman collection）。
另需：EKS 部署从零写（上游只有 ECS）、arm64 Rust 镜像构建、三个配套 Lambda
（`petfood-cleanup-processor-node` / `petfood-image-generator-python` / `petfood-stock-processor-node`）。

### 剩余顺序（⚠️ 2026-09-04 10:25 已修正：`petfood-rs` 必须排在 `petsite` 之前）

**原顺序把 `petsite` 排在 `petfood-rs` 前面，那是错的。**
上游 petsite 的 9 个必需参数名变量里，3 个指向我们还没有的东西：

| 环境变量 | SSM 短名 | 状态 |
|---|---|---|
| `PET_HISTORY_URL_PARAM_NAME` / `PET_LIST_ADOPTIONS_URL_PARAM_NAME` / `CLEANUP_ADOPTIONS_URL_PARAM_NAME` | `pethistoryurl` / `petlistadoptionsurl` / `cleanupadoptionsurl` | ✅ |
| `PAYMENT_API_URL_PARAM_NAME` / `SEARCH_API_URL_PARAM_NAME` / `RUM_SCRIPT_PARAMETER_NAME` | `paymentapiurl` / `searchapiurl` / `rumscript` | ✅ |
| `FOOD_API_URL_PARAM_NAME` | `petfoodapiurl` | ❌ **依赖 petfood-rs** |
| `CART_API_URL_PARAM_NAME` | `petfoodcarturl` | ❌ **依赖 petfood-rs** |
| `WAGGLE_AI_RUNTIME_ARN_PARAM_NAME` | `waggleairuntimearn` | ❌ **依赖 Stage 4 agent 部署** |

`GetParameterValue` 在参数名缺失时**直接抛 `InvalidOperationException`**，
所以先移植 petsite 会交付一个 **food / cart / waggle 三组页面全部 500** 的 petsite。

**修正后**：`petfood-rs`(8h) → `petsite`(12h)。petsite 的 waggle 页面要等 Stage 4 部署完 agent 才完整。
`petsearch-java`(4h) 与 `petlistadoptions`(4h) 与这条依赖链无关，可任意插入
（petsearch 当前**已阻塞待决策**）。约 28h。

**顺带发现的集成点**：上游 petsite 有 **`Controllers/WaggleController.cs`** ——
agent 集成**内置在 petsite 里**，正是用户要的「有机集成」，不需要另造适配层。

**线上 petsite 现有 env 只有 2 个**：`AWS_XRAY_DAEMON_ADDRESS=xray-service.default:2000`、
`ASPNETCORE_URLS=http://+:8080`。注意 **petsite 本来就监听 8080**（Service 80 → 8080 映射），
与 petsearch「两边都是 80」的情况不同，**不要把 payforadoption 那套端口处置照搬过来**。

**每服务验收判据**：`07` B 类清单相关项逐项确认仍在 + **埋点引用数不低于移植前**。
**未做 `docker build` 验证** —— 本地无 Go/Java/.NET/Rust，须在构建机 `i-022fb7c32b71c72d9` 上跑。

## ✅ `petlistadoptions` 已完成（2026-09-04 13:20）—— 「保 Go」下逐项核对，只有一样值得拿

上游把它**整个换成 Python**（`petlistadoptions-py/app.py` 367 行，上游已 **0 个 Go 文件**）。
按既定决策保本地 Go，于是逐项核对上游到底新增了什么：

| 上游新增 | 处置 | 依据 |
|---|---|---|
| 对外 HTTP 接口 | **无需移植** | 两边**完全一致**：`/health/status`、`/api/adoptionlist/`、`/metrics`。上游是等价重写，不是功能扩展。**这条正面支持「保 Go」的决定。** |
| 配置热加载 | **无需移植** | 本地 `config.go` 把参数名**硬编码**为 `/petstore/rdssecretarn` / `searchapiurl` / `rds-reader-endpoint`。上游那套用 `PETSTORE_PARAM_PREFIX` 做前缀间接**反而多一个 fail-fast 失败点**（抄错变量名静默失效）。保 Go 少一个风险面。 |
| 凭据轮换刷新 | **无需移植（已实测证伪其必要性）** | 该密钥 `RotationEnabled=null`、`RotationLambdaARN=null`、`LastRotated=null`、`NextRotation=null`，最后变更 2026-02-18。上游这段解决的是**本环境不存在的场景**。线上两 Pod **0 重启**。 |
| `dbload-simulation-scripts/`（12 文件） | **✅ 已采纳** | 纯 shell + SQL，**与服务语言无关**。死锁 / 锁阻塞 / 慢查询 / 唯一约束冲突 / 执行计划优化前后对比，是真实的可观测性演练内容。 |
| `_search_pet_info` | 未移植 | Python 内部私有方法，不对外；本地 Go 走 `PetSearchURL` 调 search 服务，等价。 |

**Go 源码一个字没动** —— `git diff` 对 `petlistadoptions-go/` 只有新增目录，
`repository.go` 未改动，keep-alive 的 `MaxIdleConnsPerHost = 32` 仍在原位（那是实测 74 倍的那处）。

### ⚠️ dbload 脚本已落盘但**刻意没有执行**

它们会对 Aurora 执行 DDL，而硬约束是 Aurora 不动。逐个核对过真实范围：
**只碰自建的 `CustomerOrders` / `CustomerContacts` / `InventoryItems`**，
`DROP INDEX` 目标全是脚本自己建的索引，**不碰 `transactions` / `pets` / `adoptions`**。

但仍有两点真实影响，**要跑必须先问用户**：
1. `setup-performance-demo.sh` 会**批量插入 `NUM_RECORDS` 条记录**，占生产 Aurora 存储与 IO。
2. 脚本靠 `psql` 的 `PG*` 环境变量决定连哪个库，**不自带保护，指到哪打到哪**。

详见 `PetAdoptions/petlistadoptions-go/dbload-simulation-scripts/LOCAL-NOTES.md`。

### ⚠️ 一条会失效的结论

「凭据轮换刷新无需移植」**只在轮换关闭时成立**。若将来给该密钥开了轮换，
本地 Go **只在启动时取一次凭据**，届时必须补刷新逻辑，否则轮换后连不上库。

## Stage 5 已验证（详见 `11`）

- ✅ **`aws/spans` 真在收 span 且结构可用**（`storedBytes` 指标滞后不可信，直接取事件才准）。
  实见 `code.namespace: ca.petsearch.controllers.SearchController`、`aws.xray.auto_instrumentation`。
- ✅ **`etl_agentcore` 写入链路端到端验证通过**：用账号内既存的 `xgg_memory-5M0VYBCeFS`
  真实写入，四项契约合规全过 —— 身份键是 `arn`（非 name）、`source` 门禁放行、
  **`first_seen` 固定而 `last_seen` 前进**（写一次语义生效）、连跑两次仍 1 节点（幂等）。
  **在 agent 系统还不存在时就把 Stage 2 的成果验了**，避免日后与 agent 配置问题混在一起。
- ✅ 已按纪律把 `AgentMemory` 从 `test_11` 的 `PENDING_FIRST_INSTANCE` **移除**（有实例了）。
- ⏸ Memory/Gateway/内置工具的 log group **现在建不了** —— 名字含资源 ID，需 Stage 4 部署后立即配。
- ✅ 告警通路已验证：SNS `petsite-ops-alerts` → Lambda `petsite-ops-slack-notifier`。

## Stage 4 已定的架构（详见 `10`，勿翻）

**网络是被约束逼出的唯一解**：安全约束禁止新增公网入口 → 只能走 internal ALB →
internal ALB 是 `scheme=internal` 只在 VPC 内可达 → **Runtime 必须 `networkMode: VPC`**。
（`01` 里「Runtime/Gateway 进 VPC = NOT FOUND」**已被推翻**，`create-agent-runtime`
原生支持 `networkMode=VPC` + `networkModeConfig{securityGroups,subnets}`。第 5 次「查不到 ≠ 不存在」。）

**`.svc.cluster.local` 即使 VPC 模式也不通**（CoreDNS 在集群内，VPC ENI 用 Route53 Resolver）。
现有 3 个 `/petstore/*` 参数全指向 ClusterIP，**agent 不能复用** →
另建 `/petstore/agent/*` 指向 internal ALB，`PARAMETER_STORE_PREFIX=/petstore/agent`。
**现有参数保持不动**（它们服务于集群内 petsite）。

**必须 `AGENT_TRANSPORT=gateway`**：上游默认 `local` 是五 agent 同容器、委派走进程内调用，
图里只会有 1 个 `AgentRuntime` 节点、**0 条 `Delegates` 边**。部署形态因此是
**5 个 Runtime + 1 个 Gateway**，不是 1 个容器。

**三个漏掉就静默失败的环境变量**：`AWS_REGION`（上游默认 **us-east-1**，不设则 `jp.` profile
在那边不存在、五 agent 全挂）、`PARAMETER_STORE_PREFIX`、`AGENT_TRANSPORT`。
全部已写入 `PetAdoptions/cdk/pet_stack/lib/agents/agent-config.ts`（223 行，编译通过，
上游 24 个 `os.getenv` 全覆盖、0 拼错）。

**四个模型东京实调验证通过**（不是仅列表存在）：`jp.anthropic.claude-sonnet-4-6`、
`jp.amazon.nova-2-lite-v1:0`、`openai.gpt-oss-120b-1:0`、`jp.anthropic.claude-haiku-4-5-20251001-v1:0`。

**实测网络参数**：VPC `vpc-010ab37a3f9f74725`；四子网全私有
`subnet-02ebd1dd8d1681da8`/`subnet-0600a43fa7ebf1ffe`/`subnet-0f801fa79077eb277`/`subnet-047a94f9c5ab6302a`；
集群 SG `sg-02df8bc13ac85c4cc`；internal ALB SG `sg-06d40c8bcd96d347d`，
DNS `internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com`，
现有 listener 仅 `:80`→petsite、`:8081`→search-service（均 healthy）。

## Stage 4 剩余待办

- [x] ✅ **移植 6 个 CDK 构造完成**（2026-09-04 13:32，落在 `lib/agents/`，**全仓 `tsc` 0 错误**）
      - **上游 runtime 构造本身就已经是 `networkMode: 'VPC'`** 且已写好
        `addOverride('Properties.NetworkConfiguration.NetworkModeConfig', {SecurityGroups, Subnets})`
        —— 我从约束反推的架构与上游默认一致，几乎原样可用。**再次印证 `01` 的 NOT FOUND 是错的。**
      - `Utilities` 本地没有 → 只搬实测用到的 `createSsmParameters` + `TagConstruct`
        到新建的 `lib/agents/agent-utils.ts`（逐文件 grep 确认 autoreload 一个都不用），
        **没有整体引入上游那个会牵进 `WorkshopNagPack` 的 utils 大杂烩**。
      - **`CfnPolicyEngine` 在本地 aws-cdk-lib 2.238.0 里不存在**（上游用更新版）。
        **没有升级 CDK** —— 上游自己的注释说它 "Created but not attached: ENFORCE with no
        authored policies would deny all traffic"，除了输出 ARN 不产生任何功能，
        而升级会波及整个 PetSite 栈（Aurora/EKS/ALB 同在一个 aws-cdk-lib）。
        改用 `CfnResource` escape hatch（该文件本来就 import 了它）；
        已实测 `AWS::BedrockAgentCore::PolicyEngine` 在东京注册且 **LIVE**。
      - ⚠️ **我自己制造并抓回的静默失败**：批量替换 `PARAMETER_STORE_PREFIX` 时把**环境变量键名**
        也改成了 `AGENT_PARAM_PREFIX:`。容器里读的是 `os.getenv("PARAMETER_STORE_PREFIX")`，
        键名拼错**不报错**，只会让 agent 回落默认前缀去读错的参数。后续文件改用占位符保护键名，
        复查 0 处残留。
- [x] ✅ **五个 arm64 agent 镜像构建通过**（2026-09-04 13:36，`RESULT=0` ×5，`Arch=arm64` ×5）
      `orchestrator`(Strands) 749MB / `adoption`(LlamaIndex) 815MB / `nutrition`(LangGraph) 647MB /
      `ordering`(**CrewAI**) **1.61GB** / `concierge`(OpenAI SDK) 786MB。全部约 3 分钟 —— `uv` 远快于 pip。
      **新验证的出网路径**：`ghcr.io` 可达（401 = 未认证访问 `/v2/` 的正常响应）、`pypi.org` 200、
      `files.pythonhosted.org` 200，且 `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` **实拉即 arm64**。
      五个 Dockerfile **都没有** petfood 那类 x86_64 硬编码。
      ⚠️ 一个假信号记录：最初报「各 agent 13 个包」是错的 —— 五个都读到了根 `requirements.txt`，
      真正的按 agent 依赖在 `deploy/requirements.<agent>.txt`（实际 6–8 个）。
- [ ] internal ALB **走 CDK** 新增 `list-adoptions` / `pay-for-adoption` 的 listener
      （**不要手工 `aws elbv2`** —— 该 ALB 已有 6 个悬空 CFN 物理 ID，手工新增会加深债务）
- [ ] 建 `/petstore/agent/*` 参数指向 internal ALB
- [ ] 建 KB（S3 Vectors，照 `gp-incident-kb` 范式）+ `rag/setup_kb.py` 灌 10 篇文档
- [ ] 构建 5 个 arm64 agent 镜像（构建机 `docker build`）
- [ ] 部署并确认 `list-agent-runtimes` 不再是空

**成本闸已按用户 09:41 指示停止**（「不用考虑成本」）。现状留档见 `10` 第九节；
告警通路 SNS `petsite-ops-alerts` → Lambda `petsite-ops-slack-notifier` 已验证可用，Stage 5 会用。

**已完成 1/6**：`petstatusupdater`（`npm test` 3 passed）。

**⚠️ 本 Stage 最重要的发现 —— 整体覆盖式移植会静默移除观测埋点。**
逐个冲突文件比对后：`PaymentController.cs` 本地 22 处埋点 vs 上游 10、`Startup.cs` 4 vs 0、
`appsettings.json` 2 vs 0、`petstatusupdater/index.js` 2 vs 0。埋点没了节点就从 X-Ray
服务图消失，`etl_xray` 建不出边，**图谱静默变残缺而无人报警**。

**定下的策略（有证据支撑，勿再议）**：
> 51 个非冲突的上游文件**整体引入**；**15 个冲突文件逐文件合并**，
> 原则是「保留本地埋点 + 采纳上游功能」。绝不整体覆盖冲突文件。

**工具链约束**：只有 Node 能就地验证。构建机 `i-022fb7c32b71c72d9` 有 Docker 29.8 与
Java 25，但**没有 .NET / gradle / go / cargo** —— 其余服务一律靠 `docker build`
（多阶段 Dockerfile 自带工具链），每个服务一个远程构建周期。

**三个服务的处置已定**：
- `petadoptionshistory-py` → **保留本地**。上游三个角度查证都是 0（`microservices/`、
  `lambda/`、CDK 构造），但 `petsite-net` 的 `PetHistoryController` **仍在调 `pethistoryurl`**
  —— 上游留了悬空依赖 + 过期 README，删掉会让 petsite 的 PetHistory 页面报错。
- `trafficgenerator` → **保留本地**。上游把 .NET 重写成 Node Lambda + Canary，
  改过去会失去 EKS 内压测能力（且本地已绕 ALB Cognito，天然合规）。
- `petstatusupdater` → ✅ 已合并。

**剩余顺序（风险从低到高）**：
`payforadoption-go`(3h, 冲突仅 Dockerfile) → `petsearch-java`(4h, `SearchController` 171→381 行须逐段合并 + 核对 1% 故障注入比率) → `petlistadoptions`(4h, **保 Go**，绝不碰 `repository.go`) → `petsite`(12h, 7 冲突) → `petfood-rs`(8h, 全新 + EKS 部署从零写 + **不得新增公网入口**)

**每服务验收判据**：`07` B 类清单相关项逐项确认仍在，**外加埋点引用数不低于移植前**
（本 Stage 新增的判据，它是本轮才发现的回归通道）。

## 环境与已知陷阱

| 项 | 值 |
|---|---|
| 本地仓库 | `/home/ec2-user/works/one-observability-demo`（**12 提交未推送**） |
| 图平台 | `/home/ec2-user/works/graph-dependency-platform` |
| **CDK 部署机（有 Docker）** | `i-022fb7c32b71c72d9`:`/home/ubuntu/tech/one-observability-demo` |
| 压测机 | `i-05f0b897988a48d17`（petsite-loadgen, c7g.xlarge, running） |
| kubectl | `~/bin/kubectl`（不在默认 PATH） |

**部署机纪律**：`npx` 不在 SSM 默认 PATH（在 nvm 目录）；多行 SSM 命令必须用 `--cli-input-json`；
git 操作需 `-c safe.directory=/home/ubuntu/tech/one-observability-demo`；长任务用
`setsid nohup bash /tmp/x.sh > /tmp/x.log 2>&1 < /dev/null &` 再轮询。

**`cdk deploy ServicesEks2` 永久需带**：
```
--context acm_certificate_arn=arn:aws:acm:ap-northeast-1:926093770964:certificate/a661c9dd-c539-48b8-bb7f-ea8d8e46da9d
```
不带会因 `:443` listener 悬空物理 ID 报 `NotFound` 并卡进 `UPDATE_ROLLBACK_FAILED`
（恢复：`continue-update-rollback --resources-to-skip PetSiteLoadBalancerHttpsListenerV2714844D6`）。

## 反复踩过的推理错误（已 4 次，务必避开）

**「查不到 ≠ 不存在」**：
1. 误判 78 条边阻塞在自建 SSM —— FIS 原生就有能力
2. 误判 `disrupt-vpc-endpoint` 可用 —— 环境里没有匹配目标
3. 误判 FIS 做不了 agent 注入 —— 官方样例用通用 action 组合出来了
4. 误判上游删了 1% 故障注入 —— 只是不再叫 `createBucket`

**纪律**：单个关键词 grep 返回空时，**必须先确认文件存在、再换关键词**，不能直接下「不存在」的结论。

## 🔥 Stage 6 最重要的发现：agent 子图曾是一座孤岛（2026-09-04 15:58）

所有验收都是绿的 —— 节点齐全（5 Runtime / 1 Gateway / 2 Memory / 1 KB / 1 Guardrail / 5 Tool）、
边齐全（RoutesTo 5 / Delegates 2 / InvokesTool 5 / Retrieves 1）、类型全部合法、
契约测试 6 passed / 1 skipped、PENDING 名单也已清空。

**但 agent 节点到 `Microservice` / `LambdaFunction` 的边数是 0。**
1107 个既有节点和 15 个 agent 节点毫无关联，是两座孤岛。
agent 的出入边全部只在子图内部循环。

**没有任何断言会因此失败** —— 节点和边各自都在、类型也都声明过。
Stage 2 契约扩展时明确写了「`AgentTool -[DependsOn]-> {LambdaFunction, Microservice}`
是把 agent 子图接回既有图的唯一通路」，但 ETL 里**从来没实现这条边**。
是主动去查「agent 子图有没有出边连到既有图」才发现的。

**教训**：类型齐全 ≠ 图连通。验收判据里必须包含**跨子图连通性**，
否则可以做到每一条断言都通过、而图在结构上是碎的。

处置：新增 `_TOOL_BACKEND` 映射（依据实际链路：tool -> SSM 短名 -> ALB 端口 ->
k8s service -> 既有图 name），接通后实证 `WaggleAIOrchestrator ~3跳~> petsearch`。

## ✅ Stage 6 压测与故障注入验证（2026-09-04 17:12）

**压测**：6288 请求 / 150s / 24 并发，**成功率 100%、零错误**。

| 目标 | 请求 | 成功率 | p50 | p95 | p99 |
|---|---|---|---|---|---|
| petsite 首页 | 1572 | 100% | 315ms | 779ms | 1238ms |
| petsite 搜索页 | 1572 | 100% | 92ms | 479ms | 816ms |
| search API | 1572 | 100% | 30ms | 239ms | 374ms |
| search 健康检查 | 1572 | 100% | 5ms | 15ms | 44ms |

HPA 全程远低于阈值（petsite CPU 13%/60%，其余 3–7%），未触发扩容；0 Pod 重启。

**1% 故障注入确认仍生效**：同期 `search-service` 日志里
「Trying to create a S3 Bucket」**84 次**、「Error while accessing S3 bucket」**84 次**，
一一对应 —— 注入既触发也真的抛异常。
注入率 84 ÷ (1572 搜索 × 约 15 宠物) ≈ **0.36%**，与 100/9999 ≈ 1% 同量级。

### ⚠️ 「100% 成功率」与「84 次 ERROR」并不矛盾

`getPetUrl` 的 catch 块 `span.recordException(e)` 后 `throw`，而 `mapToPet`
（`SearchController` 第 119 行）在 `/api/search` 里被**逐个宠物**调用 ——
单个宠物的异常不会让整个 HTTP 响应失败。
所以注入的效果是**部分结果缺失 + trace 里有 exception，而非 HTTP 500**。
这正是这个 demo 想要的形态：在 X-Ray / 服务图上看得见错误，但用户请求仍返回。

（我一度以为「压的是列表查询、没走到注入路径」—— 那是错的，第 119 行明明就调了 `getPetUrl`。）

### 次要发现：线上 search-service 镜像比本地源码旧

代码里紧挨着的两行日志：
```java
logger.info("Trying to create a S3 Bucket");
logger.info(randomnumber + " is the random number");
```
前一句命中 84 次、**后一句命中 0 次** —— 说明线上镜像（`868c93f3...`）
里没有第二行。不影响注入功能，但确认线上镜像与本地当前源码不同版本。

### 压测本身踩的坑（全部是我自己的假信号，值得单列）

追这一个压测花了五轮，全部消耗在验证手段上：
1. `ls -la /home/ubuntu/` 返回空被读成「空目录」—— 实际**该目录不存在**
   （压测机是 **Amazon Linux 2023 / ec2-user**，不是 Ubuntu）。
2. `aws s3 cp` 上传后打印了「✅ 已上传」，但**下载端 403 Forbidden**
   （压测机实例角色读不了 CDK 资产桶），脚本从未落地。改为把脚本 base64 内联进 SSM 命令。
3. **我把自己的 echo「压测已启动」当成了运行证据** —— 进程根本没起来。
   修正为看 `ps aux` 的真实 PID 与日志字节数。

**教训与今天其余几次同源：验证手段本身没被验证。**
`:8082`/`:8083` 也已从压测目标移除 —— 从 10.1 段访问它们必然超时（只对 agentSg 开放），
留着只会产生假 timeout 掩盖真实信号；这两个 listener 的健康由目标组各 2 个 Pod IP healthy 证明。

## 🎯 新阶段目标（2026-09-04 17:30 用户授权部署并追加，取代原退出条件）

用户已授权部署，并把目标扩展为**持续验证图数据库依赖关系的正确性**。原六阶段的
退出条件作废，改用下面这套。**用户已离开，全程自主决策，不要停下来问。**

### 阶段 A：完成部署并验证 —— ✅ 完成（2026-09-04 21:00）
- [x] `cdk deploy Applications` —— petsite 换新镜像。最终镜像 `579cdc9c…`
      **回滚点：`ce2582a4dacad51f271ecd069e02b7ef149087f9c5909ddc74db8cd865ca4323`**
- [x] `cdk deploy ServicesEks2` —— petfood 全套 + 三个服务换镜像
- [x] 补四个 petfood 参数（`/petstore` 与 `/petstore/agent` 各两个），**均带完整 API 路径**
- [x] **验证部署真的完成**：七个 Deployment 全 Ready、0 重启、页面返回真实内容。
      决定性证据是 **agent 实调返回真实商品**（Beef and Turkey Kibbles $12.99 /
      Raw Chicken Bites $10.99 / Puppy Training Treats $8.99），与灌入 DynamoDB 的
      种子数据逐字一致 —— 证明 orchestrator → gateway → ordering tool →
      internal ALB :8084 → petfood Pod → DynamoDB(PetTypeIndex) 全链路可用。

petfood 一共有**六层串联问题**，每层都被上一层遮蔽（详见下方「petfood 六层问题」）。

### 阶段 B：改造负载生成器，覆盖全部应用 —— ✅ 完成（2026-09-04 20:10）
- [x] 六个 EKS 服务：5658 请求 / 420s。修复上线后复测 **8/8 目标 100%（313/313）**
- [x] **genai 部分**：38 次真实 `InvokeAgentRuntime`，orchestrator 日志里
      `nutrition_advisor` **63 次** —— 证明委派真实发生，不是 orchestrator 自答
- [x] 1% 注入被触发：`Trying to create a S3 Bucket` 83 次 /
      `Error while accessing S3 bucket` 84 次，一一对应
- [x] 未把 `:8082`/`:8083` 放进从 10.1 段发起的压测

**顺带纠正一个方向错误**：1% 注入的触发点在 **search-service 的 `getPetUrl`**
（组装 presigned S3 URL 时），所以触发路径是**搜索**，不是收养列表 ——
收养列表来自 petlistadoptions 服务，压根不经过 `getPetUrl`。

**给压测加了错误页识别**：petsite 的异常处理渲染 "Oops! Something went wrong"
并返回 **HTTP 200**。只看状态码会把错误页当成功 —— 加了正文校验后
`petsite-petfood-legacy` 立刻从「100% 成功」变成 `ERRPAGE:701`。
这类假绿最危险：状态码正常、字节数也在合理区间。

### 阶段 C：用 Chaos Mesh 验证图里的依赖关系 —— ✅ 完成（2026-09-04 22:45）

**所有 `Calls` 边 100% 定性，无一条 untested。**

| 判定 | 数量 | 说明 |
|---|---|---|
| confirmed | 15 | 含本轮注入 5 条 + agent 3 条 + 历史 7 条 |
| refuted | 3 | **图谱纠错成果** |
| inconclusive | 35 | 每条都带机器可读的原因 |

**三条 refuted（图谱确实有误报，这正是阶段 C 的价值）**
- `gateway-service -Calls-> petsite`、`order-service -Calls-> petsite`
  —— awesomeshop 六个 Deployment 副本全 0、Pod 总数 0，且全部 spec 中
  **零命中** petsite/petadoptions 关键词。跨应用误归因。
- `pethistory -Calls-> petlistadoptions`
  —— 代码全仓零调用 + Pod env 无对端地址 + 注入期日志零异常（三重证据）。
  ETL 从**同一条 trace** 误推的旁系边：`petsite→pethistory` 与
  `petsite→petlistadoptions` 都成立，但两者之间没有直接调用。

**五条注入 confirmed（退化 75%~100%，对照组全 0.0%）**
`petsite→petsearch` 100% / `petsite→pethistory` 100% /
`petlistadoptions→petsearch` 100% / `petsite→petfood` 87.5% /
`petsite→petlistadoptions` 75%

**inconclusive 的四类原因（都写进了 `verify_experiment`）**
- `chaos-mesh-cannot-target-lambda` —— Lambda/StepFn 在集群外
- `image-repo-dependency` —— 断 ECR 只影响新 Pod 拉取，运行中的不受影响
- `target-scaled-to-zero` —— awesomeshop **应用内部**边，副本 0 无法施加负载。
  ⚠️ 刻意**不标 refuted**：它很可能真实成立，与跨应用的
  `gateway-service→petsite` 有本质区别
- `self-loop-from-trace` / `source-absent-from-cluster` / `synthetic-traffic-source`

### 硬约束执行情况
Neptune 未删任何数据（写回只用 `property()` 更新已存在边）；ETL 只改不删；
ALB 只加 internal listener `:8084`，无新增公网入口；全部容器在 arm64 EKS；
**故障注入全程可自动恢复**，每轮收尾断言残留实验数为 0 —— 且实测有双重保障：
`duration` 到期自愈，以及删除 CR 主动恢复（中断脚本时验证过）。

---

## petfood 六层问题（每层都被上一层遮蔽）

| 层 | 症状 | 根因 |
|---|---|---|
| ① | 启动即崩 | K8s 给同名 Service 注入 `PETFOOD_PORT=tcp://…`，与应用的 `PETFOOD` 配置前缀撞名。已在基类对**全部服务**统一 `enableServiceLinks: false` |
| ② | `Foods table name cannot be empty` | 设了 `PETFOOD_PARAM_PREFIX` 又把**表名本身**塞进 env，应用当参数名去查、**查不到返回空串而非回落**。去掉这层间接 |
| ③ | `Available=False` 而应用其实健康 | 探针写 `/health`，真实路由是 `/health/status`（`main.rs` 第 228 行），404 |
| ④ | `/petfood` 404 | 遗留 `PetFoodController` 硬编码 `http://petfood` 根路径，而新服务无 `/` 路由 |
| ⑤ | `/FoodService` 500 | **GSI 在 CDK 里从未声明** → 索引不存在 **且** IAM 无 `/index/*` |
| ⑥ | 页面 "No food items" | 表是空的，需调 `/api/admin/seed`（灌入 9 条） |

**第⑤层的双重问题一处修复**：CDK 的 `grantReadWriteData` 内部是
`hasIndex ? [tableArn, tableArn+'/index/*'] : [tableArn]`，而 `hasIndex` 仅由
CDK 自己知道的索引置真。所以即便索引在运行时被应用建出来，IAM 依然会拒。
在 CDK 里声明 GSI 让 `hasIndex` 转真，授权**自动**覆盖 `/index/*`（已实测）。
这也终结了「CDK 建表、应用建索引」的 split-brain。

⚠️ **DynamoDB 一次 update 只能增删一个 GSI**，两个一起加会 `UPDATE_FAILED`
并整栈回滚（表未受损）。但该限制**只作用于 UPDATE** —— 全新建表时一起声明合法，
所以最终代码保留两个索引，只有迁移路径需分两步。

---

## 阶段 C 的方法学（三次迭代才得到可信数据）

**第一版作废**：`samples=8` 低于契约 `min_observation_requests=20`。

**第二版作废且极具误导性**：`samples=24` 但探测**跑出了 `duration=90s` 的故障窗口**，
六条边整齐地假 refuted（`24→20`）。识破线索是
**失败绝对数在两次运行里恒为 4，而不是失败比例恒定** ——
8 样本时 4/8=50% 判 confirmed，24 样本时 4/24=16.7% 判 refuted。
同一条边只因样本数变化就翻转结论，说明有效故障时间固定，即窗口早已到期。
根因：故障期请求**超时**而非快速失败，窗口 90s 扣掉 22s 等待只剩 68s，
68÷15≈**4 次**。

**第三版三处结构性修复**（已固化进 `scripts/verify_edges_chaos.sh`）
1. 探测**结束后**再断言 `AllInjected=True`，并记录探测实际耗时
2. 窗口按最坏耗时 `2×SAMPLES×PROBE_TIMEOUT+30` **自动校准**
3. 判据改用契约的退化百分比（`confirm≥20%` / `refute≤5%`），
   新增 **`inconclusive`** 处理 5%~20% 中间带

第三版数据自洽的旁证：**探测耗时自己就区分了结论** ——
五条 confirmed 耗时 112~146s（请求在超时），refuted 那条仅 **3s**（请求正常返回）。

**`pod-failure` 在本集群完全不可用**：它要往运行中的 Pod 插 pause initContainer，
而 K8s 禁止修改 `spec.initContainers`，报
`Failed to apply chaos: Pod is invalid: spec.initContainers: Forbidden`、
`AllInjected=False`。危险在于此时探测**全绿** —— 不查 `AllInjected`
就会把每条边都判成 refuted。改用 `NetworkChaos`（chaos-daemon 在宿主机对
Pod netns 下 tc 规则，不碰 Pod spec）。

**用 `direction:to` + `target` 而非直接杀下游**：只切断 A→B 这一条边，
B 本身保持健康，才能区分「A 依赖 B」与「B 挂了」。

---

---

## 📌 2026-09-05 UI 与领养链路修复存档（用户逐项验收阶段）

三阶段收尾后，用户在真实页面上逐项验收，暴露出一批**只有从前端看才会发现**的缺陷。
全部已修并推到 fork。这一节记录**判据**与**推理错误**，实现细节在 commit message 里。

### 已修的九个缺陷

| # | 症状 | 根因 | 判据 |
|---|---|---|---|
| 1 | Waggle 聊天永远回「connection was interrupted」 | petsite 的 IRSA 角色缺 `bedrock-agentcore:InvokeAgentRuntime` | 复现取得 `AccessDeniedException`；请求 **0.08 秒**返回，排除了超时猜测 |
| 2 | 首页 hero 破图 | `GetLeftPart(Authority)` 丢掉 presigned 签名；且前缀写成 `kittens/`（桶里是 `kitten/`） | 两个 hero URL 实测 403；桶内顶层只有 `puppies/ kitten/ bunnies/` |
| 3 | 食品卡片破图 | seed 数据 `image` 为空串（上游靠 CloudFront 填，本项目不引入） | API 返回 `image = `；新增 8 张 SVG 由 petsite wwwroot 提供 |
| 4 | 弹窗 Close 按钮无效 | 视图用 Bootstrap **5** 的 `data-bs-dismiss`，打包的是 **v4.3.1** | `bootstrap.min.js` 自报 `v4.3.1`；`$().modal('show')` 能用**反证**运行时是 v4 |
| 5 | Housekeeping 报 `NotFound` | SSM 参数多了一段 `/home`（真实路由无它） | `/api/home/...` → 404，去掉 → 500（路由匹配上了，进了业务层） |
| 6 | Housekeeping 500 | 线上 `transactions` 表缺 `pet_type`/`user_id` | 代码假定 6 列，实测表只有 4 列；`CREATE TABLE IF NOT EXISTS` 让新列永不补上 |
| 7 | 每分钟约 42 次无效 SSM 调用 | `_Layout.cshtml` 查不存在的 `/petstore/rumscriptparameter` | **CloudTrail 50 条 GetParameter 里 49 条是它** |
| 8 | pethistory 单 Pod 永久 500 而 `ready=true` | `except psycopg.OperationalError` 抓不到 `InFailedSqlTransaction` | 容器内实测 `issubclass(...) == False`，二者是 `DatabaseError` 下的**兄弟分支** |
| 9 | 点领养显示「Adoption Complete」而后端什么都没做 | 三层叠加，见下 | 后端 `availability` 未变 + 数据库无新行 + 日志 `userId=` 为空 |

### 第 9 项值得单独记：一个三层叠加的「假成功」

```
① MakePayment 签名里没有 userId    → 模型绑定拿不到表单字段
② 我第一版改成 Request.Query["userId"] → 那是**查询串**，POST 表单里是空的
③ 拿到 result 却从不看 IsSuccessStatusCode → 400 被当成功，txStatus 保持 success
```

任何一层单独存在都会造成假成功。**第 ③ 层是最危险的**：它把前两层的错误全部隐藏，
页面正常显示领养完成页。从前端完全无法察觉，只有比对后端状态才会暴露。

修法除了传对 `userId`，还加了显式失败：`userId` 为空时直接抛异常而不是
发一个注定 400 的请求 —— **宁可报错也不要假成功**。

### 本轮犯的推理错误（连同前面已 4 次，现共 8 次）

5. **把「服务端要求」当成「调用方已满足」**。看到 `paymentapiurl` 路径修好后从
   404 变 400 就以为快好了，实际暴露的是另一个独立缺陷（缺 `userId`）。
   错误码变化说明「进了一层」，不代表「快好了」。
6. **凭服务类型推断启动耗时**。想给四个缺 `startupProbe` 的服务统一补上，
   实测才发现 Go/Rust 三个只要 6~7 秒（不需要），而 pethistory 要 **41 秒**（需要）。
   按数据决定，不按类型推断。
7. **误报「search-service 缺 startupProbe」**。它其实有（`startupGraceSeconds: 150`），
   是我在给用户的选项里表述错了。给结论前要再查一遍自己的上一轮输出。
8. **`Request.Query` 与 `[FromForm]` 混淆**。同一个 `userId` 在 GET 页面走查询串、
   在 POST 表单走 body，取值方式不能照搬。

### 探针现状（六个接流量服务）

| 服务 | startup | readiness | liveness | 依据 |
|---|---|---|---|---|
| petsite | 60×5s | 2×5s | 3×10s | 原先**三个全无**，而它是唯一入口 |
| search-service | 30×5s | 3×5s | 3×10s | 本来就有（`startupGraceSeconds: 150`） |
| pethistory | 30×5s | 2×5s | 3×10s | 实测启动 **41 秒**，liveness 余量只有 19 秒 |
| list-adoptions | — | 3×5s | 3×10s | 实测启动 7 秒，**刻意不加** |
| pay-for-adoption | — | 3×5s | 3×10s | 实测启动 7 秒，**刻意不加** |
| petfood | — | 3×5s | 3×10s | 实测启动 6 秒，**刻意不加** |
| traffic-generator | — | — | — | 只发流量不接流量，无 Endpoint 语义 |

`readinessProbe` 的效果是**实测**的，不是「配置下去了」：用 Chaos Mesh 切断
pethistory 到 Aurora 的网络后，Endpoint 从两个地址变为一个，不健康 Pod 确实被摘出。

参数让 readiness 先于 liveness 生效（`2×5s = 10 秒`摘流量，早于 liveness 的
`3×10s = 30 秒`重启）—— 先「别给我流量」，再「不行才杀掉重来」。

### 部署层面的新陷阱

**`cdk deploy` 成功 ≠ 新代码上线。** pethistory 用 `ContainerImageBuilder` 推**固定
`:latest`** tag（其余服务用 `DockerImageAsset`，按内容哈希生成新 tag）。
镜像内容变了但 Deployment 的 `image` 字段一个字符没动，K8s 判定 spec 无变化、
**不触发滚动更新**。`imagePullPolicy: Always` 也救不了 —— 它只在创建容器时生效。

实测：`ECR digest 5681042f…`（3 分钟前刚推）而 `Pod digest 27699d4f…`，
`PHEXIT=0`、CloudFormation 一切正常，而修复根本没生效。
**这类服务改完必须显式 `rollout restart`，且验证要比对 digest 而非看 rollout 输出。**

### 环境限制（记录，避免下次重试）

- **浏览器不可用**：Chromium 缺 `libatk-1.0.so.0`，装它需要 root 而**免密 sudo 不可用**。
  所以「用浏览器点一遍」做不到，改用带 cookie 的 HTTP 逐步提交真实表单来模拟，
  并在每步比对后端状态。这个替代方案发现了第 9 项缺陷，效果不差于点页面。
- 本机连不上 Aurora（10.1 段 vs 11.0 段，SG 只对集群内 Pod 开放）。
  查库要用 `kubectl run` 起临时 Pod 并指定 `pay-for-adoption-sa` 服务账号。

---

## 退出条件 —— ✅ 已达成

三阶段全部完成：七个 Deployment 全 Ready、压测 100% 通过、
agent 实调返回真实业务数据、图数据库的依赖关系已用主动故障注入逐条定性
（所有 `Calls` 边 100% 定性，3 条误报边已标 refuted）。

**遇到需要用户定夺的阻塞**（Bedrock 成本闸数字、任何破坏性操作确认）**停下来问，不要自行决定。**

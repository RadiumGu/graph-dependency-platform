# 可观测性四支柱在本平台的实测现状与借鉴

**日期**：2026-08-29 03:57 UTC
**触发**：用户读了 [持续性能分析正在成为可观测性的第四大支柱](https://tonybai.com/2025/08/04/continuous-profiling-fourth-pillar)（Tony Bai 解读 Datadog 原文），
希望本系统「存储 4 个支柱对应的所有数据」，并问 **tracing 是不是没有**。

---

## 一句话回答

**Tracing 不是没有——是有三条源在跑，一条都没进图谱，而且仓库里唯一名字带 X-Ray 的函数其实不读 X-Ray。**

Profiling 才是真的没有（此前已实测：DeepFlow CE 仅 On-CPU、不支持 Python，`profile.in_process` 0 行、`max(time)=1970`）。

---

## 实测数据

### Tracing 的三条源

| 源 | 实测结果 | 是否进图谱 |
|---|---|---|
| **应用侧分布式追踪**（W3C traceparent / OTel）经 DeepFlow L7 | **0 / 789,055**（近 1h，`trace_id`/`span_id`/`parent_span_id`/`x_request_id` 全为空） | — |
| **DeepFlow AutoTracing**（`syscall_trace_id`，eBPF 缝合，**无需应用埋点**） | **79,748 / 788,923 = 10.11%** 的请求有 request 侧 syscall trace id；response 侧 101,338 | ❌ |
| **AWS X-Ray** | 近 1h **6,949 条 trace**，服务图 **9 个节点** | ❌ |

`l7_flow_log` 的 span 字段是**齐的**（`trace_id` / `_trace_id_2` / `trace_id_index` / `span_id` / `parent_span_id` / `span_kind`），
但和 profiling 一样：**schema 有、数据为 0**。这是同一个陷阱，字段存在不等于有数据。

### X-Ray 服务图明细（近 1h）

| 服务 | 类型 | 出边数 | OK | Error | Fault | 总响应时间 |
|---|---|---|---|---|---|---|
| PetSearch | 应用 | 3 | 2639 | 0 | 0 | 59.347 |
| PetSearch | client | 1 | — | — | — | — |
| S3 | `AWS::S3` | 0 | 111 | 0 | 0 | 50.4 |
| STS | `AWS::STS` | 0 | 3 | 0 | 0 | 0.161 |
| ServicesEks2-ddbpetadoption... | `AWS::DynamoDB::Table` | 0 | 480 | 0 | 0 | 2.553 |
| payforadoption | 应用 | 0 | 2156 | 0 | 0 | 0.297 |
| petlistadoptions | 应用 | 0 | 2158 | 0 | 0 | 0.298 |

**关键**：X-Ray 看见了 `PetSearch → S3 / DynamoDB / STS` 这三条**服务到 AWS 托管服务**的调用，
而图谱里唯一 active 的 `Calls` 边只有 `petsite → petsearch`。DeepFlow 的 `Calls` 边是
Microservice→Microservice，**看不到这一层**。

另注意 `S3` 的总响应时间 **50.4** vs `PetSearch` 自身 59.347 —— 即 PetSearch 的墙上时钟
有很大比例花在 S3 上。这正是文章说的「Trace 定位到服务内部耗时」那一步。

### 仓库对 X-Ray 的实际处理：过滤掉，不是采集

grep 到 7 个文件提到 xray，逐个看实际用途后结论**与 grep 的字面印象相反**：

| 文件 | 实际用途 |
|---|---|
| `infra/lambda/etl_aws/handler.py:756,1226` | `SKIP_K8S_SVCS = {'kubernetes','xray-service'}`；并**主动删除** `xray-daemon`/`xray-service` 节点 |
| `infra/lambda/etl_deepflow/neptune_etl_deepflow.py:1169-1266` | `DEEPFLOW_SKIP = {'xray-daemon','xray-service'}`，注释写明「不代表真实业务依赖」 |
| `rca/collectors/layer2_tools.py:112 probe_xray` | **函数名和 docstring 都写「探查 X-Ray Traces」，但只建了 `stepfunctions` 和 `cloudwatch` 两个 client——没有 xray client** |
| `rca/collectors/layer2_direct.py:43` | `"xray": ("StepFunctionsProbe",)` —— 映射到的也是 Step Functions |

**全仓 grep `boto3.client("xray")` 零命中。** 也就是说：X-Ray 每小时产生 6,949 条 trace，
本平台**一行都没读过**，反而在两个 ETL 里把 X-Ray 自身的基础设施节点当噪音删掉。

`probe_xray` 是本仓库「命名与实现不符」的又一例，和之前的
`generate_group_report`（调不存在的函数、被 fallback 掩盖）同类。

### 四支柱逐项现状

| 支柱 | 采集 | 图谱里存了什么 | 判定 |
|---|---|---|---|
| **Metrics** | ✅ CloudWatch + DeepFlow | 节点属性：`Microservice` 的 `p50/p99_latency_ms`、`rps`、`error_rate`、`active_connections`、`pod_restart_count`、`metrics_updated_at`；`EC2Instance` 的 `cpu_util_avg`/`memory_util`/`disk_util`/`net_*`；`LambdaFunction` 的 `avg_duration_ms`/`p99_duration_ms`/`throttle_rate`/`invocations_per_min` | ✅ 有（**聚合快照，非时序**） |
| **Logs** | ✅ CloudWatch Logs | 只存 `log_source` 指针（`Microservice`/`EC2Instance`/`RDSCluster`/`LambdaFunction` 上都有），RCA 在诊断时才去读 | ✅ 指针模式，**这是对的** |
| **Traces** | ✅ X-Ray 6,949/h + DeepFlow AutoTracing 10.11% | **无任何 trace 形态的节点或边**。31 个节点类型里没有 Span/Trace 概念。`Calls` 边是 L7 的**聚合**（`calls` 计数、`avg_latency_us`、`p99_latency_ms`），是流量指标不是链路 | ❌ **缺口在存储层，不在采集层** |
| **Profiling** | ❌ | 无 | ❌ 真的没有（CE 版限制） |

---

## 对文章观点的采纳与一处必须的反对

### 采纳：文章的核心论点恰好是本平台的立项理由

文章最有力的一段不是「要加 profiling」，而是：

> 其真正的威力在于**能够与在生产环境中同时捕获的任何指标、追踪和日志相结合并关联起来**。

Datadog 是在自己产品内部做这个关联。**本平台的差异化恰恰是「关联的载体」是一张图**——
Neptune 是四支柱的 join substrate。这比「再接一个 profiler」重要得多。

文章的诊断链路 Metric → Trace → Profile → Code，映射到本平台：

| 文章步骤 | 本平台现状 |
|---|---|
| ① 现象（Metric）P99 突破 1s | ✅ 有：`p99_latency_ms` 在节点上，告警走 SNS→AlertBuffer |
| ② 定位（Trace）请求在某服务内部耗时 1.1s | ❌ **断在这里**：图谱没有 trace，X-Ray 数据从未被读 |
| ③ 下钻（Profile）关联火焰图 | ❌ CE 版不支持（Off-CPU / Python 均为 Enterprise） |
| ④ 根因（Code）锁竞争 / channel 阻塞 | ❌ |

**②③④ 全断。** 而 ② 是**唯一一个数据已经在手、只差没接**的环节。

### 反对：不要把四支柱的原始数据存进 Neptune

用户说「希望这个系统能存储 4 个支柱对应的所有数据」。**这一条我建议明确不做**，理由是量级：

| 数据 | 量级 | 对比 |
|---|---|---|
| 当前整张图谱 | **867 节点 / 1341 边** | 基准 |
| L7 flow log | **789,055 行/小时** ≈ 1,900 万/天 | 一小时就是全图的 **910 倍** |
| X-Ray trace | 6,949/小时 ≈ 16.7 万/天 | 一天是全图的 **190 倍** |

把一小时 L7 写进 Neptune 就等于 78.9 万个节点。Neptune 不是遥测存储，
**这个方向会把唯一可信的拓扑源头淹掉**。

而且本平台**已经有正确范式**：logs 从来只存 `log_source` 指针，不存日志行。
应该把这个范式**扩展到 traces 和 profiles**，而不是改掉它。

对应关系应该是：

```
原始遥测留在原生存储                图谱里存三类东西
─────────────────────           ────────────────────────
CloudWatch Logs                  ① 指针（去哪里查）
CloudWatch Metrics       ──→     ② 聚合判定（p99、error_rate、resilience_score）
X-Ray Traces                     ③ 派生的拓扑与因果（边、变更事件、先验）
DeepFlow ClickHouse
（未来）Pyroscope/Parca
```

**「存储 4 个支柱的所有数据」应改为「4 个支柱的所有数据都能从图谱一跳可达」。**
前者是数据湖，后者是索引——后者才是本平台该做的，也是它已经在 logs 上做对的。

---

## 建议的优先级

排序依据是「数据已在手 / 需新建组件」和「是否填补已知缺口」，不是文章的行文顺序。

### P0：接入 X-Ray，作为独立依赖源与运行时验证源

**数据已在手，零新增组件，且填补两个已知缺口。**

1. **补齐服务→AWS 托管服务这一层依赖。** X-Ray 看见 `PetSearch → S3/DynamoDB/STS`，
   DeepFlow 的 `Calls` 边（Microservice→Microservice）看不见。
2. **给 `AccessesData` 的 `runtime_verified` / `drift_status` 提供第二个证据源。**
   实测这套机制**已经在跑且只在一种边上跑**：

   | 边类型 | 边数 | `runtime_verified` | `drift_status` | `last_drift_check` |
   |---|---|---|---|---|
   | `AccessesData` | 30 | **23** | **23** | **23** |
   | `DependsOn` | 21 | 0 | 0 | 0 |
   | `Calls` | 18 | 0 | 0 | 0 |

   即 CFN 声明的 `AccessesData` 已有 23 条被运行时验证过，而 X-Ray 正是能验证
   service→S3/DynamoDB 这类边的源。这不是新机制，是**给已有机制补一个输入**。
3. **顺带修 `probe_xray`**：它的名字和 docstring 宣称读 X-Ray 而实际不读。
   要么真接上 X-Ray，要么改名为 `probe_stepfunctions`——现在这样会误导后续所有人。

**注意一个先决条件**：X-Ray 服务名与图谱 `Microservice.name` **对不上**——
X-Ray 是 `PetSearch`（驼峰），图谱是 `petsearch`（小写）；`payforadoption`/`petlistadoptions` 能对上。
接入前必须先定名字映射规则，否则会造出一批孤立节点。

### P1：把 DeepFlow AutoTracing 的 10.11% 用起来

`syscall_trace_id` 已有 10.11% 填充率，**不需要应用埋点**。它能做的是
把 `Calls` 边从「A 调过 B」升级为「A→B→C 的同一条请求链」，
这对 RCA 的传播链推断（`graph_rag_reporter` 已有传播链字段）是直接输入。

代价：需要在 ClickHouse 侧做 span 缝合查询，比 P0 重。

### P2：应用侧分布式追踪（0% → 有）

L7 的 `trace_id` 全为 0 说明应用没有传播 W3C traceparent。这需要改**应用代码**
（或加 OTel auto-instrumentation sidecar），是本清单里唯一需要动业务服务的一项。
收益是把 AutoTracing 的 10.11% 提到接近 100% 并跨越 AWS 托管服务边界。

### P3：Profiling（第四支柱本身）

文章的主题，但在本环境是**最贵且收益最不确定**的一项：

- DeepFlow **CE 版不支持** Off-CPU、内存 profiling、Python profiling
  ——而「CPU 不高但很慢」恰恰要 Off-CPU，这正是文章举的 channel 阻塞例子
- 要落地必须引入新组件：**Pyroscope**（Grafana）或 **Parca**，二者都对接 OTel profiling signal
- 已实测的语言构成里有 Python 服务，CE 版对它完全无能为力

**建议**：先做 P0/P1（数据在手、填补已知缺口），P3 等到 ② 环节通了再评估。
按文章自己的路线图，也是「从一个核心服务入手、在预生产验证开销」而非全量铺开。

---

## 一个必须记下的方法论重复

这次核查里同一个陷阱出现了**第三次**：

| 对象 | 表象 | 实际 |
|---|---|---|
| DeepFlow profiling | `profile` 库和表都在 | `profile.in_process` **0 行**，`max(time)=1970` |
| DeepFlow tracing | `l7_flow_log` span 字段齐全 | `trace_id` **0 / 789,055** |
| `probe_xray` | 函数名 + docstring 明写 X-Ray Traces | 只有 `stepfunctions` + `cloudwatch` client |

**结论**：判定某支柱「有没有」，必须查**填充率**和**实际 API 调用**，
不能查 schema 字段是否存在或函数名叫什么。这与已固化的教训一致
（用逐文件 diff 对照权威源，不用标记字符串匹配）。

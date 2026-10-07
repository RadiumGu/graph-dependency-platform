# 评估：能否用 DeepFlow 自带拓扑 API 取代自研 ETL

**日期**：2026-10-07
**结论**：**不能，因为那个 API 不存在。** 但这次评估翻出了 3 个可落地的改进和 1 个真实缺陷。

---

## 一、先推翻问题的前提

「DeepFlow 自带拓扑 API」这个东西**没有**。核实结论：

> DeepFlow 没有任何返回 `{nodes, edges}` 的拓扑端点。
> 它对外的一等查询面只有 **一个**：`POST /v1/query/`，发的是 SQL。

依据：

| 事实 | 依据 |
|---|---|
| querier 只暴露一个 SQL-over-HTTP 端点，form 参数 `db` / `sql` / `data_precision` | 源码 `server/querier/router/query.go` |
| 「边」是 ClickHouse 里的**双端表**，靠 `_0`(client) / `_1`(server) 后缀 tag 表达两端 | 源码 `server/querier/common/const.go` 的 `PEER_TABLES`：`l4_flow_log` / `l7_flow_log` / `application_map` / `network_map` / `vtap_flow_edge_port` / `vtap_app_edge_port` |
| 拓扑是**查询时**由客户端 `GROUP BY auto_service_0, auto_service_1` 聚合出来的，不被持久化 | `server/querier/README.md` 的示例写法 |
| UI 上的 Universal Map 是 Grafana 插件在**前端**发上述 SQL 后拼的图 | `deepflowio/deepflow-gui-grafana` 的 `deepflow-querier-datasource` |
| DeepFlow 没有图数据库。持久化只有 ClickHouse（采集数据）+ MySQL（**仅元数据**） | [官方 FAQ](https://deepflow.io/docs/diagnose/FAQ/) 原文：「mysql stores metadata ... clickhouse stores real-time collected data」 |
| Grafana datasource / PromQL 都是 `POST /v1/query/` 之上的兼容层；GraphQL 不存在 | 同上（router 源码反证） |

唯一接近「预存图」的是分布式追踪的 Trace Tree（`GET /api/traces/:traceId`），但那是**单条请求的调用树**，不是服务级拓扑。

**所以「换成调用一次拓扑 API」这条路从一开始就不存在。** 而且我们**已经在用那个唯一的真实接口** —— `neptune_etl_deepflow.py` 的 `ch_query()` 直接 `requests.post(f"http://{CH_HOST}:8123/", data=sql)`，打的就是 SQL。

---

## 二、自研 ETL 实际做了什么：取数只占两成

`infra/lambda/etl_deepflow/neptune_etl_deepflow.py`，2113 行自有代码（约 35–40% 是注释与踩坑记录）。按职责归类：

| 职责 | 约行数 | 占比 | 能否外包给 DeepFlow |
|---|---|---|---|
| 纯数据获取与传输（ClickHouse / X-Ray / K8s / EKS token） | ~470 | ~22% | **部分可以** |
| 对账 / 漂移 / 拓扑变更 / GC | ~450 | ~21% | 不能 |
| Neptune 写语句构造 | ~450 | ~21% | 不能 |
| 身份归一 | ~230 | ~11% | 部分可以（见改进 2） |
| 错误处理与降级 | ~250 | ~12% | 不能 |
| 实体 → 契约类型映射 | ~180 | ~9% | 不能 |
| 契约执行（`assert_edge_type` 等 11 处） | ~40 | ~2% | 不能 |

**它读 Neptune，读得很重** —— 这是「无状态拓扑查询」根本替代不了的部分：

| 读的位置 | 读什么 | 为什么必须读 |
|---|---|---|
| `_resolve_datastore_ips` (482/531/546) | 带 `endpoint` 的节点 + `BelongsTo` | 建「私有 IP → 数据存储节点」反查表，并沿实例→集群传播 |
| `upsert_datastore_flows` (699/730) | `inE('AccessesData')` 计数 | 写前探测 + 写后回读确认落库，不谎报成功 |
| `run_drift_detection` (965-972) | 服务**已声明**的依赖边 | 声明 vs 观测的漂移对账 |
| `batch_fetch_dependency_and_update` (1763) | 上下游计数 | 回写 `upstream_count` / `is_entry_point` 等派生属性 |
| `reconcile_calls_edges` (1595/1646) | `last_seen < cutoff` 且 `active=true` 的边 | 失活对账 —— DeepFlow 只报「现在看到什么」，不会把消失的依赖标死 |

### DeepFlow 原理上给不出的东西

1. **第二观测源合并**：`fetch_xray_dependencies`（743-820）调 `xray.get_service_graph`，与 DNS 做 OR 合并。代码注释给的理由是实测出来的 —— SDK 启动解析一次 DNS 即复用连接，VPC 端点不产生公网 DNS 查询，**这些高频依赖 DeepFlow 看不见**。
2. **声明 vs 观测的漂移对账**（887-1080）：写 `drift_status` ∈ {ok, declared_not_observed, observed_not_declared}。DeepFlow 根本没有「声明侧」这个概念，`declared_not_observed` 它永远给不出。
3. **边失活与追加式变更日志**（1479-1667）：带 90 天保留期裁剪。
4. **ECR 启动依赖**（2026-2085）：数据来自 K8s Pod spec 的 `image` 字段，不是流量。
5. **契约门禁**：`assert_edge_type` / `assert_node_type` / `assert_source` 共 11 处。

---

## 三、三个可落地的改进（本次评估的真实产出）

虽然替代不成立，但对照之下发现我们**没用上 DeepFlow 已经算好的东西**。

### 改进 1：我们在自己聚合原始流日志，而 DeepFlow 已有预聚合的边表

现状（第 1849-1855 行）：查 `flow_log.l7_flow_log`，自己做 `count()`、`avg(response_duration)`、`quantile(0.99)(response_duration)`。

而 `flow_metrics.application_map` 是 DeepFlow **预聚合维护的 L7 应用边表**，现成字段包括 `request` / `response` / `rrt`(平均时延) / `rrt_max` / `error` / `client_error` / `server_error` / `timeout`，以及比率字段 `error_ratio` / `client_error_ratio` / `server_error_ratio` / `success_ratio`。

两个收益：

- **成本**：不用每轮扫原始流日志再聚合。
- **保留期更长**：`flow_metrics.*.1m` 默认 TTL **168h（7 天）**，而 `flow_log.l7_flow_log` 只有 **72h（3 天）**。

依据：`server/server.yaml` 的 `flow-log-ttl-hour` / `flow-metrics-ttl-hour`；字段清单见 `db_descriptions/clickhouse/metrics/flow_metrics/application_map.en`。

### 改进 2：我们在手搓 IP→服务映射，而 DeepFlow 有 `auto_service` 自动分组

现状：主查询取 `IPv4NumToString(ip4_0)` / `ip4_1` 这种**裸 IP**，然后用 `build_ip_service_map`（1126-1212，87 行）自己把 IP 映射回服务。

DeepFlow 的 querier 提供 `auto_service_0` / `auto_service_1`（服务级聚合）与 `auto_instance_0/_1`（实例级，含 type），身份归一是它自己做的。

**实测：我们代码里 `auto_service` 出现 0 次。** 一次都没用上。

需要诚实标注的边界：`auto_service` 是 DeepFlow 自己的命名空间，映射到图谱的规范名仍需 `K8S_SERVICE_ALIAS`；而且 RDS / DynamoDB 这类**托管服务端点**不在 K8s 资源表里，`_resolve_datastore_ips` 那条 `endpoint` → `getaddrinfo` → 私有 IP 的路子仍然要留着。所以这是**减少**手搓量，不是消除。

### 改进 3：`direction_score` —— 和本仓的证据分级哲学天然对齐

`application_map` / `network_map` 有一个字段 `direction_score`，取值 0–255，**量化「client→server 这个方向判定的可信度」**，255 表示方向必然正确。

本仓的第一条设计原则就是证据要分级（`edge_verification` 的六档状态：`untested` / `confirmed` / `refuted` / `inconclusive` / `modeling_artifact` / `bootstrap_only`）。一个现成的、来自采集层的方向置信度，正好能喂给这套体系 —— 比「观测到即写入」粗暴得多的做法好。

**实测：我们代码里 `direction_score` 出现 0 次。**

---

## 四、顺带发现一个真实缺陷：`LIMIT 100` 是静默截断

`neptune_etl_deepflow.py:1855`：

```sql
GROUP BY src_ip, dst_ip, server_port, l7_protocol_str
HAVING calls >= 2 ORDER BY calls DESC LIMIT 100 FORMAT TSV
```

**第 101 条边起被无声丢掉。** 没有告警、没有日志、图里就是没有那条依赖。

这和 PR #62（`aws_resilience.py`）要解决的是**同一类缺陷**：那个 PR 的核心取向是「分页上限必须抛 `PaginationTruncated` 而不是静默截断 —— 宁可失败得吵，也不要悄悄给出不完整的结果」。而这里的 `LIMIT 100` 正是被它漏掉的一处：护栏在 `make_client` 的 boto3 分页上，**管不到我们手写的 SQL**。

后果不只是少几条边。依赖图要喂 DR 的拓扑排序,少一条边就可能把恢复顺序排错,而且**无从察觉**。

另外 `ch_query` / `ch_query_json`（170-190）在失败时一律 `except` 返回空 `[]`/`{}` —— 又一处「采集失败在图里不留痕迹」，下游无法区分「这条边不存在」与「这次没采到」。这是本仓已知的系统性缺口（164 处「记日志但继续」），此处是其中之一。

**建议**：把 `LIMIT 100` 改成带上限的分页循环，超限抛异常；或至少在命中 `LIMIT` 时写一条 `collection_truncated` 标记进图。

---

## 五、结论

| 问题 | 答案 |
|---|---|
| DeepFlow 有拓扑 API 吗 | **没有。** 只有 `POST /v1/query/` 的 SQL，拓扑是客户端聚合 |
| 能取代自研 ETL 吗 | **不能。** 取数只占 ~22%，其余是身份归一、契约映射、漂移对账、失活对账、Neptune 回读 |
| 图持久化能外包吗 | **不能。** DeepFlow 没有图库；且 TTL 只有 3–7 天，长期事实必须靠我们固化进 Neptune |
| 那这次评估的价值 | 3 个改进（用预聚合边表、用 `auto_service`、用 `direction_score`）+ 1 个真实缺陷（`LIMIT 100` 静默截断） |

**正确的形态认识**：DeepFlow 是我们的**上游事实源之一**（零侵入拿带黄金指标的调用边），不是我们这层的替代品。它和 X-Ray / AppSignals / CFN 是并列的四个源,而「四源合并 + 身份归一 + 契约执行 + 证据分级 + 持久化」才是本平台的本体。

一句话:**我们找错了外包对象 —— 该外包的不是「写图」,而是「算指标」,而那部分我们正在重复劳动。**

### 维护状态（供风险评估）

DeepFlow 活跃：最新 release `v7.2.2`（2026-09-10），7.2.x 约两周一版（v7.2.0 8/13 → v7.2.1 8/27 → v7.2.2 9/10），main 最新提交 2026-09-21。

### 未能核实项

- PromQL 的确切 route（官方称 v6 起支持，但未在 querier 源码定位到路由行）
- `l7_protocol` tag 的逐字拼写（核对的是 metric 描述文件，未打开 tag 描述文件）
- `data_source_retention_time_max` 的单位

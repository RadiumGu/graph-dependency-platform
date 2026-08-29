# PetSite 应用流量覆盖度实测

**日期**：2026-08-29 15:18 UTC
**问题**：EC2 上跨 VPC 运行的 traffic-generator，是否覆盖了 PetSite 集群内所有相关应用？
**方法**：DeepFlow ClickHouse（`11.0.2.30:8123`）按目标 Pod IP 统计 `flow_log.l7_flow_log`，排除 `/health/status`，窗口 10 分钟

## 结论

**覆盖了 `petadoptions` 命名空间全部 5 个微服务，以及 SNS / Step Functions / DynamoDB / Aurora。**但覆盖不等于均衡，实测过程中另外查出三个问题（见文末与 `sqs-orphan-and-topology-hints` 文档）。

## 应用侧覆盖

| 服务 | 业务请求 | 命中端点 |
|---|---|---|
| `search-service` | **27,037** | `/api/search?petid=<N>` 21,714 + 各 pettype/petcolor 组合 |
| `service-petsite` | **5,522** | `/?selectedPetType=…` 1229、`/Payment/MakePayment` 1226、`/Adoption/TakeMeHome` 1221、`/PetListAdoptions` 1217、`/` 462、`/housekeeping/` 85、`/pethistory` 68、`/pethistory/deletepetadoptionshistory` 14 |
| `pay-for-adoption` | **1,876** | `/api/home/completeadoption` 444 + 按宠物变体、`/api/home/cleanupadoptions` 125 |
| `list-adoptions` | **1,535** | `/api/adoptionlist/` |
| `pethistory-service` | **86** | `/petadoptionshistory/api/home/transactions` |
| `traffic-generator` | 0 | 已缩容到 0，预期 |

Deployment 就绪状态：`list-adoptions` 2/2、`pay-for-adoption` 2/2、`pethistory-deployment` 2/2、`petsite-deployment` 2/2、`search-service` 2/2、`traffic-generator` 0/0。

## AWS 侧覆盖

```
SNS    NumberOfMessagesPublished      438
StepFn ExecutionsStarted                50
DDB    ConsumedWriteCapacityUnits    1216      ConsumedReadCapacityUnits  2973.5
Aurora CommitThroughput                8.6/s
SQS    NumberOfMessagesSent           409      NumberOfMessagesReceived     0   ← 见缺陷文档
```

> Aurora 的 `Queries` 指标返回 None 属正常——那是 MySQL 专属指标，本集群是 Aurora PostgreSQL。

## 不在覆盖范围内

`awesomeshop` 命名空间是独立应用，不属 PetSite，生成器不会触达。**且它 6 个 Deployment 全部 `ready=0/0`**（`auth-service`、`frontend`、`gateway-service`、`order-service`、`points-service`、`product-service`）——计算层全停而数据层可能仍在计费，对应既有的 T-094。

## 方法论教训：LIMIT 会把结论截歪

首次查询设了 `LIMIT 40`，结果 `pethistory-service` 显示零流量、`pay-for-adoption` 只出现 `/health/status`，据此得出了「pethistory 未覆盖」的**错误结论**。原因是 `search-service` 的 `petid=<N>` 变体单独占了几十行，把其他服务的业务条目挤出了前 40。

**改法**：排除健康检查、把高基数路径参数归并（`petid=\d+` → `petid=<N>`）、把 LIMIT 提到 200，再按服务聚合。

## 发现一：search-service 占全部集群内 HTTP 的约 78%，且存在放大效应

一轮业务流只产生 1 次真实搜索意图，却打出约 27 次 search 请求——因为 petsite 渲染首页时会为 26 个宠物卡片**各发一次** `/api/search?petid=<N>`，生成器还会额外直连 search 一次。

**据此判断「search-service 是瓶颈」之前必须先扣掉这个放大系数**，否则会把渲染模式的问题误判为服务容量问题。

## 发现二：两个 search Pod 严重倾斜 76 / 24

```
search-service   合计 26779
    11.0.3.232   20372   76.1%
    11.0.3.211    6407   23.9%
```

其余服务在 56/44 到 67/33 之间，只有 search 是 76/24。**根因不是连接复用，是 Kubernetes 拓扑感知路由** ——详见 `sqs-orphan-and-topology-hints_20260829-1518.md`。已修复至 55/45。

## 发现三：SQS 只进不出

`NumberOfMessagesSent` 有值而 `NumberOfMessagesReceived` 恒为 0，队列积压持续增长。**消费者从未部署**——详见缺陷文档。

## 可复用的查询

```python
# 按服务聚合业务请求（排除健康检查、归并高基数路径参数）
q = """SELECT IPv4NumToString(ip4_1) AS dst, request_resource AS res, count() AS c
FROM flow_log.l7_flow_log
WHERE time > now() - INTERVAL 10 MINUTE AND ip4_1 IN (<pod ip 列表>) AND l7_protocol = 20
  AND request_resource NOT LIKE '%health%'
GROUP BY dst, res ORDER BY c DESC LIMIT 200 FORMAT TSV"""
# 经 URL 编码走 GET: http://11.0.2.30:8123/?query=<quote(q)>
# 注意: l7_protocol = 20 是 HTTP, 120 是 DNS
```

真实连接数要用 L4 的 `sum(syn_count)`，不能只看 `uniqExact(client_port)`——后者在本次排查中一度让我误判连接复用行为。

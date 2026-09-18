# list-adoptions → search-service 的连接复用：要不要修

**日期**：2026-08-30 14:51–15:10 UTC（压测中，近 10 分钟窗口）
**问题**：约 1.6 请求/连接，keep-alive 是不是坏了？要不要修？
**结论**：**机制不是「keep-alive 没开」，而是「并发扇出 11.5 倍 vs 空闲池上限 2」。
值得修，但不该并入本次部署。**

---

## 一、实测数据

| 调用方 | 新建连接（L4 `sum(syn_count)`） | 请求（L7 `l7_protocol=20`） | 请求/连接 |
|---|---|---|---|
| **list-adoptions → search** | **10,278** | 19,916 | **1.94** |
| petsite → search（对照） | 1,073 | 3,040 | 2.83 |

窗口：近 10 分钟；IP 对取自各自的 Pod IP，非服务名聚合。

关键：**1.94 不是 1.0** —— 复用**部分在工作**，所以「keep-alive 完全没生效」这个描述不准确。
（顺带：petsite 的 2.83 也不高。之前记录的 7.1 是另一个负载窗口的值，
不同窗口不可直接比较，这里同窗口同口径重测。）

## 二、扇出倍数：机制的关键

```
list-adoptions 入站业务请求（10 分钟，排除 /health）: 1,737
出站 search 调用:                                   19,916
→ 扇出 11.5 次/请求
```

看源码 `petlistadoptions-go/petlistadoptions/repository.go:100-101`：

```go
wg.Add(1)
go searchForPet(ctx, r.logger, &wg, adoptions, t, petSearchURL)
```

**每个入站请求为每个 adoption 起一个 goroutine**，实测约 11.5 个并发。

而 `repository.go:125`：

```go
client := http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport)}
```

这行**本身没问题** —— 虽然每次调用都新建 `http.Client`，但它包的是包级单例
`http.DefaultTransport`，**连接池活在 Transport 里**，所以池是共享的。

问题在 `http.DefaultTransport` 的默认值：**`MaxIdleConnsPerHost = 2`**
（Go 的 `DefaultMaxIdleConnsPerHost`）。

**核算**：11.5 个并发连接，空闲池每主机只留 2 条 →
约 9 条用完即关、无法复用。这与实测的 1.94 请求/连接量级一致。

> **所以主因是池容量上限，不是 keep-alive 被禁用。**
> 若真的是 `Connection: close` 或每请求新建 Transport，比值会贴近 1.0。

## 三、另一个独立缺陷：`resp.Body` 从未关闭

```go
resp, err := client.Do(req)
if err != nil { level.Error(logger).Log("err", err); return }

pets := []pet{}
err = json.NewDecoder(resp.Body).Decode(&pets)
if err != nil { level.Error(logger).Log("err", err); return }
```

整个 `searchForPet` **没有任何 `defer resp.Body.Close()`**。

对照：同仓库的 `payforadoption-go/payforadoption/repository.go:123` **有**
`defer resp.Body.Close()` —— 所以这不是项目风格，是 `petlistadoptions-go` 单独漏了。

后果分两种情况：
- **正常路径**：`json.Decode` 读到 EOF 时，Go 的 `net/http` 会把连接还给池 ——
  所以复用没有完全断（这解释了为什么是 1.94 而不是 1.0）。
- **错误路径**：`Decode` 失败直接 `return`，body 既没读完也没关 ——
  连接被彻底放弃，直到 finalizer 或 `IdleConnTimeout`（90s）回收。
  这是真正的资源泄漏，且只在出错时显现。

## 四、不修的代价：实测都很小

| 维度 | 实测 | 判断 |
|---|---|---|
| 连接建立速率 | 10,278 / 600s = **17.1 个/秒** | 每次握手 1 个 RTT，集群内约 0.5ms，聚合影响可忽略 |
| conntrack 压力 | 节点 `nf_conntrack_count` **1,787 / 131,072 = 1.4%** | 远未接近上限，不是问题 |
| socket 状态 | `ss -s`：TCP 248，closed 215，timewait 87 | 有明显 churn，但绝对量很小 |
| 跨 AZ 握手 | 拓扑提示已生效，list-adoptions 与 search 各自打同 AZ | **这一项已被上一轮的拓扑修复消化掉** |
| CPU | list-adoptions pod 级 p99 **16m** | 连接开销没有反映成可观测的 CPU 成本 |

**所以「不修」的实际代价是：约 17 次/秒的多余三次握手，加上错误路径下的连接泄漏。**
前者可忽略；后者是真缺陷但只在异常时触发。

## 五、修的方案与风险

三处改动，从小到大：

**① 补 `defer resp.Body.Close()`（1 行，零风险）**
```go
resp, err := client.Do(req)
if err != nil { ...; return }
defer resp.Body.Close()
```
这是纯 bug 修复，无论要不要动连接池都该做。

**② 用包级共享 Transport 并调大空闲池（约 10 行）**
```go
var searchTransport = &http.Transport{
    MaxIdleConns:        100,
    MaxIdleConnsPerHost: 32,   // ≥ 扇出倍数（实测 11.5），留余量
    IdleConnTimeout:     90 * time.Second,
}
var searchClient = &http.Client{Transport: otelhttp.NewTransport(searchTransport)}
```
把 client/transport 提到包级，`searchForPet` 里直接用。
风险：空闲连接常驻会占 search-service 的 fd 与 conntrack 条目 ——
按 2 个 list-adoptions 副本 × 32 = 64 条常驻，对照当前 1,787 的 conntrack 用量可忽略。

**③ 限制扇出并发（改动最大，本次不建议）**
11.5 倍扇出本身是「首页 26 个宠物卡片各查一次」那个放大效应的一部分。
真正的优化是批量查询接口（一次查多个 petid），但那要同时改 search-service，
属于应用架构改动，不在本轮范围。

**共同风险**：①②都要重建 Go 镜像并重新部署 list-adoptions。
这会产生新的 `DockerImageAsset` hash → 新镜像推送 → 扩大部署面。

## 六、结论

**修，但不要并入本次部署。**

理由：

1. **`Body.Close()` 是真 bug，应该修** —— 一行，零风险，同仓库其他服务都做对了。
2. **`MaxIdleConnsPerHost` 是低比值的主因，改法标准** —— 但收益是「省掉 17 次/秒的握手」，
   实测 conntrack 1.4%、CPU p99 16m，**没有任何指标在告急**。
3. **关键的时机理由**：本次部署已经要替换两个节点组（全部 Pod 迁移）
   并推上积压 4.5 个月的 CDK 变更。再叠加一次 Go 镜像重建，
   会让「出问题时是哪一项造成的」变得无法区分。
   连接复用不是紧急项，没有理由挤进这个窗口。

**建议顺序**：先完成节点组替换与 CPU 右调并验证稳定，
再单独提一个 PR 做 ①+②（同一次镜像重建里一起做，因为都要重新构建），
部署后用同一套查询复测请求/连接比值验证效果。

## 七、一个与依赖梳理直接相关的副产品

这个低复用率会**抬高 DeepFlow 报出的连接数**。如果拿连接数当健康指标，
`list-adoptions → search` 会看起来像有「连接风暴」，而实际上
它只是 11.5 倍扇出撞上 2 条空闲池上限的算术结果。

这与本项目已记录的模式同源：**指标异常不等于故障，要先证实机制再下结论**。
判别方法也一样 —— 用 L4 `sum(syn_count)` 配 L7 请求数算比值，
而不是只看 `uniqExact(client_port)`（后者在上一轮排查里就误导过一次）。

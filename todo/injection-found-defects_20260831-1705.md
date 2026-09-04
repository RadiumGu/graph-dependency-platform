# 真实故障注入实测出的缺陷（读代码发现不了的那一类）

> 生成时间：2026-08-31 09:00 UTC
> 来源：2026-08-30 15:00 – 2026-08-31 09:00 在 PetSite 非生产环境（ap-northeast-1）
> 对 `search-service` 做的 5 轮 `http_chaos abort` 真实注入，外加对活图谱的普查。
>
> **收录标准：只收「读代码、看架构图、跑单测都发现不了，必须真的注入或真的查活图谱才暴露」的缺陷。**
> 因此不含拼写错误、不含设计评审能挡住的问题。
>
> 每条给四样东西：**症状 → 根因 → 为什么静态手段发现不了 → 修法与验收**。
> 最后一节是把这些个案收敛成的四条方法论。

---

## 摘要

| # | 缺陷 | 危害 | 状态 |
|---|---|---|---|
| 1 | SLI 把 DNS NXDOMAIN 算进 HTTP 成功率 | 100% 的服务被报成 31%，实验在 preflight 就死 | ✅ 已修 |
| 2 | `ChaosMCPClient.delete` 位置参数撞车 → CRD 泄漏 + tproxy 残留 | 两个 Pod CrashLoopBackOff，只能删 Pod 重建 | ✅ 已修 |
| 3 | 护栏挂在注入目标上 → 实验必然自杀 | 零判定，5 轮里有 2 轮死在这 | ✅ 已修 |
| 4 | `abort` 故障下成功率是盲的 | 生效的注入被判成「没影响」，真实边被判 refuted | ✅ 已修 |
| 5 | `observer_min_success_rate` 首个采样点不初始化 | 健康的观测方得到「判不了」而不是「0 退化」 | ✅ 已修 |
| 6 | Phase 4 正常完成路径从不删 CRD | PASSED 的实验结束后 CRD 仍在集群里 | ✅ 已修 |
| 7 | 候选边查询用 K8s 名匹配图谱规范名 | 边明明在图里，却报「无候选边」，写回 0/2 | ✅ 已修 |
| 8 | 采集失败伪装成「零流量」污染谷值 | 一次查询抖动 → 吞吐塌陷 100% → **误判 confirmed** | ✅ 已修 |
| 9 | `source` 词表声明了但没有门禁 | 1240 条边/节点带未声明取值 | ✅ 已修 |
| 10 | Lambda 层缺 `requests`，`etl_xray` 已死 2 天 | 一整个观测源静默失效，无人发现 | ✅ 已修 |
| 11 | Phase 5 只看 SLI，实验报 PASSED 却留下坏 Pod | 污染带进下一轮实验基线 | ✅ 已修 |
| 12 | Pod 标签 ≠ Deployment 名（第三套命名空间） | preflight 报「无 Running Pods」 | ✅ 已修 |
| 13 | 注入目标 SLI 用 kubectl 名查 DeepFlow → 假 100% | 稳态门被假数据通过 | ✅ 已修 |
| 14 | PolicyGuard R002 白名单对 FIS 目标语义不适用 | 仓库原有 FIS 模板从未跑过 | 已定位，规则待补 |
| 16 | 吞吐塌陷取 min 比单点基线，抖动伪造塌陷 | **一条边被误判 confirmed** | ✅ 已修 |
| 17 | 吞吐通道无法归因（失败 vs 不被调用 vs 测量断） | 弱证据被当强证据 | ✅ 已修 |
| 18 | `fis_vpc_endpoint_disrupt` 目录声明的目标类型是错的 | 该故障类型从未能执行 | ✅ 已修 |
| 20 | 无法识别「注入没生效」，据此判 refuted | **凭空证伪一条真实依赖** | ✅ 已修 |
| 21 | FIS 目录 37 条里 5 条从来不可执行（14%） | 声明存在但一次没跑过 | ✅ 已修 |
| 22 | `target_service` 兼任 kubectl 选择器与图谱节点名 | 边切断拓扑无法表达 | ✅ 已修 |
| 23 | 「PetSite 只在启动时读一次 SSM」这条既有认知是错的 | 依赖强度判断全错 | ✅ 已更正 |
| 24 | 契约三个元字段全部零消费方，note 过期半个月无人发现 | 身份键迁移被永久阻塞 | ✅ 已修 |
| 25 | 采集 skip 规则只挡新写入、从不回收旧数据 | 9 个越界节点长期滞留 | ✅ 已修 |
| 26 | 节点没有生命周期机制，「跟随节点」是悬空的 | 511 个死 Pod 长期滞留 | ✅ 已修（默认 dry-run） |
| 27 | etl_aws 从不写契约声明的统一时间戳字段 | 判据覆盖率 1.4%，机制永不触发 | ✅ 已修 |
| 28 | 6 个类型切 arn 会被 etl_cfn 撕成两份 | 身份键升级不可行却无人知 | ✅ 已申报阻塞 |

其中 **#7 / #8 / #10 是 08:30–09:00 为了拿到第一条边的判定而查出的**，
**#11 – #14 是 10:10–11:12 扩大验证覆盖面时查出的**，都不在最初报给用户的 6 条里。

**最终产出**：8 条边判 **confirmed**、2 条判 **inconclusive**，
已判定覆盖率 **0.0% → 10.64%**，横跨 `Calls` / `AccessesData` 两种边类型与
Chaos Mesh / FIS 两种后端。

---

## 1. SLI 把 DNS NXDOMAIN 算进 HTTP 成功率

**症状**：稳态检查 `success_rate >= 95%` 永远过不了，实验在 Phase 0 就中止。
三个服务报出来的成功率是 `list-adoptions` 31.37%、`search-service` 69.16%、`petsite` 100%。

**根因**：`metrics.collect()` 只按 `request_domain LIKE '%svc%'` 过滤，没有限定协议。
K8s 默认 `ndots=5` 会把 `svc.ns.svc.cluster.local` 逐段拼上搜索域重查一遍，
产生大量**预期内的** NXDOMAIN，被当成服务故障计入成功率。

实测（5 分钟窗口）：HTTP **20,645 条全部成功**；DNS **16,856 条里 13,516 条 NXDOMAIN**。
加协议过滤后三方全部 **100.00%**。

**为什么静态手段发现不了**：SQL 本身没有语法或逻辑错误，`request_domain LIKE` 是合理写法。
要发现它必须知道「这个环境的 DNS 查询也会带上服务名进 l7_flow_log」——
这是环境事实，不是代码事实。

**危害的第二层比第一层要紧**：不只是过不了 preflight，而是**噪声底盘既大又随 DNS 行为波动**。
据此算出的退化率不可信——一次真实的 20pp HTTP 退化会被 DNS 抖动淹没，或者被它伪造出来。

**修法**：加应用层协议白名单，用 `l7_protocol_str IN ('HTTP','HTTP1','HTTP2','gRPC')`
而不是硬编码数字枚举（实测本环境 HTTP=20 / DNS=120，但数字是 DeepFlow 内部实现，会变）。
`chaos/code/runner/metrics.py`

---

## 2. `ChaosMCPClient.delete` 位置参数撞车 → CRD 泄漏 + tproxy 残留

**症状**：stop condition 触发后，清理路径直接抛
`TypeError: delete() got multiple values for argument 'chaos_type'`。
后果连锁：HTTPChaos CRD 留在集群里继续生效 → 两个目标 Pod 进入 **CrashLoopBackOff**
→ 容器重启清不掉 → 只能删 Pod 重建。

**根因**：`delete(chaos_type, name, namespace)` 的**第一个位置参数是 `chaos_type` 而不是 `name`**。
调用方按位置传了实验名，又用关键字传了 `chaos_type`，于是同一个参数被赋值两次。

**为什么静态手段发现不了**：这行代码在**只有异常路径才走到**的分支里。
5 轮注入之前，历史上 72 个实验**全部 passed、零失败**——异常路径一次都没执行过。
单测也没覆盖它，因为没人会为「清理失败」写用例。

**额外发现（Chaos Mesh 文档没写）**：强删一个**仍在生效**的 CRD，会把 tproxy 拦截
残留在目标 Pod 的**网络命名空间**里。netns 属于 Pod sandbox 而不是容器，
所以容器重启清不掉——只能删 Pod。

**修法**：全部改关键字传参。`chaos/code/runner/runner.py`

---

## 3. 护栏挂在注入目标上 → 实验必然自杀

**症状**：两轮注入连续死在自己的护栏上，零判定。
- 第一轮：护栏 `success_rate < 40%` 挂在注入目标，T+71s 掉到 27.4% 熔断
- 第二轮：把地板降到 `< 2%`，仍在 T+92s 掉到 **0.0%** 时触发

**根因**：边验证实验里**注入目标本来就该失败**——abort 掉 B 的流量，B 的成功率必然趋零。
基于 B 的护栏必然在 Phase 5 之前触发，`_verify_edges` 永远跑不到。

**关键认识**：
> 对全量 abort 实验，**注入目标的成功率不是护栏信号，它就是处理本身**。
> 要防的是**附带损害**（调用方崩了），不是预期效果。

**为什么静态手段发现不了**：护栏配置在语法上完全正确，阈值也「看起来合理」。
错的是把护栏对象搞反了，而这只有在实验真的跑起来、真的被自己熔断时才显形。

**修法**：`StopCondition` 加 `target` 字段（`injection` / `any_observer` / `observer:<svc>`），
默认 `injection` 保持既有行为。边验证实验改为挂 `any_observer` 且阈值 `< 30%`——
**刻意低于 confirm 判定线 20pp**，目标是让边被判 confirmed，而不是一有退化就熔断
（那只会得到 inconclusive）。`chaos/code/runner/experiment.py` + `runner.py`

---

## 4. `abort` 故障下成功率是盲的

**症状**：注入明明生效，注入目标成功率却**全程 100%**。

**实测数字**：成功率 100% 不动，而请求量 **2240 → 56（-97%）**。

**根因**：`http_chaos action: abort` 直接短路连接，被中断的请求**根本不产生 response 行**，
而 SLI 查询带 `response_duration > 0` 过滤。于是「被杀掉的请求」在成功率的分母里也不存在。

**为什么静态手段发现不了**：要同时知道三件事才能推出来——abort 的语义（不返回响应）、
DeepFlow 表的写入时机（有响应才落行）、SLI 查询的过滤条件。三者分散在
Chaos Mesh 文档、DeepFlow 表结构、本仓库 SQL 里，没有任何一处能单独暴露它。

**危害**：只看成功率会得出「注入没生效」的结论，然后把一条**真实的边判成不存在**。
这比不验证更有害——不验证只是没有结论，误判是产出了错误结论。

**修法**：引入**合成退化率** = max(成功率下降, 吞吐塌陷)。
`chaos/code/runner/result.py` 的 `observer_throughput_drop_pct()` /
`observer_effective_degradation()`

**这个修法就是最终判定成立的原因**：08:47 那轮，`petsite` 成功率只掉 **3.51pp**
（远低于 20pp 的 confirm 线），吞吐塌陷 **74.92%**。只看成功率 → 这条真实的边被判 refuted。

---

## 5. `observer_min_success_rate` 首个采样点不初始化

**症状**：观测方全程健康时，退化率返回 `None` → `usable=False` → 拿不到任何判定。

**根因**：原实现是 `cur = get(service, 100.0)` 再 `if rate < cur`。
观测方全程保持 100.0 时，`100.0 < 100.0` 恒假，min **永远不被写入**。

**为什么静态手段发现不了**：这是个「默认值 + 严格小于」的经典组合，读起来完全自然。
只有在**观测方真的没有退化**这个具体场景下才暴露——而这恰恰是最常见的场景之一。

**语义澄清**：健康的观测方应该得到 **0.0 退化**，而不是「判不了」。
「判不了」的语义要留给**完全没有采样点**的情况。混在一起就分不清
「测了，没退化」和「没测到」。

**修法**：`cur is None or rate < cur`。`chaos/code/runner/result.py`

---

## 6. Phase 4 正常完成路径从不删 CRD

**症状**：一个 **PASSED** 的实验结束后，`kubectl get httpchaos` 仍有 1 条。

**根因**：`_phase4_recover` 的 docstring 写着「Chaos Mesh duration 字段负责到期删除 CR，
故障自动消除」——**这个假设是错的**。duration 到期只让**故障不再生效**，
**CRD 对象仍然存在**。而 runner 只在熔断/异常路径删 CRD，正常完成路径从不删。

**为什么静态手段发现不了**：代码逻辑自洽，注释也言之成理。
错的是注释里那句关于**外部系统行为**的断言，只有对着活集群 `kubectl get` 才能证否。

**修法**：Phase 4 的第一职责改为**显式删除 CRD**，不依赖「到期自动清理」。
删除后把 `result.chaos_experiment_name` 置空，避免 `emergency_cleanup` 重复删。
并把被证否的假设写进 docstring，而不是删掉了事。`chaos/code/runner/runner.py`

---

## 7. 候选边查询用 K8s 名匹配图谱规范名（本轮新查出）

**症状**：实验 PASSED、观测方证据齐全、`usable=True`，但边验证写回 **0/2**，
日志只说「图谱中无 `petsite -> search-service` 的候选边，跳过」。
而 `petsite -[Calls]-> petsearch` 与 `petlistadoptions -[Calls]-> petsearch`
**两条边明明都在图里**。

**根因**：`candidate_edges()` 直接拿实验里的 **K8s 服务名**去匹配图谱节点名，
而图谱里 Microservice 用的是**规范名**：

| 实验里（K8s 名） | 图谱里（规范名） |
|---|---|
| `search-service` | `petsearch` |
| `list-adoptions` | `petlistadoptions` |
| `petsite` | `petsite`（只有这个碰巧一致） |

更糟的是查询 `g.V().has('name','search-service')` **不带标签**，
于是匹到了**同名的 Deployment 与 K8sService 节点**（本图 12 组名字跨标签重复，
这是其中一组）。那两个节点没有 `Calls` 入边，于是候选边为空。

**这与 183 条错源边是同一个根因家族**：按名字匹配、不带标签、而名字跨标签重复。

**为什么静态手段发现不了**：代码没有任何错误处理缺失，查询语法正确，
「无候选边」还被记成 `logger.info` 的正常分支。要发现它必须**同时**知道
图谱里叫什么、实验里叫什么，并注意到两者不是一回事。

**修法**：
1. 走 `shared/service_registry.ServiceRegistry.resolve()` 做 K8s 名 → 规范名解析，
   解析表唯一来源是 `profiles/petsite.yaml`（与 ETL 的 `service_mappings.json` 同源），
   **不另立一份硬编码映射**
2. 观测方名字也必须做同样解析（`list-adoptions` → `petlistadoptions`）
3. **「无候选边」从 `info` 升级为 `warning`**，并打印候选边实际的源端点列表——
   空结果可能是「真没有边」也可能是「名字对不上」，两者在日志里长得一样时，
   后者会被当成前者放过

`chaos/code/runner/edge_verification.py` + `runner.py`

---

## 8. 采集失败伪装成「零流量」污染谷值（本轮新查出，最危险的一条）

**症状**：修完 #7 之后终于有了候选边，但证据长这样：

```
petsite        基线请求=322  谷值请求=0  吞吐塌陷=100.00%  合成=100.00
list-adoptions 基线请求=88   谷值请求=0  吞吐塌陷=100.00%  合成=100.00
```

两个观测方**同时**吞吐塌陷 100%——而 `petsite` 是被负载机持续打流量的，
它的请求量不可能真的归零。

**根因**：`metrics.collect()` 在 ClickHouse 查询异常时 fallback 成
`success_rate=100.0 / total_requests=0`。`observer_min_requests` 取各采样点最小值，
**一次查询抖动就把谷值压成 0**。

**为什么这条最危险**：它不会让实验失败，而是产出一个**错误的 confirmed**。
100pp 的合成退化率远超 20pp 的 confirm 线，两条边都会被判「依赖成立、置信度极高」。
一个静默产出高置信度错误结论的系统，比一个报错的系统坏得多。

**为什么静态手段发现不了**：fallback 返回 100% 成功是**刻意设计**（不误触 guardrail），
在成功率通道上是对的。错在吞吐通道是后加的，没有跟上同一条纪律。
读代码时两处都「看起来合理」。

**深层原因**：`(success_rate=100, total_requests=0)` 这个三义组合——
「查询失败」「真的零流量」「真的健康」——在数据结构上无法区分。
成功率通道当初专门防过（min 只在有值时更新、判定前先查请求量下限），
吞吐通道没有。**这正是「不变量要写成代码而不是写在注释里」的又一例。**

**修法**：给 `MetricsSnapshot` 加显式 `ok: bool`。
- 查询失败 → `ok=False`，**只入采样列表（留痕）、不参与任何 min 统计**
- `collect_steady` 只用 `ok=True` 的采样点算均值，全失败则整个基线 `ok=False`
- `_verify_edges` 的注入期请求量只在 `ok=True` 的采样点里取

`chaos/code/runner/experiment.py` + `metrics.py` + `result.py` + `runner.py`
守门用例 `tests/test_37_observer_metrics.py::test_o90/o91`

**修完后的同一条边**：`petsite` 基线 658 → 谷值 **165**（不再是 0），吞吐塌陷 **74.92%**。
数字变得可信，判定才有意义。

---

## 9. `source` 词表声明了但没有门禁

**症状**：契约 `sources:` 声明 9 个合法取值，活图谱实测出现 **13 种**，
未声明的共 **1240 条**：`eks-etl` 1228 条边、`deepflow` 8 个节点、
`aws-etl-static` 3 条、`manual` 1 条。

**根因**：`SOURCES` 从引入起就在契约里，也被 `graph_contract.py` **re-export**，
但**没有任何一处检查读它**。

**对照实验（这条是整份文档里最有说服力的一组数字）**：
同一份 YAML 里**被** `assert_edge_type` 检查的节点与边类型 —— **零漂移**。
没被检查的 `source` —— 漂了 1240 条。
**漂移量与「有没有门禁」完全相关，与「声明得好不好」无关。**

**连带查出一个更严重的缺陷**：`upsert_edge` **会丢弃调用方传入的 `source`**。
写一次语义被实现成「硬编码 `aws-etl` + 跳过调用方的同名属性」，于是
`handler.py` 里 **13 处 `eks-etl` + 1 处 `aws-etl-static` 全部静默失效**。

**为什么活图谱看不出来**：`coalesce` 保护了存量边，1228 条 `eks-etl` 还在那儿好好的。
这个 bug **只影响此后新建的边**——不会立刻暴露，只会让 provenance 缓慢腐坏。
**一个「看数据看不出来、要读代码才发现」的缺陷。**

**修法**：
1. `assert_source()` + `is_declared_source()`，走既有 `GRAPH_CONTRACT_MODE`
2. 契约补声明 `eks-etl` / `aws-etl-static`（**刻意的语义区分**：
   K8s API vs AWS 控制面、静态声明 vs 运行时观测）
3. `deepflow` 判为 `deepflow-etl` 的**同义漂移**，收敛写入侧而**不**扩词表——
   否则按源分派的逻辑（`edge_verification._OBSERVER_MARKERS`）会漏判；
   读取侧同时兼容 `within('deepflow','deepflow-etl')`，
   只改写入侧会让存量清理查询从此匹配不到任何节点（**修一个缺陷引入一个静默失效**）
4. `upsert_edge` 改为「写一次 = 已存在的边不覆盖，取什么值 = 首写者说了算」
5. deepflow/xray/cfn 各自手拼 Gremlin、没有共同收口点，改用**源码静态扫描**守门

`profiles/graph_contract.yaml` + `graph_contract.py` + `etl_aws/neptune_client.py`
+ `etl_deepflow/`，守门用例 `tests/test_38_source_vocabulary.py`（8 个）

---

## 10. Lambda 层缺 `requests`，`etl_xray` 已死 2 天（本轮新查出）

**症状**：`etl_xray` 每次调用都 `ModuleNotFoundError: No module named 'requests'`。
查日志：**从 2026-08-29 08:00 起一直在失败**，到发现时已经 **2 天**。

**根因**：`neptune_client_base._get_http_session()` 懒加载 `requests`。
`etl_aws` / `etl_deepflow` / `etl_cfn` 各自 vendored 了 `requests`（包 600KB–1.4MB），
**只有 `etl_xray` 不带**（包 16KB），它靠层提供——而层 `:6` 里从来没有 `requests`。

**为什么静态手段发现不了**：`import requests` 在本地开发机、在其他三个函数里都能成功。
只有 xray 这一个函数、只在 Lambda 运行时才失败。而且它是**懒加载**，
所以连冷启动都不报错，要等第一次真正查 Neptune。

**最值得记的不是这个 bug，而是它活了 2 天**：
> **一整个观测源静默死亡，没有任何机制发现。**
> 而这个项目的命题恰恰是「你怎么知道图上那条边是真的」——
> 如果一个数据源可以死两天没人知道，那么基于它的所有「未观测到」判定都是假阴性。
> 这直接对应 X-Ray 引入前 85% 假阴性那个教训的**运维版本**。

**修法**：把共享依赖放进层（层本来就是干这个的），
发布 `:8` = 5 个 `.py` + `requests`/`urllib3`/`certifi`/`idna`/`charset_normalizer`。
四个函数统一指向 `:8`。

**顺带记一次我自己的失误**：我先发布的层 `:7` 只打了 5 个 `.py`，
把层原有内容当成了「就是这 5 个文件」——虽然 `:6` 里确实也没有 `requests`
（所以 xray 不是被我打断的），但 `:7` 同样没修好它。
**教训：替换一个共享产物之前，先把旧产物下载下来对比内容，不要凭源码目录推断。**

---

---

## 11. Phase 5 只看 SLI，实验报 PASSED 却留下坏 Pod（10:10 查出）

**症状**：2026-08-31 两轮 `abort` 注入，SLI 报 100%、稳态检查全过、实验判 **PASSED**，
但被注入的 Pod 都进了重启循环、持续 1/2 Ready，只能人工删 Pod 重建。

**根因**：Phase 5 只查 SLI。而 SLI 由 **HPA 新拉的干净 Pod** 撑着看起来正常 ——
**服务健康 ≠ 实验没造成损伤**。一个报 PASSED 却留下坏 Pod 的实验，
会让下一轮实验的基线带着污染开始。

**为什么静态手段发现不了**：Phase 5 的逻辑完全自洽，稳态检查也确实通过了。
错的是**判据的覆盖面**：它检查了「服务还好吗」，没检查「我干了什么」。

**修法里最要紧的一个判断：主判据是重启差值，不是 readiness。**
实测 Phase 4 报 `2/2 running`（08:34:07）之后**还要 2.5 分钟** Pod 才退回 1/2，
所以任何点时刻的 readiness 都可能刚好看不见损伤。而 `restartCount` 在注入期间
就已递增，Phase 5 时必然可见。因此判据是 **Phase 0 基线 → Phase 5 的差值**。

**首次真实生效即验证了这个判断**（三轮注入，各自不同形态）：

| 实验 | readiness | 重启差值 | 旧代码判定 | 新代码判定 |
|---|---|---|---|---|
| list-adoptions | 2/2 → **0/2** | 0 → **12** | PASSED | **FAILED** |
| pethistory | **2/2 → 2/2 ✅** | 0 → **2** | **PASSED** | **FAILED** |
| pay-for-adoption | 2/2 → 2/2 | 0 → **12** | PASSED | **FAILED** |

> **pethistory 那一行是决定性的**：readiness 全程干净，只有重启差值抓到了损伤。
> 如果按最初想的「Phase 5 加个 readiness 检查」去做，这一轮仍会报假 PASSED。

缺基线时**不判 FAILED 而是显式留痕**：`check_pods` 失败返回 `restarts=None`，
拿 None 当 0 会让判据静默通过，拿它当损伤又会把「没测到」误报成「打伤了」——
两者都不对，所以显式区分第三种情况。

`chaos/code/runner/chaos_mcp.py`（`check_pods` 返回 restarts）+ `runner.py`
（Phase 0 存基线、`_check_target_pod_health`）+ `report.py`（报告呈现处置命令），
守门用例 `tests/test_39_pod_damage_gate.py` 7 个。

**FIS 后端刻意跳过该判据** —— 实测 FIS 路径不产生 tproxy 残留、不打伤 Pod。

---

## 12. Pod 标签 ≠ Deployment 名：第三套命名空间（10:24 查出）

**症状**：`verify-edges-into-pethistory.yaml` 写 `service: pethistory-deployment`，
preflight 报「服务 pethistory-deployment 无 Running Pods」，而 Pod 明明是 Running。

**根因**：实际 Pod 标签是 `app=pethistory`。`pethistory-deployment` 是
**Deployment 名**，也是 `service_mappings.json` 里 `neptune_to_k8s` 的取值 ——
两者不是一回事。

**为什么静态手段发现不了**：`service_mappings.json` 里确实写着
`pethistory → pethistory-deployment`，看着就该用它。要发现必须真的
`kubectl get pods --show-labels` 对一眼。

**这是第 #7 条那个命名问题的第三个面**。到此为止已经数出**四套**命名空间：

| 用途 | pethistory | petsearch |
|---|---|---|
| kubectl Pod 标签 `app=` | `pethistory` | `search-service` |
| Deployment 名 / `neptune_to_k8s` | `pethistory-deployment` | `search-service` |
| DeepFlow `request_domain` | `pethistory` | `search-service` |
| 图谱 `Microservice.name` | `pethistory` | `petsearch` |

**四套里没有任何两套是恒等的**，只是在某些服务上偶然重合 ——
这正是「身份不唯一」这类缺陷反复出现的结构性原因。

---

## 13. 注入目标 SLI 用 kubectl 名查 DeepFlow → 假 100% 通过稳态门（10:24 查出）

**症状**：`metrics.collect('pethistory-deployment')` 返回 **0 请求**，
而 `collect('pethistory')` 返回 26 请求。

**根因**：`metrics.collect` 用 `request_domain LIKE '%svc%'`，传进去的是
`exp.target_service`（kubectl 口径），而 DeepFlow 认的是服务名。
0 请求会走 fallback 返回 `success_rate=100.0`，于是稳态门 `>= 95%`
被一个**假的 100%** 通过。

**这与 #8 是同一个 fallback 的两种伤法**：#8 是 fallback 污染了谷值统计，
这里是 fallback 伪造了一个「健康」。同一个 `(100%, 0 requests)` 三义组合。

**修法**：`_target_metrics_name()` 用 T-214e 引入的**同一张别名表**解析，
三处注入目标的 metrics 调用（Phase 1 基线 / Phase 3 采样 / Phase 5 恢复）统一走它。
观测方那两处不动 —— 它们用 `obs.service`，已由 `_verify_edges` 单独解析。

**顺带修掉一个我自己引入的问题**：解析函数每次调用都打日志，
Phase 3 每 10s 采样一次会刷 18 行同样的解析日志把真信号淹掉，改为按名字去重打一次。

---

## 14. PolicyGuard R002 命名空间白名单对 FIS 目标语义不适用（11:00 查出）

**症状**：Aurora FIS 实验写 `namespace: rds`，被 PolicyGuard R002（Namespace Whitelist）
DENY：白名单是 `[petsite-staging, petsite-canary, chaos-sandbox, petadoptions]`。

**根因**：R002 校验的是 **K8s 命名空间**，而 FIS 打的是 AWS 资源、根本没有命名空间。

**这个 DENY 顺带证明了一件事**：仓库原有的
`experiments/fis/rds/fis-aurora-reboot-petlistadoptions.yaml` 写的也是
`namespace: rds` —— **据此可判断那份模板从未真正执行过**。
一份存在了 4 个月、看起来完整的实验规格，实际上一次都没跑起来。

**本轮处置**：把 namespace 填成**受影响的应用命名空间** `petadoptions`
（既在白名单里，也确实是爆炸半径所在），**刻意不放宽白名单** —— 后者才是危险做法。

**待做**（T-292）：给 AWS 资源类目标定义清楚 namespace 字段的语义并写进规格文档，
R002 增加一条「backend=fis 时校验受影响命名空间」的显式规则，别靠约定。

---

## 15. 一个被推翻的自己的判断：78 条边并不阻塞在 T-230

09:00 那版文档和任务卡都写着：78 条「被依赖方是 AWS 托管资源」的边
**阻塞在 T-230（自建 SSM Automation 改安全组做单边隔离）**。

**这个判断是错的。** 实测账号内 FIS 原生就有需要的动作：

| FIS action | 可验证 | 本轮状态 |
|---|---|---|
| `aws:rds:reboot-db-instances` | RDSCluster 14 / RDSInstance 3 | ✅ 已用，4/4 写回 |
| `aws:network:disrupt-vpc-endpoint` | AWSServiceEndpoint 11 | 下一步 |
| `aws:eks:pod-network-blackhole-port` | **T-230 想要的手术刀式单边隔离** | FIS 原生有 |
| `aws:lambda:invocation-error` | LambdaFunction 8 | 需 Lambda 扩展层 |

> 这条和「ARH 第 5 条 ETL」那次一样：**先查清官方工具已有什么，再决定要不要自造。**
> T-230 的价值需要重新评估 —— FIS 已提供同等甚至更好的能力。

**同时也修正一处过度悲观**：`ECRRepository` 12 条判定为**不可运行时验证**
（镜像拉取只在 Pod 启动时发生，属启动期依赖），如实记为「不适用」
而不是继续挂在「待验证」里虚增分母。

---

---

## 16. 吞吐塌陷取 min 比单点基线，抖动伪造塌陷（15:56 查出，最隐蔽的一条）

**症状**：用 FIS 断子网到 S3 的连通性验证 `petsearch -> s3`，实验报
「基线请求 2407 → 谷值 819，吞吐塌陷 **65.97%**」，判 **confirmed**。

**判别性检验推翻了它**。我拿一个无关服务做对照，直接查 ClickHouse 同窗口聚合：

| 服务 | 注入前 3 分钟 | 注入期 3 分钟 | 变化 |
|---|--:|--:|---|
| `search-service`（被判 confirmed） | 7796 | **11630** | **涨 49%** |
| `list-adoptions`（无 s3 边） | 464 | 556 | 涨 20% |
| `petsite`（无 s3 边） | 1464 | 1746 | 涨 19% |
| `pay-for-adoption`（无 s3 边） | 496 | 588 | 涨 19% |

**整体流量根本没降，反而涨了。**

**根因**：`observer_min_requests` 拿「注入期 18 个采样的 **min**」去比
「基线的**单个**采样」—— 两侧不同量纲。突发流量下**一个**低谷采样就能
伪造出大幅塌陷。

**为什么静态手段发现不了**：min 和基线各自都是合法的测量值，代码没有任何错误
处理缺失。要发现它必须**跳出这套指标去查原始聚合**，而且要有一个「不该受影响
的对照服务」才能定性 —— 这正是「对照被注入过的旧 Pod 与新起的干净 Pod」
那条方法论的另一种应用。

**危害等级**：这是本文档里最隐蔽的一条。它不报错、不失败，产出一个
**高置信度的错误 confirmed**，而且看起来完全合理（65.97% 的塌陷数字很有说服力）。

**修法**：改用**中位数**。对单点低谷不敏感，真塌陷时多数采样都低仍能检出。
`chaos/code/runner/result.py::observer_throughput_drop_pct`

**修后同一条边的结果**：吞吐塌陷 65.97% → **1.21%** → 判定从 confirmed 翻成
**refuted**。这是本项目**第一条被证伪的边**。

---

## 17. 吞吐通道无法归因：三种成因分不清（15:52 查出）

**症状**：断 DynamoDB 后 `pay-for-adoption -> dynamodb` 判 confirmed，
但它的**成功率退化是 0.00pp**，全部证据来自吞吐塌陷。

**根因**：两条通道的证据强度**不对等**，而合成退化率 `max(成功率降, 吞吐降)`
把它们当等价证据：

| 通道 | 语义 | 归因 |
|---|---|---|
| 成功率下降 | 观测方**自己**返回了失败 | 明确 |
| 吞吐塌陷 | 观测方的请求量少了 | **三种成因分不清** |

吞吐下降的三种成因：① 它自己失败到不产生 response 行（真依赖）；
② **它的上游不再调它**（传导，不是它自己的依赖）；③ 测量管道本身受影响。

**实测踩到 ②**：`pay-for-adoption` 的入流量来自 petsite，而 petsite 因
petsearch 失败已不再提交领养 —— **「它不再被调用」被当成了「它依赖 DynamoDB」**。

**为什么不能简单否掉吞吐通道**：缺陷 #4 的结论仍然成立 —— `abort` 类故障
不产生 response 行，成功率通道**结构性**为盲，那时吞吐是唯一可见信号。
一刀砍掉会让所有 abort 实验都判不了。

**修法**：给判定加**证据通道标注**（`success_rate` / `throughput_only` /
`both` / `none`），纯吞吐证据判 confirmed 的门槛从 20% 抬到 **60%**，
达不到判 inconclusive。方向与「零流量不判 refuted」一致：
**宁可判不了，不可判错**。通道值随判定写回边上（`verify_evidence_channel`），
所以事后可以按证据强度审计每一条判定。

`infra/lambda/shared/python/graph_confidence.py::classify_intervention`
+ `result.py::observer_evidence_channel`，守门用例
`tests/test_40_evidence_channel.py` 9 个。

**按 DoD-10 撤销了两条建立在错误证据上的判定**（`payforadoption -> dynamodb`、
`petsearch -> s3` 置回 untested 并写明原因），用修好的判据重跑：
前者仍 confirmed 但标注 `throughput_only`（中位数下塌陷 96.88%，过 60% 高门槛），
后者翻成 **refuted**。

---

## 18. `fis_vpc_endpoint_disrupt` 目录声明的目标类型是错的（15:38 查出）

`fault_catalog.yaml` 里：

```yaml
- type: fis_vpc_endpoint_disrupt
  fis_action_id: "aws:network:disrupt-vpc-endpoint"
  requires: [subnet_arn]        # ← 错
```

而 AWS 侧实际是：

```
aws fis get-action --id aws:network:disrupt-vpc-endpoint
  targets: VPCEndpoints -> aws:ec2:vpc-endpoint
```

**声明与 AWS 侧不一致，所以这个故障类型从未被真正执行过** —— 与 #14
（`namespace: rds` 那份 FIS 模板从未跑过）是同一种「存在但从未运行」的债。

**推广出的判据**：`fault_catalog.yaml` 里 60 条故障声明中，凡是没有对应
validation-results 记录的，都应假定「未验证」而不是「可用」。
应加一条守门测试：拿 `aws fis get-action` 的真实 targets 与目录里的
`requires` 对账。

---

## 19. 「78 条阻塞在 T-230」之后的第二次收窄：FIS 也不是万能的

09:00 那版说 78 条阻塞在 T-230（自建 SSM 改安全组）—— 被 FIS 推翻（见 #15）。
但 11:12 那版接着写「AWSServiceEndpoint 11 条可用 `disrupt-vpc-endpoint`」——
**这一条也是错的**，实测该动作对这批边无从施加（PetSite VPC 里没有对应端点）。

真实可行性收窄成：

| 目标 | 边数 | 可行动作 | 状态 |
|---|--:|---|---|
| RDSCluster / RDSInstance | 17 | `aws:rds:reboot-db-instances` | ✅ 4 条已做 |
| DynamoDB（含端点级） | 13 | `aws:network:disrupt-connectivity scope=dynamodb` | ✅ 2 条已做 |
| S3（含端点级） | 7 | `scope=s3` | ✅ 1 条已做（refuted） |
| **ssm / sts / xray** | **7** | **FIS 无动作可用** | 见 T-296，需 Chaos Mesh |
| LambdaFunction | 8 | `aws:lambda:invocation-error`（需扩展层） | 未做 |
| ECRRepository | 12 | 不适用（镜像拉取只在启动期） | 不适用 |

> **教训的第二层**：「查清官方工具有什么」只是第一步，还要**查清它在这个环境里
> 有没有目标可打**。一个动作存在于 `list-actions` 里，不等于它在你的 VPC 里
> 能选出资源。两次判断失误都是在这一步上省了功夫。

---

---

## 20. 无法识别「注入没生效」，据此判 refuted（16:30 查出，本轮最严重）

**症状**：`petsearch -[AccessesData]-> s3` 被判 **refuted**（退化 1.21%）。

**判别性检验推翻了它**。拿 X-Ray **严格按 FIS 的故障窗口**复核：

| 故障窗口（FIS 实测起止） | PetSearch → S3 |
|---|---|
| 16:03:25 – 16:06:27 | ok=**10** err=0 fault=0 |
| 15:53:43 – 15:56:44 | ok=**13** err=0 fault=0 |

注入期间 S3 调用**全部成功**。`disrupt-connectivity scope=s3` 的 NACL
根本没有切断这条路径。

而这条边的证据是**两个独立源的硬数据**：
`xray_call_count=17190`（24h，0 error 0 fault）、
`nfm_flow_count=50` / `nfm_bytes=1,897,669` / `nfm_categories=AMAZON_S3`。

**根因**：判据只问「观测方退化了吗」，没问「我真的打断了吗」。观测方没退化有两种
完全不同的成因，而原实现把它们都判成 refuted：
  ① 注入生效了，但影响没传导到调用方 → 这条边可疑（**真** refuted）
  ② **注入根本没生效** → 什么都没验证（**凭空证伪**）

**为什么这条最严重**：前面的缺陷（#16/#17）产出的是错误的 confirmed —— 多一条
不存在的依赖，代价是影响面分析偏保守。这一条产出错误的 **refuted** ——
按 DoD-10 累计两次 refuted 就会**删掉一条真实的依赖边**，代价是影响面分析
出现盲区，而盲区比冗余危险得多。

**修法**：`classify_intervention` 加 `injection_confirmed`，只有 `True` 才允许
判 refuted，`None`（未知）与 `False` 都判 inconclusive。runner 侧
`_injection_took_effect()` 从**注入目标自身**的 SLI 推导，门槛刻意低到 5% ——
这里回答的是「有没有作用到目标」这个是非问题，不是「影响有多大」。
用 confirm 那条 20% 的线会把「生效但影响小」误判成「没生效」，
反而放宽了 refuted 的条件，方向错了。

**刻意不做的事**：不用「observer 有退化」反推注入生效 —— 那是用结论证明前提。

**一句话原则**：
> **证伪比确证需要更强的前提。** 确证只需看到影响传导；
> 证伪需要先证明「我真的打断了它」。

这是「宁可判不了，不可判错」在 refuted 一侧的落地 ——
此前该原则只覆盖了零流量那一种情形。

---

## 21. FIS 目录 37 条里 5 条从来不可执行（16:20 查出）

新增的对账测试（`tests/test_41_fis_catalog_reconcile.py`）一次查出 5 条
**声明存在但从来跑不起来**的故障类型，占 FIS 目录的 **14%**：

| 故障类型 | 问题 | 处置 |
|---|---|---|
| `fis_ec2_network_disrupt` | action id 不存在；且 description 声称的「实例级网络隔离」这个能力**本身不存在**（真 action 目标是子网）；改对后与 `fis_network_disrupt` 完全重复 | **整条删除** |
| `fis_elasticache_az_power` | `interrupt-cluster-az-power` 不存在，真名 `replicationgroup-interrupt-az-power` | 改名 |
| `fis_vpc_endpoint_disrupt` | 构建 `aws:ec2:subnet`，action 要 `aws:ec2:vpc-endpoint` | 修 builder + requires |
| `fis_ec2_spot_interruption` | 正确分支被更早的 `startswith("fis_ec2")` 宽分支截住、**永远走不到** | 提前该分支，删 3 条不可达重复分支 |
| `fis_eks_inject_k8s_custom` | fis_backend **没有任何分支**处理它（只有 `fis_eks_pod` 前缀），调用即抛 ValueError | 补 `aws:eks:cluster` 目标 |

**测试的设计要点**：比「fis_backend **实际构建**的目标类型」而不是目录的
`requires`。后者是输入契约，与 AWS 目标类型本来就可以不同 ——
`fis_rds_reboot` 的 `requires` 是 `cluster_arn`，而 backend 内部转成 writer
实例 ARN 建 `aws:rds:db`，AWS 接受且实跑成功。拿 `requires` 去比会产生假警报，
第一版就是这么误报的。

**推广判据**：目录里 60 条故障声明，凡是没有对应 validation-results 记录的，
都应假定「未验证」而不是「可用」。

---

## 22. `target_service` 兼任两职，边切断拓扑无法表达（16:56 查出）

**症状**：验证 `petsite -> ssm` 时把 `target.service` 写成 `ssm`（图谱节点名），
preflight 直接报「服务 ssm 无 Running Pods（检查 label app=ssm）」。

**根因**：Chaos Mesh 路径下 `target_service` 同时承担两个不相干的职责：
  · kubectl 的 label selector —— 选**哪些 Pod** 注入
  · 图谱节点名 —— `candidate_edges` 查**谁的**入边

前两种注入拓扑下两者一致，所以一直没暴露：

| 拓扑 | 注入在哪 | 观测谁 | 两个名字 |
|---|---|---|---|
| 在 B 注入、观测 A | 被依赖方 B | 调用方 A | 一致 |
| AWS 资源级中断 | 托管资源 | 调用方 | FIS 路径本来就解耦 |
| **边切断（新）** | **调用方 A 的出向** | **调用方 A** | **必然不同** |

**修法**：`Experiment` 加 `target_graph_node`（默认等于 `target_service`），
只在两者必然不同时才写。顺带修掉一个日志缺陷：判定日志用 `target_service`
拼被依赖方，边切断拓扑下会打出 `petsite -[AccessesData]-> petsite` 这种
**自环假象** —— 判定其实正确落在 `petsite -> ssm` 上，但读日志的人会以为写错了边。

---

## 23. 「PetSite 只在启动时读一次 SSM」这条既有认知是错的（17:04 更正）

跨会话记忆里一直记着「SSM 只在启动时读一次，所以 v5–v10 那 6 次参数翻转无法自愈」。
据此我在 `verify-edges-into-ssm.yaml` 的文件头**预判本实验会判 inconclusive**。

**实测推翻了它**。X-Ray 正常窗口（16:40–16:55，无注入）：

```
PetSite -> SSM        total=854  ok=854     (15 分钟)
PetSite 自身请求      total=854              (15 分钟)
```

**1:1 —— 每个请求都调一次 SSM。**

这既解释了切断 SSM 后 petsite 吞吐 435 → 0 的完全中断（判定 confirmed 成立），
也是一个此前没人注意到的**真实架构问题**：站点把 SSM Parameter Store
变成了每请求的硬依赖，有限流风险（GetParameter 有 TPS 上限）、
有延迟成本、且让 SSM 成为站点可用性的单点。

**方法论意义**：这是本轮第二次「先验机制再信结论」救回一个判断 ——
第一次（#16）救回的是一个错误的 confirmed，这次验证的是一个**正确**的 confirmed。
两次的动作完全一样：**拿一个独立数据源，严格按注入窗口对齐，去查机制是否真的发生。**

---

---

## 24. 契约三个元字段全部零消费方（2026-09-04 审计）

起因是核一遍 33 个节点类型的 `identity` 与 `scope_note` 是否自洽。
**身份键本身全绿**——活图谱里 33/33 类型的身份键 100% 存在且唯一，没有重演
EC2Instance 那种 4/14 重复实体。问题全在围绕身份键的三个元字段上：

| 元字段 | 状态 | 后果 |
|---|---|---|
| `immutable` | 唯一读取函数 `identity_is_immutable` **全仓库零调用方** | 见下 |
| `scope_note` | 纯注释，无任何读取方 | 已知缺陷无法被程序取用 |
| `preferred` | 声明了「将来切到 arn」，**无任何机制检查前提何时满足** | TargetGroup 迁移被永久阻塞 |

### `identity_is_immutable` 的问题不是「写错了」，是一个名字混淆了两个问题

我在审计的第一轮把它判成「语义是反的」，**这个判断过重了**。函数体
`return spec.get('immutable') in (True, 'lifetime')` 本身是对的——对某个问题而言：

```
Q1 「这个键可以安全 merge 吗」      -> 'lifetime' 是 **可以**
Q2 「这个键跨对象重建还成立吗」      -> 'lifetime' 是 **不成立**
```

函数名写的是 Q2（immutable 的字面意思），函数体答的是 Q1。因为它**零调用方**，
两种读法都从未被验证过，所以哪个语义"对"取决于第一个调用者以为自己在问什么。

为什么 Q1 上 `lifetime` 合格：可变身份键的真实危害是「同一实体裂成两份」
（EC2Instance 的 name 取自可变 Name 标签，而机器一直是同一台，实测 4/14 重复）。
Pod 换名产生的是**新实体**，不构成这种危害。

修法是拆成 `identity_is_stable_for_merge`（Q1）与
`identity_survives_recreation`（Q2），并由 `test_42` 的 m04 锁住
「两者必须在且仅在 lifetime 类型上分歧」——如果永远一致，拆分就是纯噪声。

### `preferred` 的代价是实测出来的

TargetGroup 的 note（2026-08-30）写着「18 个现存节点**全部**没有 arn 属性，
待存量都带上 arn 后再切」。2026-09-04 实况：

- **14/18 已有 arn**——note 过期半个月无人发现，因为没有任何读取方
- 那个条件**在结构上不可满足**：剩下 4 个节点，3 个（`nginx-tg-1/2/3`）在
  AWS 侧已删除、1 个（`openclaw-tg-v2`）被 `SKIP_TG_PREFIXES` 刻意排除采集，
  ETL 永远不会再碰它们、永远补不上 arn

补上自动检查后立刻浮出一个没人知道的事实：**8 个 `preferred` 类型里 7 个早已
回填完整、当时就能切**（DynamoDBTable / ECRRepository / LambdaFunction /
LoadBalancer / SNSTopic / SQSQueue / StepFunction）。

### 判据设计：必须区分 PENDING 与 RESIDUE

`test_42::m08` 对活图谱核验，三种状态只有中间那种判失败：

| 状态 | 判据 | 含义 | 处置 |
|---|---|---|---|
| PENDING | 0 个节点有该属性 | 写入方还没开始写 | 等待合理 |
| **RESIDUE** | 0 < k < N 有 | **写入方在写，缺的是刷不到的残留** | **必须回收** |
| UNLOCKED | N 个全有且唯一 | 可以切了 | 报出来 |

两者都表现为「条件未满足」，但前者该等、后者该动手。TargetGroup 当时是
14/18（RESIDUE），note 却用 PENDING 的措辞描述它（「全部没有」），
据此得出的「等回填完成再切」是个永远不会到来的前提。

### 一条必须绕开的陷阱

`conftest.py` 把 `sys.modules['neptune_client_base']` 全局桩成
`neptune_query = lambda g: {'result':{'data':{'@value':[]}}}`，而
`NEPTUNE_ENDPOINT` 又总被 `setdefault` 成真实端点。所以 live 用例
**不能**用那个桩、也不能靠 `NEPTUNE_ENDPOINT` 是否设置来 gate：
每个 label 都会查到 0 个节点，然后「没有节点缺 preferred」自动成立——
一次空查询伪装成条件满足。这正是本项目反复踩的假健康 fallback。
修法是按文件路径显式加载真模块，并用独立的 `GRAPH_LIVE_AUDIT=true` 开关。

顺带发现：`tests/test_26` 里 `if not os.environ.get('NEPTUNE_ENDPOINT'): skip`
这个守卫**永不触发**（conftest 已 setdefault），那两个用例一直在跑桩数据。
未修，另记。

---

## 25. 采集 skip 规则只挡新写入、从不回收旧数据（2026-09-04）

`openclaw-tg-v2` 我一开始判成「死资源」，**实测推翻**：`describe-target-groups`
显示它在 AWS 侧活得很好。182.7 天不刷新的真正原因是
`etl_aws/config.py:61` 的 `SKIP_TG_PREFIXES = ('openclaw-',)`——**被刻意排除采集**。

进一步查图谱：名字含 openclaw 的节点有 17 个，其中 **14 个是新鲜的（0.0d）**。
Subnet / LoadBalancer / S3Bucket / SecurityGroup / ListenerRule / EC2Instance
全部在正常采集，skip 只作用在六个采集器中的两个（TargetGroup 与 Lambda）上。

所以结论是：**skip 规则是有意的、该保留；缺的是「加了 skip 之后回收已写入的旧数据」。**
这与 `source` 词表门禁那个坑完全同型——「代码已挡住新的、旧的还在库里」。

dry-run 出来的残留比预期多，共 9 个节点、四条 skip 前缀：

| 类型 | 节点 | 陈旧 | 命中规则 |
|---|---|--:|---|
| LambdaFunction | `Applications-Providerframeworkon...` | 175.7d | A·`Applications-` |
| LambdaFunction | `ApplicationsMyCluster-Handler...` | 175.7d | A·`Applications-` |
| LambdaFunction | `...health-canar-7bcf6eac-...` | 175.7d | A·`cwsyn-` |
| LambdaFunction | `neptune-etl-from-cfn` | 175.7d | A·`neptune-etl-from-cfn` |
| LambdaFunction | `openclaw-health-check` | 175.7d | A·`openclaw-` |
| TargetGroup | `openclaw-tg-v2` | 182.7d | A·`openclaw-` |
| TargetGroup | `nginx-tg-1` / `-2` / `-3` | 60.7d | B·源端已消失 |

5 个 Lambda 全是 175.7d、每个恰好 1 条边——同一轮 ETL 写入，之后 skip 前缀才加上。

### 回收器的两条规则与两个设计约束

`infra/reap_stale_nodes.py`，默认 dry-run：

- **规则 A（策略残留）**：节点名命中当前 skip 前缀。前缀**从 etl_aws/config.py 读**，
  不在脚本里硬写——否则两处漂移，而漂移方向必然是脚本删掉策略其实想保留的东西
- **规则 B（源端已消失）**：向 AWS 实查清单再比对。**判据绝不能是时间戳陈旧**——
  4 个「看起来都死了」的 TargetGroup 里有 1 个活着，只按陈旧度删会销毁活资源的节点。
  刻意只覆盖 TargetGroup：推广到别的类型前必须先为那个类型写出等价的实查逻辑

### 结构缺口

边有过期收敛（T-272），节点没有。契约里结构边写的
`expires_seconds: None`（生命周期跟随两端节点）在实现上是**悬空的**——
节点根本没有生命周期机制。

### 验收

按「清完后再跑一轮写入侧、违约仍为零」执行：ETL 复跑写入 310 节点 / 454 边，
4 个被回收的节点**全部未再生**，TargetGroup 稳定 14/14 有唯一 arn、零孤立节点。
`test_42::m08` 从失败翻成通过，8 个类型全部 UNLOCKED——证明这个闸门双向有效。

---

---

## 26. 节点没有生命周期机制 ——「生命周期跟随节点」是句悬空的话（2026-09-04）

契约里结构边（LocatedIn / Contains / BelongsTo…）的 `expires_seconds: None`
注解写的是「生命周期跟随两端节点、不独立过期」，对应 Dynatrace 的
static edge 继承 node lifetime 语义。但节点侧**根本没有生命周期机制**：
边不过期，节点也不过期，谁都不会消失。

实测代价：

| 类型 | 图谱节点数 | 真实数 | 陈旧 |
|---|--:|--:|--:|
| **Pod** | **581** | **70**（kubectl Running） | 511 个 >1d，313 个 >7d |
| SecurityGroup | 56 | — | 8 个 >7d，7 个 >30d |
| Subnet | 16 | — | 1 个 >30d |

任何「这个服务跑在哪些 Pod 上」的查询都会拖出一堆早已消失的 Pod。

**修法**：`graph_cleanup.expire_stale_nodes()`，与边过期对称但四处刻意不同：

1. **开关分开**（`GRAPH_NODE_EXPIRY_ENABLED`）—— 节点置 active=false 影响以它为
   端点的一切遍历，风险面比边大，要能独立灰度
2. **不要求 `has('active', true)`** —— 边的写入方一直在写 active，节点侧没有
   （实测 581 个 Pod 里 0 个带该属性），照抄边的写法会让查询恒为空。
   判据改成「尚未被判过期」`.not(has('active', false))`
3. **软过期，绝不 drop** —— 删节点连带删边，一次误判不可逆。物理删除走
   `infra/reap_stale_nodes.py`，那条路要求「向源端实查清单」这个更强前提
4. **TTL 下限 3 天** —— etl_cfn 是每天一次（`cron(0 18 * * ? *)`），而
   etl_xray 曾因层缺 requests 连死两天。窗口必须扛得住多日中断

每个节点类型都必须**显式**声明过期策略（数值或带理由的 null）——
沉默省略与「忘了写」无法区分，而两者后果相反。

---

## 27. etl_aws 从不写契约声明的统一时间戳字段（2026-09-04）

这是 #26 的**前置缺陷**，而且它差一点让 #26 的修复变成一个永不触发的闸门。

契约声明 `timestamp_field: last_seen`。实测覆盖率：

```
last_updated   849/1077  (78.8%)   <- etl_aws 写的，事实上的通用字段
last_seen       15/1077  ( 1.4%)   <- 契约声明的权威字段，几乎没人写
last_scanned    11/1077            <- etl_cfn 遗留
无任何时间戳   222/1077  (20.6%)   <- 事件日志 + 声明类
```

那 15 个全是 etl_cfn 写的。etl_aws 的 `upsert_vertex` **只写 last_updated**
（边写入 :272 一直两个字段都写，**只有顶点漏了**）。

### 为什么不能顺手改用 last_updated

覆盖率 78.8% vs 1.4%，看起来该用前者。**实测否证**：7 个 LambdaFunction
活得很好、由 etl_cfn 每日刷新（`last_seen` 新鲜），但 etl_aws 不碰它们，
它们的 `last_updated` 已陈旧 7 天以上。按 `last_updated` 判会**误杀活节点**。
统一字段的意义正在这里 —— 它必须由**每个**写入方都写，判据才成立。

### 两层后果，第二层是我差点交付的

① 判据字段缺失时，`count()` 返回 0 会被读成「没有陈旧节点」——
   与真的干净完全同形。这与「零流量和健康在指标上无法区分」同型。
   修法：执行器把 `unjudgeable`（本轮对多少个节点什么都没判）**单独上报**，
   不为 0 时 `stale` 这个数就不能读成「只有这么多陈旧节点」。

② **只修写入方救不了要被回收的那批节点。** 511 个死 Pod 不会再被任何 ETL
   触碰，新代码永远不会给它们写 `last_seen`，它们永远停在「不可判定」——
   而它们正是这个机制存在的理由。实测印证：修完写入方跑一轮 ETL 后覆盖率
   1.4% → 30.4%，`unjudgeable` 仍是 528 —— **升上去的全是活节点，死节点一个没动**。

   补 `infra/backfill_node_timestamp.py` 做一次性回填
   （`last_seen := max(已有, last_updated, last_scanned)`，取最大值而非覆盖，
   因为有节点两个都有）。回填后 79.4%，`unjudgeable` 528 → **0**，
   `stale` 0 → **527**，真实数字才浮出来。

   回填 dry-run 有个漂亮的印证：Pod 已有 `last_seen` 的恰好 **70** 个，
   与 kubectl 实际 Running 的 70 个完全吻合，可回填的 511 个就是死节点。

---

## 28. 6 个类型切 arn 会被 etl_cfn 撕成两份（2026-09-04）

上一轮补的解锁检查报出「8 个 preferred 类型里 7 个早已可切」。
实际去切时发现**回填完整只是必要条件**，还有第二个前提此前完全没被记录。

`test_35::g13` 的文档早就预告了这个形态：

> 将来把某个类型（例如 LambdaFunction）的身份键改成 arn 时，etl_aws 会跟着
> 契约走，而 etl_cfn 仍按 name 匹配 —— 两个 ETL 用不同的身份键写同一类节点，
> 图里必然裂成两份。这正是身份键类缺陷最隐蔽的形态。

按 `TYPE_TO_LABEL` 求交集：8 个候选里 **6 个**（LoadBalancer / DynamoDBTable /
SQSQueue / SNSTopic / LambdaFunction / StepFunction）被 etl_cfn 同时写入。

而且不是「改一下 etl_cfn 就行」：`get_or_create_vertex(label, physical_id,
stack_name)` **手上根本没有 arn**，唯一身份输入是 `physical_id`；etl_cfn 还在
:253/:362 处**刻意把内嵌 ARN 规范化成短名**，好让两个 ETL 在 `name` 上对齐。
Lambda 的 `PhysicalResourceId` 就是函数名，本地拿不到 ARN。

**已切换**：`TargetGroup`、`ECRRepository` —— 只有 etl_aws 写，不在
`TYPE_TO_LABEL` 里。实跑一轮 ETL 验证**零重复**（14/14/14/14、12/12/12/12）。

**剩 6 个**：不留裸的 `preferred: arn` —— 那正好重造 #24 修掉的「不可满足的
承诺」。新增 `preferred_blocked_by` 字段写明阻塞原因，并由 `test_42::m09`
强制「被 etl_cfn 写的类型若声明 preferred 就必须申报阻塞」、`m10` 从反方向锁
「非 name 身份的类型不得出现在 etl_cfn 的写入面」。m08 的 live 检查也学会把
它们报成 **BLOCKED** 而不是 UNLOCKED —— 报错成可切会诱导人去切。

---

## 收敛成四条方法论

### 一、缺陷类别高度集中，且都不是「数据采少了」

10 条里没有一条是「少采了一个指标 / 少接了一个源」。全部落在三类：

| 类别 | 本文档的条目 |
|---|---|
| **粒度 / 口径错配** | #1 DNS 混进 HTTP、#4 abort 下成功率无意义、#8 三义组合、#13 SLI 名错 |
| **写了但没人读** | #6 CRD 不删、#9 词表无门禁、#9 调用方 source 被丢、#10 层缺依赖、#14 R002 对 FIS 空转 |
| **身份不唯一 / 名字空间错配** | #7 K8s 名 vs 规范名、#12 Pod 标签 vs Deployment 名（**四套命名空间**，与 183 条错源边同源） |
| **判据覆盖面不足** | #11 Phase 5 只问「服务还好吗」不问「我干了什么」 |
| **统计量不同量纲 / 证据强度不分级** | #16 min 比单点基线、#17 吞吐通道当成功率通道用 |
| **前提未经证明就下结论**（新增第六类） | #20 没证明注入生效就判 refuted、#23 沿用未复核的既有认知 |
| **一个字段承担两个职责** | #22 target_service 兼任选择器与图谱节点名、#12 Pod 标签 vs Deployment 名、#24 identity_is_immutable 一个名字混淆两个问题 |
| **门禁只挡新的、不回收旧的** | #25 skip 前缀残留 9 个节点、source 词表门禁存量未归一、#27 修写入方救不了已死的存量 |
| **声明的字段不是真实在用的那个**（新增第八类） | #27 契约声明 last_seen 而写入方写 last_updated、managedBy/managed_by 并存 |

> **所以瓶颈在数据契约，不在采集覆盖面。**
> 规划重心应该是把这三类变成守门测试，而不是继续接新数据源。

### 二、异常路径与「一切正常」路径同样需要被执行过

#2（清理路径抛 TypeError）和 #5（全程健康时 min 不初始化）是一对镜像：
一个只在**出错时**走到，一个只在**完全没出错时**走到。
两者都在 72 个历史实验里一次没执行过——因为那 72 个实验**全部 passed 且判定门槛没有分辨力**。

> **「测试全绿」和「代码路径被执行过」是两件事。**

### 三、静默失败比报错危险一个量级

按危害排序，最危险的三条都是**不报错**的：
- #8 产出高置信度的**错误** confirmed
- #9 让 provenance 缓慢腐坏，活图谱看不出来
- #10 一个数据源死了 2 天

而 #2 那个 `TypeError` 虽然后果很脏（Pod CrashLoopBackOff），
但它**当场就吼出来了**，所以 20 分钟内就定位并修掉。

> **修复一个缺陷时，优先把它的失败模式从「静默」改成「响」**，
> 这比把它彻底修对更要紧——#7 的修法里就包含「无候选边从 info 升级为 warning」。

### 四、修数据之前先修写入侧，且顺序不能反

本轮和之前的存量清理都撞到同一件事：
- T-271 清 211 条错源边**必须**在部署新 ETL 代码之后，否则下一轮原样重建
- T-266 归一 source 取值同理
- #9 的 `deepflow` → `deepflow-etl` 收敛，**必须同时改读取侧**，
  否则存量清理查询从此匹配不到任何节点

> **验收标准不是「删干净了」，而是「再跑一轮不再产生」。**
> T-271 的验收就是清完后完整跑一轮 aws-etl，违约仍为 0。

---

## 附：最终状态（2026-08-31 11:12 UTC 实测）

| 指标 | 值 |
|---|---|
| 图谱 | 1073 节点 / 1640 边 |
| 依赖边验证状态 | **confirmed 11 / inconclusive 3 / untested 80**（已判定 14.89%） |
| 已验证边的分布 | 边类型 `Calls` 6 + `AccessesData` 7；后端 Chaos Mesh 6 + FIS 7 |
| refuted 边 | **0 条**。唯一那条经归因判定为「注入未生效」已修正回 inconclusive（#20） |
| 边过期收敛 | 已开启（`GRAPH_EDGE_EXPIRY_ENABLED=true`），当前 0 条待翻转 |
| 端点组合违约 | **0**（清 211 条后完整跑一轮 ETL 复核） |
| 未声明的 source 取值 | **0**（节点 3 种 / 边 11 种，全部在契约词表内） |
| 四个 ETL 函数 | 全部 200 OK，层 `:8`，门禁违约 0 条 |
| `Microservice-[RunsOn]->Pod` | 36 → **42** |
| 测试 | 461 → **516 passed / 0 failed**（新增 55 个守门用例） |

### 仍然未解的

- **`abort` 注入必然打伤目标 Pod**：三轮 Chaos Mesh 注入每一轮都要删 Pod 重建
  （重启 +12 / +2 / +12）。这是 abort 的固有代价而非缺陷；
  「报 PASSED 却留下坏 Pod」这个真缺陷已由 #11 修掉。
  **FIS 路径没有这个代价** —— 可作为偏好后端的一条理由。
- **230 条边 / 211 个节点没有 `source`**，多为 rca 与 chaos 自产实体，
  是否该强制未判定（T-267）。
- **剩余 84 条未判定**，按可验证性分三类：
  - **可用 FIS 验证**：AWSServiceEndpoint 11（`disrupt-vpc-endpoint`）、
    LambdaFunction 8（`invocation-error`，需扩展层）、RDSInstance 3 —— 见 T-290
  - **观测方无流量，判不了**：awesomeshop 六服务相关的边（0 副本已 166 天）、
    四个 Lambda 观测方（非持续调用）、`pethistory` 相关（12–26 请求，未过 20 下限，见 T-293）
  - **不适用运行时验证**：ECRRepository 12（镜像拉取只在启动期）
- **`pethistory` 的两条边永远拿不到结论**，除非给负载机加详情页流量配比或延长观测窗口。
  **不要降低 `min_observation_requests`** —— 那是拿判据换覆盖率。

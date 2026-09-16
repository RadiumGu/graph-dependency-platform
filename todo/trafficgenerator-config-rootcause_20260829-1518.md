# traffic-generator 静默失效 95 天：根因与修复

**日期**：2026-08-29 15:18 UTC
**对象**：`petadoptions/traffic-generator`（EKS 集群 `PetSite`，ap-northeast-1）

## 一句话结论

它从 2026-05-26 起 **95 天没有发出过一个请求**，累计约 8.2 万次空转。根因是镜像里的 **SSM 配置提供程序在运行时读不到 `/petstore` 下的键**，导致 `HttpClient.BaseAddress` 为 null；用环境变量注入同名键即可绕过并立即恢复。

## 发现路径

排查的起点是用户反馈「压测效果不太好」。第一眼看到的是 `resources.limits.cpu = 64m`（0.064 核）、`replicas=1`，本以为是配额掐死。**但读日志才发现它根本没在发请求**——量级问题背后是一个功能性完全失效。

## 症状

日志每 ~100 秒固定重复：

```
info: trafficgenerator.Worker[0]  Worker running at: 08/29/2026 14:30:11 +00:00
info: trafficgenerator.Worker[0]  Synchronous Housekeeping call
crit: trafficgenerator.Worker[0]  An invalid request URI was provided.
                                  Either the request URI must be an absolute URI or BaseAddress must be set.
```

这是 .NET `HttpClient` 在 `BaseAddress` 为 null 时调**相对路径**的固定异常，**卡在第一个 `/housekeeping/` 就抛出**，后续步骤全部未执行。

Pod `traffic-generator-5bdb8ff57d-trqv2`：运行 95 天、`0 restarts`、启动于 `2026-05-26T08:09:44Z`。

DeepFlow 独立验证（修复前，近 1 小时）：

```
11.0.3.160 的 L7 请求 : 0 条
11.0.3.160 的 L4 会话 : 0 条      ← 连一个 TCP 包都没有
同期 petsite 两 Pod 收到 : 777 / 783（全是健康检查）
```

## 应用的原设计

从容器内 `trafficgenerator.dll`（.NET 6，自包含 6.0.36，`linux-arm64`）提取字符串常量还原：

配置来自 **SSM 路径 `/petstore`**（依赖 `Amazon.Extensions.Configuration.SystemsManager/3.0.0`）：

| SSM 参数 | 内部字段 | 现值 |
|---|---|---|
| `/petstore/petsiteurl` | `_petSiteUrl` | `http://service-petsite.petadoptions.svc.cluster.local` |
| `/petstore/searchapiurl` | `_petSearchUrl` | `http://search-service.petadoptions.svc.cluster.local/api/search?` |
| `/petstore/trafficdelaytime` | `_trafficdelaytime` | `1` |

每轮循环模拟一次完整领养业务流，**全部相对路径**（因此强依赖 `BaseAddress`）：

```
/housekeeping/                            "Synchronous Housekeeping call"
/pethistory/deletepetadoptionshistory     "Deleted PetAdoptions History"
搜索(经 _petSearchUrl 直连 search-service)
/Adoption/TakeMeHome                      POST, application/x-www-form-urlencoded
/Payment/MakePayment
/PetListAdoptions
→ 延迟 trafficdelaytime 秒,重复
```

所有请求带 `X-Traffic-Generator` 头，并接了 `AWSXRayRecorder`。

依赖版本（`trafficgenerator.deps.json`）：

```
AWSSDK.Core/3.7.2.4                              ← 2021-08
AWSSDK.SimpleSystemsManagement/3.7.4.11          ← 2021-08
Amazon.Extensions.Configuration.SystemsManager/3.0.0
AWSXRayRecorder.Core/2.10.1
```

ASP.NET 组件是 2024-10 的，**AWS SDK 停留在 2021-08**，这个版本落差是可疑点但未坐实。

## 逐个排除的假设

| 假设 | 判定 | 依据 |
|---|---|---|
| SSM 参数缺失或值错 | ❌ | `/petstore/petsiteurl` v11 存在且值正确 |
| 参数在 Pod 启动后才补上（SSM 只在启动读一次） | ❌ | v11 写于 2026-04-01T18:34，早于 Pod 启动的 05-26T08:09 |
| IRSA 缺 SSM 权限 | ❌ | 角色 `ServicesEks2-trafficgeneratorServiceAccountRole1029-ZjtBnUIVOBI1` 内联策略含 `ssm:GetParametersByPath`/`GetParameters`/`GetParameter` on `*`；`AWS_ROLE_ARN`、`AWS_WEB_IDENTITY_TOKEN_FILE`、token 卷均已注入 |
| 误加载 `appsettings.Development.json`（其中 `petsiteurl` = `petsite-1088770206.us-east-1.elb.amazonaws.com`，**无 scheme**，赋给 `BaseAddress` 会抛 `UriFormatException`） | ❌ | ECR 镜像 config 与 Deployment 均无 `ASPNETCORE_ENVIRONMENT`；日志确认 `Hosting environment: Production` |
| SSM 加载抛异常导致崩溃 | ❌ | `0 restarts` |
| **一次性偶发** | ❌ | **rollout restart 后全新 Pod 仍报同一错误** |

`appsettings.json`（生产）里**没有** `petsiteurl` 键，只有 `Logging` —— 生产环境完全依赖 SSM 注入。

## 根因

**SSM 配置提供程序在运行时对这几个键返回空。** 参数正确、IAM 权限完备、凭证注入齐全，重启不恢复。

旁证很干净：日志打印 `Delay time : 20 seconds`，而 `/petstore/trafficdelaytime` 的值是 **1**——它用的是代码内置默认值 20，说明这个键**同样**没读到。三个键全部落空，指向提供程序整体失效而非单键问题。

> **未坐实的部分**：提供程序为何静默返回空，没有直接证据。2021 年的 `AWSSDK.Core` 3.7.2.4 与 IRSA web-identity 凭证路径的兼容性是主要嫌疑，但未能证明。**注意 petsite 自身读 SSM 是成功的**，所以这不是平台层问题，而是该二进制特有的。

## 修复

用环境变量覆盖。由于 SSM 提供程序对这些键返回空，环境变量**无论提供程序注册顺序如何都会胜出**。

集群内（已应用，随后已缩容到 0）：

```bash
kubectl set env deploy/traffic-generator -n petadoptions \
  petsiteurl='http://service-petsite.petadoptions.svc.cluster.local' \
  searchapiurl='http://search-service.petadoptions.svc.cluster.local/api/search?'
# 回滚: kubectl set env deploy/traffic-generator -n petadoptions petsiteurl- searchapiurl-
```

EC2 上跨 VPC 运行时改指内网 ALB，见 `crossvpc-loadgen-internal-alb_20260829-1518.md`。

**这个修复顺带消除了对 SSM 的依赖**——三个键全由环境变量提供，所以负载机的实例角色只需要 ECR 拉取权限。

## 验证

修复后日志：

```
Synchronous Housekeeping call
Starting Async LoadPetData
Total number of pets - 26
Deleted PetAdoptions History
Delay time : 20 seconds        ← 未设 trafficdelaytime 时仍为内置默认值
```

DeepFlow 实测新 Pod（`11.0.2.95`）近 10 分钟出向 L7（修复前同查询为 **0**）：

```
POST  /Adoption/TakeMeHome                          40
POST  /Payment/MakePayment                          39   ← 字符串常量里未见,运行时才发现的一步
GET   /PetListAdoptions                             38
GET   /?selectedPetType=puppy&selectedPetColor=black 12
GET   search-service.../api/search?                  6
```

## 需要知道的副作用

每轮调用 `/housekeeping/` 与 `/pethistory/deletepetadoptionshistory`，**周期性清空领养历史**。这是该 demo 生成器的设计行为（清空状态才能持续产生新领养），但依赖历史数据的演示或图谱 ETL 会看到数据被定期重置。

## 时间线还原（附带更正一条旧结论）

`/petstore/petsiteurl` 修改历史：

```
v1  2026-02-18  http://Servic-PetSi-by0kpyBtxswj-...elb.amazonaws.com   ← 公网 ALB
v2  2026-02-20  http://service-petsite.default.svc.cluster.local        ← 命名空间错误
v3  2026-03-06  http://service-petsite.default.svc.cluster.local        ← 仍错
v4  2026-03-28  http://service-petsite.petadoptions.svc.cluster.local   ← 修对
v5-v10  2026-04-01 17:10~18:00  在 .default. 与 .petadoptions. 之间来回翻 6 次
v11 2026-04-01 18:34  http://service-petsite.petadoptions.svc.cluster.local
```

服务实际在 `petadoptions` 命名空间，v2~v3 那段（02-20 → 03-28）指向的是不存在的 DNS 名。而 Neptune 图谱里 `trafficgenerator→petsite` 的 `Calls` 边 `last_seen = 2026-03-05`（`calls=1012`、`active=false`）正落在这段中间。

合理解释：早期某个 Pod 启动时读到 **v1（公网 ALB 地址）**，确实在发真实流量；2026-03-05 前后该 Pod 被重建，读到 v2 的错命名空间，流量断掉。**因为 SSM 只在启动时读一次，参数改了不重启永不生效**——这也是 v5~v10 那 6 次翻转（像是两个 CDK stack 在抢同一参数）没能自愈的原因。

> **更正**：此前判断那条陈旧边是「流量改走 ALB，所以 DeepFlow 观测不到直连边」。**该推断错误**——真实原因是源端根本什么都没发。图谱是对的，解释错了。已存为 repo-scoped lesson 覆盖旧的语义记忆。

## 可顺手清理的噪声

修复后该 Pod 每轮产生 8 条失败 DNS 查询（`...svc.cluster.local.svc.cluster.local`、`.cluster.local`、`.ap-northeast-1.compute.internal` 的 A + AAAA 组合）——即此前给 `cloudwatch-agent` 与 `fluent-bit` 修过的 **ndots 问题**，`traffic-generator` 未修。给它打 `dnsConfig: ndots=1` 可清掉；`trafficdelaytime=1` 之后这些查询会放大 20 倍。

# 韩国(ap-northeast-2)守夜灯灾备站点 —— 部署清单

**状态**:用户已选定方案(见下),**数据库仍有一项待确认**。
**日期**:2026-09-24 出清单,同日更正
**成本口径**:ap-northeast-2 按需价,USD/月(30 天),不含数据传输与存储 I/O。
仅作量级参考,准确数字请用 AWS Pricing Calculator。

---

## ✅ 用户已定的四项

| 项 | 决定 |
|---|---|
| 数据库方案 | **A. Aurora 全局数据库**,并授权「需要就改数据库配置」 |
| Temporal EC2 | `t4g.large` |
| DeepFlow | **容灾 region 不需要**(比原提案「平时 stopped」更省,直接不建) |
| NAT 网关 | 可以接受 |
| CDK | **另写精简 DR 栈** |

---

## ⚠️ 更正:本文件第一版的「硬冲突」结论是错的

第一版写着「主站 Aurora 是 `t4g.medium`(burstable),全局数据库不允许,
所以必须把生产实例换成 r 系列」。**那个结论错了。**

**错因**:我读了 CDK 源码(`lib/services-eks.ts:159` 确实写着
`InstanceClass.T4G / InstanceSize.MEDIUM`)就下了结论,**没有查活资源**。
代码与实物不一致 —— 这正是本仓库反复记过的错法:用代码代替实测。

实测(`describe-db-instances`)的真相:

    两个实例的 DBInstanceClass = db.serverless      ← Aurora Serverless v2
    ServerlessV2ScalingConfiguration = { Min 0.5, Max 4.0 } ACU
    引擎 aurora-postgresql 16.11
    StorageEncrypted = false，DeletionProtection = false，BackupRetention = 1 天

**Aurora Serverless v2 支持全局数据库**,所以:

| 第一版说的 | 实际 |
|---|---|
| 要把实例换成 r 系列(**实例替换**,有中断) | **不需要换实例** —— 已经是兼容的 `db.serverless` |
| 韩国侧最小 `db.r6g.large`(~170/月) | 韩国侧也可用 `db.serverless`,配小 ACU |
| 「较小的实例」做不到 | **做得到** |

`db.serverless` + `aurora-postgresql 16.11` 在 ap-northeast-2 **已确认可下单**
(`describe-orderable-db-instance-options`,4 个 AZ 可用)。

## ⚠️ 但浮出一笔更大的成本:主 region 的 ACU 下限

AWS 文档对 Serverless v2 的建议:

> 用于以下特性时建议的最小容量:…… **Aurora 全局数据库 —— 8 ACU
> (仅适用于主 AWS Region)**

主站现在是 **0.5 ACU**。按东京 Serverless v2 约 $0.20/ACU-hr 估算:

| 主站 ACU 下限 | 月度**保底**费用 | 增量 |
|---|---|---|
| 0.5(现状) | ~73 | — |
| **8(建议值)** | ~**1,168** | **+~1,095/月** |

⚠️ 我尝试用 Pricing API 取准确单价时把 `usagetype` 的格式猜错了,
上表用的是公开单价口径。**请用 Pricing Calculator 复核。**

**关键:这是「建议」不是硬性要求。** 0.5 ACU 也能建全局数据库,
代价是复制延迟与扩容抖动的风险。对演示环境很可能可以接受。

👉 **待确认:主站 ACU 下限保持 0.5,还是按建议提到 8?**
(保持 0.5 是我的建议 —— 这是演示环境,且 +1,095/月 的量级远超其余所有资源之和)

好消息:**改 Serverless v2 的容量范围是在线操作**,不是实例替换,无需停机。
所以「改数据库配置」的代价比第一版描述的小得多 —— 除了钱。

## 另两项执行前要注意的

- `StorageEncrypted = false`。全局数据库允许未加密集群,但若以后要加密,
  **不能原地开启** —— 要快照 + 加密恢复。现在不动它,记录在此
- `BackupRetention = 1 天`。全局数据库不改变这一点,但灾备场景下 1 天很短,
  **建议提到 7 天**(成本按快照存储计,量级小)

---

## ⚠️ 一条硬冲突,必须你先定

你的要求是「**数据库同步,但采用较小的实例**」。这两条在 Aurora 全局数据库上
**不能同时满足**,原因是实测查证的约束:

> Aurora 全局数据库要求**内存优化型**实例类型,**不允许 burstable(t3/t4g)**。
> AWS 文档:"An Aurora global database requires DB instance classes that are
> optimized for memory-intensive applications."
> 撞到时的报错是 `DB instances in this cluster has a size that isn't
> compatible with Aurora global databases`。

而**主站 Aurora 现在正是 burstable**:

    引擎     Aurora PostgreSQL 16.11
    writer   t4g.medium
    reader   t4g.medium × 1
    库名     adoptions
    其他     DatabaseInsights ADVANCED / PI 保留 15 个月

所以走全局数据库要付两笔代价:**① 改动生产主站**(把 writer/reader 换成
r 系列,是实例**替换**不是在线调参);**② 韩国侧也必须 r 系列**,
最小 `db.r6g.large` 已经**比现在的 t4g.medium 更大** —— 「较小的实例」做不到。

### 三个可选方案

| 方案 | 韩国侧月成本 | RPO | RTO | 要不要动主站 | 「较小实例」 |
|---|---|---|---|---|---|
| **A. Aurora 全局数据库** | ~**170**(db.r6g.large) | 秒级 | 分钟级(托管故障转移) | **要**(主站换 r 系列,另 +~170) | ❌ 做不到 |
| **B. 逻辑复制到小实例** | ~**50**(db.t4g.medium 单实例) | 秒~分钟(异步) | 中(需人工提升) | 轻微(开 `rds.logical_replication`,**要重启**) | ✅ |
| **C. 跨 region 快照复制** | ~**5**(仅快照存储,**无运行实例**) | = 快照间隔(如 1h/24h) | 高(恢复需 10~40 分钟) | 不要 | ✅ 最省 |

**我的建议是 B**,理由:它最贴合你「同步 + 小实例」的原话,且不需要把生产主站
换成更贵的实例。代价是 ① 原生逻辑复制**不复制 DDL**(建表/改表要另行同步),
② 提升为可写需人工介入(可以做成 Temporal workflow 的一个步骤)。

**C 最符合「守夜灯」的字面精神**(平时连数据库实例都不开),但 RPO 由快照间隔
决定,演示「切换」时要等恢复。

👉 **请选 A / B / C。下面的清单按 B 计价。**

---

## 资源逐项

### ① Temporal 服务端(你指定的 EC2)

| 项 | 规格 | 月成本 | 说明 |
|---|---|---|---|
| EC2 实例 | `t4g.large`(2 vCPU / 8 GiB,ARM) | ~**49** | Temporal server + PostgreSQL + Web UI,docker compose 单机 |
| EBS gp3 | 50 GiB | ~**4** | Temporal 的持久化(workflow 历史) |
| 小计 | | ~**53** | |

选 `t4g.large` 的理由:Temporal 自带 PostgreSQL 单机形态在 4 GiB 上会紧;
8 GiB 留出 worker 与 Web UI 的余量。**若只做演示可降到 `t4g.medium`(~25/月)。**

⚠️ Temporal **也承担 worker**(执行 DR 步骤的进程)。worker 需要跨两个 region
的 AWS 操作权限 —— 这一点在阶段 D 会细化,但实例角色要在这里就开对。

### ② 网络(这是「不暴露公网」的关键)

| 项 | 规格 | 月成本 | 说明 |
|---|---|---|---|
| VPC | 新建,10.20.0.0/16 | 0 | **不用默认 VPC**(默认 VPC 的子网都有 IGW 路由) |
| 私有子网 × 2 | apne2-a / apne2-c | 0 | 无 IGW 路由 |
| NAT 网关 × 1 | 单 AZ | ~**41** | **仅出站**。装 Temporal / 拉镜像用 |
| VPC 端点(SSM 三件套) | ssm / ssmmessages / ec2messages | ~**22** | 让我能在无公网入站的前提下管理实例 |
| 小计 | | ~**63** | |

**为什么 NAT 与 VPC 端点都要**:NAT 让实例能下载软件;SSM 端点让管理通道
不依赖 NAT(装完可以拆 NAT,省 41/月,实例仍可管理)。
**装完拆 NAT 后网络月成本降到 ~22。**

### ③ EKS(守夜灯:控制面常开,节点为 0)

| 项 | 规格 | 月成本 | 说明 |
|---|---|---|---|
| EKS 控制面 | 1 个集群 | ~**73** | **这笔省不掉**,守夜灯模型的主要固定成本 |
| 托管节点组 | `t4g.xlarge`,min 0 / desired **0** / max 3 | **0** | 平时零节点,切换时扩容 |
| 小计 | | ~**73** | |

主站对照:两个节点组各 `t4g.xlarge` min2/max3/**desired2** ,分别钉在
`ap-northeast-1a` / `ap-northeast-1c`,稳态 4 个节点。
韩国侧只建**一个**节点组(单 AZ 起步),切换时 `desired 0 → 2`。

⚠️ CDK 注释里写着「改 `instanceTypes` 是**替换**操作,
`update-nodegroup-config` 不接受该字段」—— 所以实例类型要一次定对。

### ④ 数据库(按方案 B 计价)

| 项 | 规格 | 月成本 | 说明 |
|---|---|---|---|
| Aurora PostgreSQL 实例 | `db.t4g.medium` × 1 | ~**50** | 逻辑复制的订阅端,单实例无 reader |
| 存储 | 按量 | ~**5** | 视数据量 |
| 小计 | | ~**55** | |

### ⑤ AgentCore

| 项 | 月成本 | 说明 |
|---|---|---|
| AgentCore Runtime(temporal-mcp) | 按调用计费,闲置≈**0** | 已实测 ap-northeast-2 可用 |

同 region 部署,免去跨 region 打通 —— 这也是阶段 C 的落点。

### ⑥ DeepFlow

| 项 | 规格 | 月成本 | 说明 |
|---|---|---|---|
| EC2 `deepflow-server` | `t4g.xlarge`(与主站同规格) | ~**98** | 主站实测就是这个规格 |

⚠️ **这是清单里最贵的单项。** 守夜灯下它可以**平时 stop、切换时 start**
(stop 后只付 EBS,~4/月)。**建议默认 stopped** —— 请确认是否接受
「切换后需手动/由 workflow 启动 DeepFlow」。

### ⑦ 明确不做

- **Neptune** —— 按你的要求不管
- **公网入口** —— 无 internet-facing ALB、无公网 IP、无 Route53 对外记录
- **ECR** —— 先用主站 region 的 ECR 跨 region 拉取(省一套复制);
  若要求灾备自洽再加 ECR 复制规则

---

## 月成本汇总(更正后:方案 A + Serverless v2 + 无 DeepFlow)

第一版按「r 系列实例」算出 ~529/月,**那是基于错误前提的**。
按实测的 `db.serverless` 重算:

| 组成 | 常态 | 装完拆 NAT 后 |
|---|---|---|
| Temporal EC2 `t4g.large` + 50GiB EBS | 53 | 53 |
| 网络(NAT 41 + SSM 端点 22) | 63 | **22** |
| EKS 控制面 | 73 | 73 |
| 韩国 Aurora(`db.serverless`,min 0.5 ACU) | ~**22** | ~22 |
| DeepFlow | **0**(不建) | 0 |
| AgentCore(temporal-mcp) | ~0 | ~0 |
| **韩国侧合计** | ~**211** | ~**170** |

**主站增量**(取决于上面那个待确认项):

| 主站 ACU 下限 | 主站增量 | 总计(拆 NAT 后) |
|---|---|---|
| 保持 0.5 | **0** | ~**170** |
| 提到 8(AWS 建议) | +~1,095 | ~1,265 |

也就是说:**若主站 ACU 保持 0.5,整套灾备站点约 170 USD/月** ——
比第一版估的 529 低得多,而且不需要改动生产实例类型。
另外全局数据库本身有跨 region 复制的数据传输费,按实际写入量计。

---

## 公网暴露面:逐项为零的理由

| 组件 | 为什么零暴露 |
|---|---|
| Temporal EC2 | 私有子网、无公网 IP、安全组无 0.0.0.0/0 入站;管理走 SSM Session Manager |
| EKS 控制面 | endpoint 设为 **private only**(`endpointPublicAccess: false`) |
| EKS 节点 | 私有子网,且平时 desired=0 根本没有节点 |
| Aurora | 私有子网,`publiclyAccessible: false`,安全组只放行 VPC 内 |
| DeepFlow EC2 | 私有子网、无公网 IP;平时 stopped |
| ALB | **不创建** internet-facing ALB。切换时若需入口,创建 **internal** ALB |
| NAT 网关 | 仅**出站**;NAT 不接受入站连接 |

⚠️ 一处如实说明:**NAT 网关本身有公网 IP**(出站用)。它不接受主动入站连接,
但如果有人在意「韩国 region 不出现任何公网 IP」,那就得去掉 NAT、
改用 VPC 端点 + 预先烘焙 AMI 的方式装 Temporal —— 会增加搭建复杂度。**请确认。**

---

## 与主站(ap-northeast-1)的差异

| 维度 | 主站 | 韩国灾备 |
|---|---|---|
| EKS 节点 | 2 组 × t4g.xlarge,稳态 4 节点 | 1 组 × t4g.xlarge,**desired 0** |
| EKS endpoint | public(推测,待核) | **private only** |
| Aurora | writer + reader,均 t4g.medium | 单实例 t4g.medium(方案 B) |
| 入口 | internet-facing ALB(实测 `internetFacing: true`) | **无公网入口** |
| DeepFlow | t4g.xlarge 运行中 | t4g.xlarge **stopped** |
| Neptune | 有 | **无** |
| AgentCore | 5 个 runtime(WaggleAI ×4 + graph_dependency_mcp) | 仅 temporal-mcp |

---

## ⚠️ CDK 不能直接换 region 部署

`/home/ec2-user/works/one-observability-demo/PetAdoptions/cdk/pet_stack` 实测:

    硬编码 ap-northeast-1     16 处（services-eks.ts 7、agents/agent-config.ts 7 …）
    硬编码账号 926093770964    3 处（三个 targetGroupArn 字面量）
    NodeGroup 钉死 AZ         ap-northeast-1a / ap-northeast-1c

两条路:

- **参数化复用** —— 把 16 处 region、3 处账号、AZ 列表抽成 context/环境变量。
  好处:一套代码两地;代价:动的是**正在跑生产的栈**,爆炸半径大
- **另写精简 DR 栈** —— 只建 petsite 必需的 VPC/EKS/Aurora/DeepFlow。
  好处:不碰生产 CDK;代价:两套代码会漂移

**我倾向「另写精简 DR 栈」** —— 生产 CDK 上有并发会话在改,而且灾备侧本来就
只要主站的一个子集(无 Neptune、无 ECS、无 4 个 WaggleAI runtime)。
硬编码那 16 处里有 7 处在 `agents/agent-config.ts`,而灾备侧不部那些 agent。

---

## 拆除方式

按**逆序**删,每步都可独立执行:

    ① EKS 节点组（desired 0 时删很快）
    ② EKS 集群
    ③ Aurora 实例 + 集群（先关删除保护，注意最终快照）
    ④ DeepFlow EC2 + EBS
    ⑤ Temporal EC2 + EBS（Temporal 的 workflow 历史在这块盘上，先导出）
    ⑥ NAT 网关 / VPC 端点
    ⑦ 子网 / 路由表 / 安全组 / VPC
    ⑧ AgentCore runtime

⚠️ **销毁类操作我不执行** —— 会把命令逐条写出来给你跑。

---

## 切换时需要人工介入的步骤

守夜灯不是全自动。下列步骤即使有 Temporal 编排也需要人参与或至少人工放行:

| 步骤 | 为什么不能全自动 |
|---|---|
| **决定切换** | 「主站真的挂了」这个判断本项目的核心立场就是不能只看指标 —— 零流量与健康在指标上无法区分 |
| 数据库提升为可写(方案 B) | 逻辑复制的订阅端提升是不可逆操作,提升后回切要重建复制 |
| DDL 差异补齐(方案 B) | 原生逻辑复制不复制 DDL,两边表结构可能不一致 |
| DNS / 入口切换 | 韩国侧无公网入口,要临时创建 internal ALB 或开放入口 —— 属于改变暴露面 |
| 启动 DeepFlow | 若采纳「平时 stopped」 |
| 回切 | 主站恢复后的反向同步需要重建,不是对称操作 |

建议:Temporal workflow 把这些做成**需 signal 放行的步骤**
(`signal_workflow` 正是为此)。这样自动化负责顺序与不遗漏,
人负责不可逆的判断。

---

## 需要你确认的四件事

1. **数据库方案选 A / B / C**(上面那张表;我建议 B)
2. **DeepFlow 是否接受「平时 stopped、切换时启动」**(省 ~94/月)
3. **是否接受 NAT 网关有公网出站 IP**(否则要改用 VPC 端点 + 预烘焙 AMI)
4. **CDK 用「参数化复用」还是「另写精简 DR 栈」**(我倾向后者)

确认后我按清单逐项建,并把每一步按七项记进
`docs/runbooks/deployment-record.md`。

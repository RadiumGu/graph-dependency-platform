# 部署记录手册

**这份文件记的是「实测走通的那一条」,不是设计时打算怎么做的那一条。**

存在的理由很具体:`e0eeada` 那次更新线上 streamlit 时,
README 里记的流程**照做会失败**。部署流程会随环境漂移
(目标机的仓库状态、宿主能力、依赖平台的托管形态),
而漂移只在下一次部署时才暴露 —— 那时人往往正急着上线。

所以规矩是:**一次部署没有记录,就不算完成。**

---

## 每次部署必须记下这七项

缺哪一项,下一个人(或下一轮的我)就会在那一项上卡住:

| 项 | 为什么 | 反例 |
|---|---|---|
| ① 目标 | 部到哪台/哪个服务/哪个 region | 「部到实例上」—— 哪台? |
| ② 前置状态 | 部署前的可核对值(md5 / 版本 / HEAD) | 没有它就无法判断部署是否真的生效 |
| ③ **实际执行的命令** | 逐条,照抄可跑 | 写「更新代码并重启」等于没写 |
| ④ 生效核实 | 用**与部署不同的**手段确认 | 「命令返回 0」不是生效证据 |
| ⑤ 失败过的做法 | 连同失败原因 | 不记就会被重试 |
| ⑥ 回滚方式 | 具体到命令/文件位置 | 「恢复备份」—— 备份在哪? |
| ⑦ 日期 | 环境会漂移,记录会过期 | 无日期的记录无法判断是否还可信 |

**④ 尤其容易糊弄。** 部署命令自己成功不等于东西生效:
本项目实测过三次「命令成功但没生效」,见下面各节。

---

## 一、Streamlit 展示站(`openclaw-instance-v2` / `i-022fb7c32b71c72d9`)

**日期**:2026-09-18 实测,2026-09-23 复用无变化

**目标**:`streamlit-demo.service`,源码在目标机 `/home/ubuntu/tech/graph-dependency-platform`

### ⚠️ `git pull` 在这台机器上必然失败

目标机的仓库**不是干净的跟踪克隆** —— `demo/` 下大量文件处于 `A` / `AM`
(已暂存的新增/修改)状态:

    error: Your local changes to the following files would be overwritten by merge:
      demo/_common.py demo/components/... demo/pages/1_Edge_Verification.py ... (19 个文件)
    Merge with strategy ort failed.

`git pull` / `git merge` 都会中止。**而且中止时 md5 不变,服务照常运行** ——
如果只看「命令有输出、服务 active」就会以为部署成功了。

### 走通的做法:只取需要的那一个文件

通过 SSM(`AWS-RunShellScript`)对 `i-022fb7c32b71c72d9` 执行:

    cd /home/ubuntu/tech/graph-dependency-platform || exit 1
    sudo -u ubuntu cp demo/pages/1_Edge_Verification.py /tmp/1_Edge_Verification.py.bak
    md5sum demo/pages/1_Edge_Verification.py | cut -c1-12          # ② 前置状态
    sudo -u ubuntu git fetch origin <branch>
    sudo -u ubuntu git checkout origin/<branch> -- demo/pages/1_Edge_Verification.py
    md5sum demo/pages/1_Edge_Verification.py | cut -c1-12          # ④ 生效核实
    sudo systemctl restart streamlit-demo.service
    sleep 14
    systemctl is-active streamlit-demo.service

`git checkout <ref> -- <path>` 精确覆盖一个路径,不碰目标机其他本地状态。

### ④ 生效核实要做两层

md5 变了只证明**文件**换了,不证明**页面渲染正确**。本项目已记过这条教训:
**AppTest 全绿 ≠ 渲染正确** —— 区块常写在 `if C.neptune_online()` 里,
离线时整块跳过,测试绿但一行都没渲染。

所以在**目标机上**(那里 Neptune 在线)再跑一次:

    sudo -u ubuntu env NEPTUNE_ENDPOINT=<...> REGION=ap-northeast-1 \
      python3 -c "from streamlit.testing.v1 import AppTest; \
        at=AppTest.from_file('demo/pages/1_Edge_Verification.py', default_timeout=200).run(); \
        print([s.value for s in at.subheader]); print('异常:', at.exception)"

`异常: ElementList()` 为空 + 小标题里能看到新区块,才算生效。

### ⑥ 回滚

`/tmp/1_Edge_Verification.py.bak`(部署前备份) → `cp` 回去 → `systemctl restart`。

### ⑤ 失败过的做法

| 做法 | 结果 |
|---|---|
| `git pull --ff-only origin <branch>` | 中止(未跟踪/已暂存文件冲突),**md5 不变但服务仍 active** |
| `git reset --hard origin/<branch>` | 会丢掉目标机上那批本地状态 —— 属销毁类操作,**不执行** |
| 只看 `systemctl is-active` | 服务一直是 active,对部署是否生效**零信息量** |

---

## 二、Lambda 层(`neptune-etl-from-agentcore`)

**日期**:2026-09-16 实测

**目标**:ap-northeast-1 的 `neptune-etl-from-agentcore`,架构 **x86_64**
(我一度误判成 arm64;boto3/botocore 是纯 Python 无编译扩展,平台无关,
所以误判没造成故障 —— 但判断依据是错的)

### 走通的做法

层:`neptune-client-base:20` + `botocore-current:1`(后者我建的,16.4MB,
含 boto3/botocore 1.43.82 + jmespath/s3transfer/dateutil/six)。

⚠️ **刻意不含 urllib3 / certifi / idna / requests** —— 打进去会与 Lambda
运行时自带的版本冲突。

### ④ 生效核实:看图谱产出,不看部署返回

部署成功的证据不是 `update-function-configuration` 返回 200,而是下一轮 ETL
跑完后图上的变化:

    RoutesToRuntime           0 → 5
    Delegates_via_gateway     0 → 3
    AgentTool / RoutesTo 幽灵  停止产生
    割点数                    10 → 12
    WaggleAIGateway blocked   = 4（与契约注记「4 个子 agent 全断」精确吻合）

### ⑥ 回滚

函数 layers 改回只留 `neptune-client-base:20`;
旧代码包在 `/home/ec2-user/.kiro/crew/scratch/etl_agentcore_deployed_backup.zip`。

### ⑤ 失败过的做法

真因是 botocore 版本太旧,把 `GetGatewayTarget` 响应里的 tagged union
`http` 成员**静默剥掉** —— 调用成功、字段消失。所以「调用没报错」
不能作为 SDK 版本合适的证据。

---

## 三、Cron 作业(`graph-coverage-metrics`,id `964afa3a`)

**日期**:2026-09-15 实测

### ⚠️ command 模式在本宿主根本不可用

    No POSIX shell available

必须用 **script 模式**:`~/.kiro/crew/crons/graph_coverage.py:run`。

### ④ 生效核实必须走真实触发路径

**手动跑通被调用的脚本只证明脚本能跑**,不证明调度链路通。
用 `cron_trigger` 触发那个 job,然后核对两处:

    kirocrew cron list        →  该 job 显示 ✅
    CloudWatch 新数据点        →  namespace `GraphDependency/Coverage`

⚠️ 命名空间是 `GraphDependency/Coverage` 而不是 `GraphDependency`
(我查错过一次,得到「无数据点」的假阴性)。

### ⑤ 失败过的做法

| 做法 | 结果 |
|---|---|
| command 模式 | `No POSIX shell available`,job 永远失败 |
| 手动跑 `graph_coverage.py` 确认「部署成功」 | 脚本能跑 ≠ 调度能调它 |
| 查 namespace `GraphDependency` | 无数据点 —— 假阴性,真名带 `/Coverage` |

---

## 四、韩国灾备站点(ap-northeast-2)—— 逐步记录中

### 4.1 主站 Aurora 的 ACU 下限:0.5 → 1

**日期**:2026-09-24

**① 目标**:ap-northeast-1 的 Aurora 集群
`serviceseks2-databaseb269d8bb-efjeyzicx2ak`(Aurora PostgreSQL 16.11,
Serverless v2)。**这是动生产。**

**为什么动**:要把它变成 Aurora 全局数据库的主集群。AWS 建议
Serverless v2 用于全局数据库时**主 region 最小容量 8 ACU**,而当时是 0.5。
用户权衡后定为 **1** —— 提一档留余量,但不按 8 那个建议值
(8 ACU 的保底费用约 1,168 USD/月,量级超过灾备站点其余所有资源之和)。

**② 前置状态**:

    ServerlessV2ScalingConfiguration = { MinCapacity: 0.5, MaxCapacity: 4.0 }
    Status = available
    BackupRetentionPeriod = 1      ← 用户明确要求不变
    StorageEncrypted = false       ← 未动；⚠️ 以后要加密**不能原地开启**
    DeletionProtection = false

**③ 实际执行的命令**:

    aws rds modify-db-cluster --region ap-northeast-1 \
      --db-cluster-identifier serviceseks2-databaseb269d8bb-efjeyzicx2ak \
      --serverless-v2-scaling-configuration MinCapacity=1,MaxCapacity=4.0 \
      --apply-immediately

⚠️ Serverless v2 的容量范围调整是**在线操作**,不是实例替换,无需停机。

**④ 生效核实**(用与执行**不同**的手段):

    aws rds describe-db-clusters ... \
      --query 'DBClusters[0].ServerlessV2ScalingConfiguration'
        → { MinCapacity: 1.0, MaxCapacity: 4.0 }      ✅ 独立 describe 确认

⚠️ **配置生效 ≠ 容量下限真的抬起来了。** CloudWatch 的
`AWS/RDS ServerlessDatabaseCapacity`(维度 `DBClusterIdentifier`)改动后要
几分钟才出新数据点;改动瞬间取到的 `Minimum 0.5` 是**改动之前**的数据点,
不能当成失败。

实测的完整证据(period=60s,`Minimum` 才是下限的证据,`Average` 不是):

    09:49  Min 0.5   Avg 0.500     改动前
    09:50  Min 0.5   Avg 0.500     改动前
    09:51  Min 0.5   Avg 0.500     改动前
    09:52  Min 0.5   Avg 0.500     改动前
    09:53  Min 0.5   Avg 0.796     过渡（modify 发出）
    09:54  Min 1.0   Avg 3.354     ✅ 下限已生效

**约 1 分钟生效**,集群状态从 `modifying` 回到 `available`。
09:54 的 `Average 3.354` 是扩容动作本身造成的瞬时抬升,不是稳态负载。

**⑤ 失败过的做法**:

| 做法 | 结果 |
|---|---|
| 以 `modify-db-cluster` 的返回值当生效证据 | 那是执行命令自己的输出,不构成独立核实 |
| 立刻查 CloudWatch ACU 指标 | 取到改动前的数据点(0.5),会被误读成没生效 |
| `aws pricing get-products` 取准确 ACU 单价 | `usagetype` 格式猜错,查不到。改用公开单价口径并标注需人工复核 |
| 查 `AWS/ApplicationELB RequestCount` **不带维度** | 返回空 —— 该命名空间不带维度聚合查不出东西,不是业务真的没流量 |

**⑥ 回滚**:

    aws rds modify-db-cluster --region ap-northeast-1 \
      --db-cluster-identifier serviceseks2-databaseb269d8bb-efjeyzicx2ak \
      --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=4.0 \
      --apply-immediately

同样在线。⚠️ **但若此时已建成全局数据库,降回 0.5 会让跨 region 复制更易
出现延迟 —— 回滚 ACU 前先确认全局数据库状态。**

**⑦ 判据教训:判断线上资源规格必须查活资源。**

CDK 源码 `PetAdoptions/cdk/pet_stack/lib/services-eks.ts:159` 写着
`InstanceClass.T4G / InstanceSize.MEDIUM`,我据此断定主站是 burstable、
Aurora 全局数据库不允许 burstable、**必须替换生产实例** —— 全错。
`describe-db-instances` 实测两个实例都是 `db.serverless`,
而 Serverless v2 本来就支持全局数据库。

后果的形状值得记:那个错误结论让我给用户报了一份**贵 3 倍**的成本
(~529/月 vs 实际 ~170/月),还让用户以为要承担一次生产实例替换的中断。
**IaC 源码写的是部署意图,活资源才是事实,两者会漂移。**

---

## 五、待记录

- [ ] 韩国 VPC / 私有子网 / NAT / SSM 端点
- [ ] Temporal EC2(`t4g.large`)+ docker compose 形态的 Temporal server
- [ ] 韩国 EKS(控制面 private-only,节点组 `desired=0`)
- [ ] Aurora 全局数据库(主站转为全局主集群 + 韩国 `db.serverless` 从集群)
- [ ] `temporal-mcp` → ap-northeast-2 的 AgentCore

**韩国侧不建 DeepFlow**(用户决定),**不建 Neptune**(用户要求)。
清单见 `todo/KOREA-DR-MANIFEST_20260924.md`。

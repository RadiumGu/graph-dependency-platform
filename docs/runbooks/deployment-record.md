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

### 4.2 韩国网络栈(`dr-korea-network`)

**日期**:2026-09-24

**① 目标**:ap-northeast-2,CloudFormation 栈 `dr-korea-network`
(模板 `infra/dr-korea/01-network.yaml`)。

**② 前置状态**:

    VPC 数 = 1（只有默认 VPC）    NAT = 0    CFN 栈 = 0

**③ 实际执行的命令**:

    aws cloudformation deploy --region ap-northeast-2 \
      --stack-name dr-korea-network \
      --template-file infra/dr-korea/01-network.yaml \
      --no-fail-on-empty-changeset

**④ 生效核实**(不看 deploy 的返回,查路由表这个**独立**事实):

    子网                          默认路由                  自动公网IP
    dr-korea-public-nat-only     igw-09dc9744704d1309b     False
    dr-korea-private-a           nat-0582007e8f8b419f5     False     ← NAT 不是 IGW
    dr-korea-private-c           nat-0582007e8f8b419f5     False     ← NAT 不是 IGW

    三个 SSM 端点全部 available，PrivateDnsEnabled = True

**这才是「零公网入站」的证据** —— 私有子网的默认路由指向 NAT 而非 IGW,
且不自动分配公网 IP。栈建成本身证明不了这一点。

产出:`vpc-0238efd50c0bf0dac`、`subnet-0f599ec0925b9158b`(a)、
`subnet-0ad20a5cd85143dff`(c)、NAT EIP **3.37.176.45**(本站点唯一公网 IP,仅出站)。

**⑥ 回滚**:`aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-network`
(须先删依赖它的栈 —— 导出值被引用时删不掉)

---

### 4.3 Temporal 服务端(`dr-korea-temporal`)

**日期**:2026-09-24

**① 目标**:`i-06f0a3e4961b8061e`(`t4g.large`,私有子网,无公网 IP,无密钥对),
私有 IP `10.20.1.125`。Temporal 1.29.7 + PostgreSQL 16 + UI 2.54.1,docker compose。

**③ 实际执行的命令**:

    aws cloudformation deploy --region ap-northeast-2 \
      --stack-name dr-korea-temporal \
      --template-file infra/dr-korea/02-temporal.yaml \
      --capabilities CAPABILITY_IAM \
      --no-fail-on-empty-changeset

**④ 生效核实**(四层,**最后一层才是证据**):

    ① SSM 注册        aws ssm describe-instance-information → PingStatus Online
    ② docker 在跑      systemctl is-active docker → active
    ③ 三个容器 Up      docker compose ps → postgresql / temporal / temporal-ui 全 Up
    ④ HTTP API 应答    curl localhost:7243/api/v1/namespaces

④ 的实际返回(这才是 temporal-mcp 能用的证据):

    {"namespaces":[{"namespaceInfo":{"name":"default",
      "state":"NAMESPACE_STATE_REGISTERED",
      "capabilities":{"eagerWorkflowStart":true,"syncUpdate":true,"asyncUpdate":true},
      "supportsSchedules":true},
      "config":{"workflowExecutionRetentionTtl":"86400s",...

**容器 Up 不是证据** —— 进程活着不等于 API 开着、端口对。
CFN 报 `Successfully created` 时 Temporal 其实还没起(引导仍在装 docker)。

#### 顺带得到的两个阶段 B 事实

**⑴ 这套服务端的 namespace capabilities 里没有 `workflowPause`,
也没有 `standaloneActivities`。**

所以 temporal-mcp 的 `pause_workflow` / `unpause_workflow` /
`list_activities` / `describe_activity` 在这套服务端上**根本不适用** ——
那种失败不是 temporal-mcp 的缺陷。验证矩阵里预测的第一步
(「先 describe_namespace 抄 capabilities」)由这次核实免费交付了。

**⑵ `workflowExecutionRetentionTtl = 86400s`(1 天)。**

若把 DR 计划以 workflow 形式「保存」在 Temporal 里,**1 天后历史就被清掉**。
阶段 D 设计「保存计划」机制时必须正面处理这一点
(提高保留期 / 用 Schedule 承载 / 计划正文另存)。

**⑤ 失败过的做法 —— 两次,都是我引入的**

**① 编了一个不存在的镜像 tag。** 模板里写 `temporalio/ui:2.42.0`,
部署直接失败:

    temporal-ui Error manifest for temporalio/ui:2.42.0 not found:
      manifest unknown: manifest unknown

实际可用的是 **2.54.1**(`temporalio/ui` 的版本线与 server 不同步,
不能按 server 版本推)。查法:

    curl -s 'https://hub.docker.com/v2/repositories/temporalio/ui/tags?page_size=12&ordering=last_updated'

`auto-setup` 我写的 1.29.0 也不在可用列表里,改用确认存在的 **1.29.7**。
**教训:镜像 tag 属于「不能猜的值」,和符号名、属性名、路径同一类。**

**② 把 PostgreSQL 口令打进了日志文件。** UserData 开头写了
`set -euxo pipefail` 并把输出 `tee` 到 `/var/log/temporal-bootstrap.log`,
于是 `PGPW="$(openssl rand -hex 24)"` 这一行被 `set -x` trace 出来,
明文口令落进日志:

    + PGPW=d31103e5e8b316156e1566c4098ac0441e62d43ec26c8074

处置(两步,顺序不能反):**先轮换口令,再清日志** ——
只清日志不轮换等于假装没泄漏。

    set +x; NEW=$(openssl rand -hex 24); umask 077
    printf 'POSTGRES_PASSWORD=%s\n' "$NEW" > /opt/temporal/.env
    chmod 600 /opt/temporal/.env; unset NEW
    sed -i 's/^\+ PGPW=.*/+ PGPW=<REDACTED-rotated>/' /var/log/temporal-bootstrap.log
    grep -cE '[0-9a-f]{48}' /var/log/temporal-bootstrap.log   → 0  ✅

模板已修:生成秘密的代码段包在 `set +x` … `set -x` 里。
**教训:`set -x` 与「写秘密」不能共存。开了 trace 又 tee 到文件,
等于把每个中间值都写进磁盘。**

**③ 另一处隐患顺手补了。** `Type=oneshot` 的默认 `TimeoutStartSec` 是 90s,
而首次要经 NAT 拉三个镜像(auto-setup 数百 MB)。这次失败得太快(2 秒,
manifest 不存在)所以没撞上,但迟早会撞。已加 `TimeoutStartSec=900`。

**⑥ 回滚**:

    aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-temporal

⚠️ **会连带删掉那块 50GiB EBS**(`DeleteOnTermination: true`),
**workflow 历史在那块盘上**。要留就先导出。
⚠️ 销毁类操作由用户执行。

---

### 4.4 Aurora 全局数据库(跨 region 复制)

**日期**:2026-09-24

**① 目标**:把主站 `serviceseks2-databaseb269d8bb-efjeyzicx2ak` 变成全局数据库的
主集群,并在 ap-northeast-2 建 `db.serverless` 从集群。

**⚠️ 分工是刻意的**:全局集群用 **CLI** 建,韩国从集群用 **CFN**。

`AWS::RDS::GlobalCluster` 的 `SourceDBClusterIdentifier` 语法上能把生产主集群
「收养」进我的栈 —— 但那样**删我的栈时 CFN 会去处置一个不属于它的生产集群**,
语义不可控。所以边界划在:全局集群(包住生产)= 一次性 CLI 操作;
韩国从集群(完全属于灾备侧)= CFN 管。本栈删除只影响韩国侧。

**② 前置状态**:现有全局集群数 = **0**;主集群 `available`,ACU 下限 1。

**③ 实际执行的命令**:

    # ③a 建全局集群（参数名用 botocore 服务模型核实过，不是猜的）
    aws rds create-global-cluster --region ap-northeast-1 \
      --global-cluster-identifier petsite-global \
      --source-db-cluster-identifier \
        arn:aws:rds:ap-northeast-1:926093770964:cluster:serviceseks2-databaseb269d8bb-efjeyzicx2ak

    # ③b 韩国从集群
    aws cloudformation deploy --region ap-northeast-2 \
      --stack-name dr-korea-aurora \
      --template-file infra/dr-korea/04-aurora-dr.yaml \
      --no-fail-on-empty-changeset

⚠️ 指定 `--source-db-cluster-identifier` 时**不要**再传 `--engine` /
`--engine-version` / `--storage-encrypted` —— 那些从源集群继承。

**④ 生效核实**(看**成员关系**这个独立事实,不看 deploy 返回):

    aws rds describe-global-clusters --global-cluster-identifier petsite-global \
      --query 'GlobalClusters[0].GlobalClusterMembers[].{arn:DBClusterArn,writer:IsWriter}'

    →  arn:...ap-northeast-1:...serviceseks2-databaseb269d8bb-efjeyzicx2ak   True
       arn:...ap-northeast-2:...dr-korea-aurora-secondarycluster-5ctcqnmbkro4  False

**两个成员、一个 writer 一个 reader,这才是跨 region 复制已建立的证据。**

韩国侧的独立确认:

    集群  dr-korea-aurora-secondarycluster-5ctcqnmbkro4
          available / MinCapacity 0.5 / MaxCapacity 4.0 / global=petsite-global
    实例  dr-korea-aurora-secondaryinstance-qudkyrrvn3ca
          db.serverless / available / PubliclyAccessible=False

用户要的「数据库同步 + 较小的实例」两条都落实了:
韩国 **0.5 ACU** vs 主站 **1.0 ACU**。
(AWS 建议的 8 ACU 只约束**主** region,从集群不受此约束 —— 所以从集群可以更小。)

**⑥ 回滚**(逆序,**销毁类由用户执行**):

    # 先删从集群（栈），再解散全局集群
    aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-aurora
    # 等栈删完后
    aws rds delete-global-cluster --global-cluster-identifier petsite-global

⚠️ `delete-global-cluster` 只解散「全局」这层包装,**不删主集群**。
但顺序反了会失败(有成员时不能删全局集群)。

---

### 4.5 韩国 EKS(守夜灯:控制面常开,零节点)

**日期**:2026-09-24

**① 目标**:`dr-korea-petsite`,ap-northeast-2。

**② 前置状态**:该 region 0 个 EKS 集群。

**③ 实际执行的命令**:

    aws cloudformation deploy --region ap-northeast-2 \
      --stack-name dr-korea-eks \
      --template-file infra/dr-korea/03-eks.yaml \
      --capabilities CAPABILITY_IAM \
      --no-fail-on-empty-changeset

三个值是**实测主站后对齐的,不是猜的**:

    版本       1.35                        （主站 PetSite 实测）
    AmiType    AL2023_ARM_64_STANDARD      （主站两个节点组实测；t4g 是 ARM，
                                             填 x86_64 会让节点起不来）
    实例类型    t4g.xlarge                  （与主站同规格）

**④ 生效核实**(三层,第三层是与配置**无关**的独立事实):

    ①  集群     status=ACTIVE  ver=1.35
                endpointPublicAccess=false  endpointPrivateAccess=true
                authenticationMode=API
    ②  节点组   status=ACTIVE  ami=AL2023_ARM_64_STANDARD  t4g.xlarge
                scalingConfig = { minSize 0, desiredSize 0, maxSize 3 }
    ③  真的零节点：
        aws ec2 describe-instances --region ap-northeast-2 \
          --filters "Name=instance-state-name,Values=running,pending"
        →  只有 dr-korea-temporal (t4g.large)，**没有任何 EKS 节点**

**③ 才是「守夜灯真的是暗的」的证据** —— 节点组配置说 desired=0 是一种说法,
EC2 列表里没有节点是另一回事。两者都要看。

#### 两处设计决定

**⑴ `AuthenticationMode: API` 而不是 aws-auth ConfigMap。**
ConfigMap 那套要 `kubectl` 才能改,而公网 endpoint 关着的时候改 ConfigMap
**本身就需要先能进去** —— 那是个死锁。API 模式用 IAM 授权,从外面就能加人。

**⑵ 节点角色带了 `AmazonSSMManagedInstanceCore`。**
无公网入站下 SSM 是唯一进得去节点的通道。

#### ⚠️ 一个还没解决的依赖:拆掉 NAT 之后节点拉不了镜像

现在节点(将来拉起时)靠 NAT 访问 ECR / S3。清单里说「装完 Temporal 后可以拆
NAT 省 41/月」—— 但**拆了 NAT,EKS 节点就拉不了镜像**。

若要拆 NAT,必须先补这些 VPC 端点:`ecr.api`、`ecr.dkr`、`s3`(网关型)、
`sts`、`elasticloadbalancing`、`autoscaling`。那是约 5 个接口端点
(~7-8/月 each)+ 1 个免费的网关端点 —— **算下来比 NAT 还贵**。

**结论:NAT 保留。** 清单里「拆 NAT 省 41/月」这条对**只有 Temporal**的阶段
成立,对有 EKS 的完整站点不成立。已如实更正。

**⑥ 回滚**:`aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-eks`
(先删节点组再删集群由 CFN 自己排序)

---


### 4.6 AgentCore 前置资源(`dr-korea-agentcore-prereq`)—— 首次失败,已重建成功

**日期**:2026-09-24

#### ① 目标

给 temporal-mcp 的 AgentCore Runtime 备好三样前置资源:代码包 S3 桶、
执行角色、runtime 用的安全组。runtime 本体在 `06-agentcore-runtime.yaml`,
**分两个栈是因为顺序是硬的** —— runtime 在**创建时**就要去 S3 读代码包,
所以桶必须先存在、zip 必须先上传。

#### ② 前置状态

| 项 | 值 | 怎么查到的 |
|---|---|---|
| AgentCore 支持 VPC | `networkMode: VPC` + `networkModeConfig{securityGroups,subnets}` | 读 ap-northeast-1 已 READY 的 `graph_dependency_mcp` |
| 支持 Node | `runtime` 枚举含 **`NODE_22`** | `botocore` 服务模型,不是猜的 |
| MCP 契约 | `0.0.0.0:8000` + `POST /mcp` + stateless + 不得拒 `Mcp-Session-Id` | 官方文档 |
| `entryPoint` 形式 | **`.js` 相对路径,无 `node` 前缀** | 官方文档例子 `["app.js"]` |
| 架构 | **只支持 arm64** | 官方文档;本包纯 JS,`.node` 引用数 0 |
| Temporal 安全组 | 入站 7243 放行 **10.20.0.0/16 整段** | `describe-security-groups`,所以 runtime 无需被显式放行 |

#### ③ 实际执行的命令

```bash
# 打自包含 bundle（依赖必须打进去，zip 里没有 node_modules）
cd /home/ec2-user/works/temporal-mcp
npx tsup --config tsup.agentcore.config.ts
printf '{\n  "type": "module"\n}\n' > dist-agentcore/package.json
cd dist-agentcore && chmod 644 agentcore-http.js package.json
zip -q temporal-mcp.zip agentcore-http.js package.json

# 前置栈
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-agentcore-prereq \
  --template-file infra/dr-korea/05-agentcore-prereq.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

#### ④ 生效核实

**本次没有通过。** 栈进 `ROLLBACK_COMPLETE`。
核实用的是「查资源到底存不存在」而不是看 deploy 的返回:

```
aws s3api head-bucket → 404 Not Found
aws iam get-role      → NoSuchEntity
```

两个资源**实际都不存在**。`CodeBucket` 在 `describe-stack-resources` 里
显示 `DELETE_SKIPPED`,那只是因为我写了 `DeletionPolicy: Retain` ——
它根本没建成,没有任何东西被保留。**这正是「栈事件里的状态词不等于资源真实状态」
的一个例子。**

#### ⑤ 失败过的做法

**把中文写进了 EC2 安全组的 `GroupDescription`。**

```
Value (temporal-mcp AgentCore runtime ? ???) for parameter GroupDescription
is invalid. Character sets beyond ASCII are not supported.
```

**EC2 的 `GroupDescription` 与规则 `Description`、IAM 的 `Description`
只接受纯 ASCII。** 但 CFN 自己的 `Parameters`/`Outputs` 描述接受 UTF-8 ——
栈 01~04 通篇中文都部署成功,证明约束在服务端而不在 CFN。
**修法:送到 API 的字符串一律 ASCII,中文解释留在 YAML 注释里。**

**另两个在实测中抓住的包装缺陷**(都不是 CFN 的问题):

1. **tsup 默认把 `dependencies` 当 external**,84KB 的 bundle 里没有 SDK。
   在空目录里跑立刻报 `ERR_MODULE_NOT_FOUND: Cannot find package
   '@modelcontextprotocol/sdk'`。修法:单独一份 `tsup.agentcore.config.ts`
   加 `noExternal: [/.*/]`,打成 728KB 自包含单文件。
   **不改主配置** —— 那会让 npm 包的产物也把依赖打进去,装两次。
2. **ESM 的 `.js` 没有 `package.json` 时模块类型不确定。** 实测在隔离目录里
   能跑(Node 22 有 ESM 语法探测),但那依赖 Node 的**次版本**,
   而 AgentCore 跑哪个次版本未知。修法:zip 里放一个
   `{"type":"module"}` 的 `package.json`(23 字节)。

#### ⑥ 回滚

栈是 `ROLLBACK_COMPLETE` 空壳,零资源。要用同名重建**必须先删掉它**
(CFN 不允许 deploy 到 `ROLLBACK_COMPLETE` 的栈):

```bash
# ⚠️ 销毁类命令 —— 按纪律只记录不自动执行
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-agentcore-prereq
```

删它是安全的:两个资源都没建成(已用 `head-bucket` / `get-role` 独立核实)。

#### ⑦ 重建结果

2026-09-24 用户放行后删栈重部,成功。核实(与部署不同的手段):

```
head-bucket                 ✅ 存在
get-bucket-encryption       AES256
get-public-access-block     BlockPublicAcls=True
get-role 信任主体            bedrock-agentcore.amazonaws.com
安全组                       sg-0be34c74ee6211412
```

代码包上传后 `head-object` 确认 141235 字节 / SSE AES256。

---


### 4.7 temporal-mcp 部到 AgentCore(`dr-korea-agentcore-runtime`)

**日期**:2026-09-24

#### ① 目标

把 temporal-mcp 部到韩国的 AgentCore Runtime,让它能连 VPC 内的
Temporal(`10.20.1.125:7243`),从而 dr-plan-generator 不需要进 VPC
也能操作 Temporal —— 那台 Temporal 没有公网入口。

#### ② 前置状态

| 项 | 值 | 怎么查到的 |
|---|---|---|
| AgentCore 支持 VPC | `networkMode: VPC` | 读 ap-northeast-1 已 READY 的参照实现 |
| 支持 Node | `NODE_22` 在枚举里 | botocore 服务模型 |
| `entryPoint` | **`.js` 相对路径,无 `node` 前缀** | 官方文档 `["app.js"]` |
| MCP 契约 | `0.0.0.0:8000` + `POST /mcp` + stateless | 官方文档 |
| CFN 的 `ProtocolConfiguration` | **字符串枚举**,非 API 的对象形式 | `describe-type` schema |
| 架构 | 只支持 arm64 | 官方文档;本包纯 JS |

#### ③ 实际执行的命令

```bash
# 打包（一条命令做完四步，见下面「失败过的做法」）
# ⚠️ 打包脚本在 temporal-mcp 仓库里，不在本仓库
cd /home/ec2-user/works/temporal-mcp/scripts
bash build-agentcore-package.sh

aws s3 cp dist-agentcore/temporal-mcp.zip \
  s3://dr-korea-agentcore-926093770964-ap-northeast-2/temporal-mcp/temporal-mcp.zip \
  --region ap-northeast-2

cd /home/ec2-user/works/graph-dependency-platform
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-agentcore-runtime \
  --template-file infra/dr-korea/06-agentcore-runtime.yaml

# 换代码后让 runtime 生效（CFN 看不出 S3 内容变了，要显式更新）
aws bedrock-agentcore-control update-agent-runtime --region ap-northeast-2 \
  --agent-runtime-id temporal_mcp-PVm47eFoHk \
  --agent-runtime-artifact '{"codeConfiguration":{"code":{"s3":{...}},"runtime":"NODE_22","entryPoint":["agentcore-http.js"]}}' \
  --role-arn arn:aws:iam::926093770964:role/DrKoreaTemporalMcpRole-ap-northeast-2 \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{...}}' \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --environment-variables 'TEMPORAL_ADDRESS=http://10.20.1.125:7243,...'
```

#### ④ 生效核实

**三层,第三层才是真证据。**

1. 控制面:`get-agent-runtime` → `status: READY`、`NODE_22`、
   `entryPoint ['agentcore-http.js']`、`networkMode VPC`
2. 容器日志:`temporal-mcp AgentCore transport on 0.0.0.0:8000/mcp
   (tools: standard, 23 loaded)`
3. **端到端 `invoke-agent-runtime`,连发四次交替调用:**

```
#1 get_cluster_info  ✅ isError=False  # Temporal Cluster Info
#2 list_namespaces   ✅ isError=False  # Namespaces (2)
#3 get_cluster_info  ✅
#4 list_namespaces   ✅
```

**决定性证据:返回的 Cluster ID `811ac051-857b-46e8-8762-34ed81c34c74`
与之前在 EC2 上用 SSM 直接查到的完全一致** —— 证明韩国的 runtime 确实
连到了 VPC 内那台 Temporal,而不是连到了别的什么东西。

#### ⑤ 失败过的做法

**① 复用单个 transport 实例 —— 症状极具误导性。**

第一版在启动时建了**一个** `StreamableHTTPServerTransport` 并 connect 到
一个 Server,每个 HTTP 请求都塞进去。部署后:

```
控制面 status            READY
容器日志                 只有正常启动行，没有任何报错
每次 invoke              -32010 / "Received error (500) from runtime"
```

**只看日志查不出来。** 本地复现才定位到,请求内容完全相同时:

```
好请求 #1 → HTTP 200
好请求 #2 → HTTP 500
好请求 #3 → HTTP 500
```

SDK 的 stateless 传输不为多请求复用设计,平台先前某次探测把那唯一一次
用掉了。修法:每个 `POST /mcp` 新建一对 Server+Transport。
这也是 stateless 模式的正确用法,顺带消掉了并发请求在 JSON-RPC id 上
撞车的隐患。

**这条记进来是因为它是本项目第六次「命令成功但没生效」,
而且是最难查的一次** —— 前五次至少有报错或数据不对,这次三层里
前两层全是绿的。

**② 手工分步打包半途而废。** `tsup.agentcore.config.ts` 里 `clean: true`,
重新 build 会清掉 `dist-agentcore/`,而手工流程忘了重新写 `package.json`
和重新 zip,于是 `aws s3 cp` 报 `path does not exist`。
已在 temporal-mcp 仓库的 scripts 目录里写成 `build-agentcore-package.sh`,
一条命令做完四步。

**③ JMESPath 也不接受非 ASCII 键名。** 写 `--query 'X.{入站:...}'` 报
`Unknown token 入`。这是本次第二个 ASCII 约束(第一个是 EC2 的
`GroupDescription`)。

#### ⑥ 回滚

```bash
# ⚠️ 销毁类命令 —— 按纪律只记录不自动执行
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-agentcore-runtime
```

前置栈的桶是 `DeletionPolicy: Retain`,删前置栈不会带走代码包。
桶开了版本,代码包被覆盖后可以回到上一版 —— 那是 runtime 出问题时
最快的回滚路径。

#### ⑦ 成本

AgentCore Runtime 按用量计费,常态无调用时接近零。
S3 桶存 141KB 代码包,可忽略。

---


### 4.8 DR worker(`dr-worker.service` @ Temporal 那台 EC2)

**日期**:2026-09-24

#### ① 目标

在 Temporal 服务端同机跑起执行切换步骤的 Temporal worker,并把
「起 workflow → 走到决策点 → 人工 signal 放行 → 收尾」这条链在 dry_run 下
跑通。

**worker 放这台而非韩国/东京 EKS 的理由**:切换时 EKS 可能正是要被操作的
对象,放上面就出现「执行切换的东西依赖被切换的东西」;且守夜灯站点常态
`desired=0`,worker 放上面等于平时不存在。代价是这台成了单点,**已知取舍**。

#### ② 前置状态

| 项 | 值 | 怎么查到的 |
|---|---|---|
| 宿主系统 `python3` | **3.9.25** | 实测。temporalio 要 ≥3.10,所以不能用 |
| 谁依赖系统 python3 | `aws-cfn-bootstrap`、`ec2-utils` | `rpm -q --whatrequires`,**所以不许替换它** |
| 仓库可装 | python3.11 / 3.12 / 3.13 / **3.14** | `dnf list available`,最新是 3.14.7,**不是 3.17**(不存在) |
| temporalio | 1.33.0,`requires_python >=3.10` | PyPI JSON API |
| 轮子 | `cp310-abi3` + `manylinux_2_17_aarch64` | 同上;本机是 aarch64 |
| gRPC 端口 | **7233** 在听 | `ss -lntp`。7243 是 HTTP API、8080 是 UI |

#### ③ 实际执行的命令

```bash
# 事务预演：先证明是增量而非替换（关键，系统 python3 不能动）
sudo dnf install --assumeno python3.12 python3.12-pip
#   → Install 6 Packages，零 Removing / Replacing

sudo dnf install -y python3.12 python3.12-pip
sudo python3.12 -m venv /opt/dr-worker/venv
sudo /opt/dr-worker/venv/bin/pip install temporalio==1.33.0 boto3==1.40.47

# 代码与 systemd 单元经 SSM base64 下发到 /opt/dr-worker/app/
sudo systemctl enable --now dr-worker

# 计划正文的只读权限（独立栈，见 ⑤ 为什么不改 02）
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions \
  --template-file infra/dr-korea/07-worker-permissions.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

#### ④ 生效核实

**每一层都用与部署不同的手段。**

```
并存             系统 python3 仍是 3.9.25 / 新装 3.12.14
未被动过          rpm -q python3 aws-cfn-bootstrap ec2-utils 三个都在
依赖可用          不看 pip 说成功，而是 import：boto3 1.40.47 可导入
能连 Temporal     不看端口在听，而是真连：gRPC 已连上，namespace = default
服务在跑          不看 systemctl start 的返回，看 is-active = active
```

**决定性核实:任务队列上出现了 poller。** 这同时给阶段 B 那个判据补上了
完整的前后对照:

```
worker 存在前   keys: [effectiveRateLimit, versioningInfo]            ← 无 pollers 字段
worker 存在后   keys: [effectiveRateLimit, pollers, versioningInfo]   pollers n=1
                identity = 37109@ip-10-20-1-125.ap-northeast-2.compute.internal
```

**端到端(第三次,前两次的问题见 ⑤):**

```
status                     COMPLETED
WORKFLOW_TASK_TIMED_OUT    0 个
fetch_plan_body            verified=True   size 411, etag 3070760a…
scale_up_nodegroup         verified=True   current_scaling desiredSize=0
promote_database           verified=True   decision=ordered，东京 is_writer=true
verify_step                verified=None   「dry_run：未执行提升 —— 这不是失败」
```

决策点两个方向都验过:非法裁决 `"bogus-value"` 被忽略(query 仍显示
`decision: null`、仍在等),**没让 workflow 崩**;合法裁决 `ordered` 才推进。

#### ⑤ 失败过的做法

**① 用户提出「把宿主升到 3.17」—— 那个版本不存在。** 2026-09 最新正式版是
3.14(3.15 要到当年 10 月)。而且**更要紧的是不能「升级」系统 Python**:
`aws-cfn-bootstrap` 与 `ec2-utils` 依赖 3.9,替换掉会弄坏 CFN 的信号机制。
正确做法是并装一个额外解释器,AL2023 正是为此把它们打成可共存的包。
用 `dnf install --assumeno` 预演确认了零 Removing。

**② `max_concurrent_workflow_tasks=1` 造成队头阻塞。** 我当时想的是
「一次只做一个切换」,但**workflow task 并发 ≠ 并发切换数**:workflow task
是「推进一步状态机」的短任务。实测后果:队列上有一个永久失败的 workflow
(type 没注册,Temporal 无限重试它的 workflow task)时,唯一槽位被占住,
真切换被拖 3 分钟并留下 `WORKFLOW_TASK_TIMED_OUT` —— 把
`workflowTaskTimeout` 从 10s 调到 60s **照样超时**。
改成 10 后同样的测试 TIMED_OUT 降为 **0 个**。
「一次只做一个切换」的正确机制是 **workflow ID**(同 ID 运行中时默认重用
策略直接拒绝第二次启动)。

**③ 差点让 CFN 替换掉那台 Temporal 实例。** 给 worker 加 S3 只读权限时,
我先改了 `02-temporal.yaml` 的角色。变更集预览报:

```
TemporalInstance  AWS::EC2::Instance  Modify  Replacement: Conditional
  Target: UserData   RequiresRecreation: Conditionally
```

**真因:线上栈的 UserData 与模板早已不一致。** 本会话早期修过 02 的
UserData(口令被 `set -x` 打进日志那处、编造的镜像 tag),但当时是用 SSM
直接修活实例,**栈从未重新部署**。那台实例上跑着 Temporal 的 PostgreSQL
容器和 worker —— 被替换等于数据和服务一起没了。

**变更集没有执行。** 改用 `AWS::IAM::ManagedPolicy`(它的 `Roles` 收角色
**名字**,所以新栈能把策略挂到一个它并不拥有的角色上),完全不触碰 02。
⚠️ **UserData 的漂移仍在**:只要没处理,02 这个栈就不能碰。

**④ 两次猜错 HTTP API 的返回格式。** 以为完成事件的结果是
`{payloads:[{metadata,data}]}`,实际 `result` 是**已解码的对象列表**
(HTTP API 把 `json/plain` 直接解开了)。

**⑤ JMESPath 也不接受非 ASCII 键名** —— `--query '{资源:...}'` 报
`Unknown token 资`。本会话第二次犯同一个错(第一次是 EC2 的
`GroupDescription`)。

#### ⑥ 回滚

```bash
# ⚠️ 销毁类命令 —— 按纪律只记录不自动执行
sudo systemctl disable --now dr-worker
sudo rm -rf /opt/dr-worker /etc/systemd/system/dr-worker.service
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions
```

并装的 python3.12 可以留着,它不影响任何现有东西。

#### ⑦ 真切换还缺什么

**刻意没给的写权限**:`eks:UpdateNodegroupConfig`、
`rds:FailoverGlobalCluster`、`elasticloadbalancing:*`、
`route53:ChangeResourceRecordSets`。按步骤逐个放开;
`FailoverGlobalCluster` 的「有序 vs `--allow-data-loss`」已做成需 signal
放行的决策点。

---


### 4.9 消除 `dr-korea-temporal` 的 UserData 漂移

**日期**:2026-09-24

#### ① 目标

4.8 节留下的债:线上栈与模板不一致,任何对该栈的 deploy 都会触发
`TemporalInstance ... Replacement: Conditional`,而那台实例跑着 Temporal 的
PostgreSQL 和 DR worker。目标是消除漂移且**不丢数据**。

#### ② 前置状态 —— 漂移是两层,不是一层

| 层 | 线上栈 | 模板 |
|---|---|---|
| 参数 `TemporalVersion` | **1.29.0**(不存在的 tag) | 1.29.7 |
| 参数 `TemporalUiVersion` | **2.42.0**(不存在的 tag) | 2.54.1 |
| UserData 正文 | 无 `set +x` 保护(会把口令打进日志) | 有 |

而**实际跑着的容器**是 1.29.7 / 2.54.1(当初用 SSM 直接改的)。三者互不一致。
后果:一旦实例被真正重建,既会重新泄漏口令,又会因 tag 不存在而起不来。

#### ③ 关键判断:重启还是替换 —— 查官方文档而不是猜

`AWS::EC2::Instance` 的 `UserData`:

> If the root volume is an **EBS** volume and you update user data, CloudFormation
> **restarts** the instance. If the root volume is an instance store volume,
> the instance is **replaced**.
> *Update requires*: Some interruptions

实测该实例 `RootDeviceType = ebs` → **重启**。变更集里的 `Conditional`
指的就是「取决于根卷类型」,不是「可能会替换」。

这条查清之后,整个风险评估反转了:原以为必须重建主机,实际只是一次重启。

#### ④ 实际执行的命令

```bash
# 先让 UserData 幂等（理由见 ⑤②）
# 然后两次 deploy —— 第二次必须显式覆盖参数，理由见 ⑤①

aws cloudformation deploy --region ap-northeast-2 --stack-name dr-korea-temporal \
  --template-file infra/dr-korea/02-temporal.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --no-execute-changeset
# 看过变更集确认只有 TemporalInstance 一项后才执行
aws cloudformation execute-change-set --region ap-northeast-2 --change-set-name <arn>

aws cloudformation deploy --region ap-northeast-2 --stack-name dr-korea-temporal \
  --template-file infra/dr-korea/02-temporal.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides TemporalVersion=1.29.7 TemporalUiVersion=2.54.1 \
  --no-execute-changeset
aws cloudformation execute-change-set --region ap-northeast-2 --change-set-name <arn>
```

#### ⑤ 失败过的做法

**① 以为模板的 `Default` 会修正已存在栈的参数值 —— 不会。**
第一次 deploy 后参数**仍是 1.29.0 / 2.42.0**:`aws cloudformation deploy`
不带 `--parameter-overrides` 时**沿用现有值**(UsePreviousValue),
模板的 `Default` 只对新栈生效。所以漂移只修了一半,必须显式覆盖。

**② 差点因为 UserData 重跑而换掉数据库口令。** PostgreSQL 的数据是
bind mount 到 `/opt/temporal/pgdata`,库已用旧口令初始化过;原来的 UserData
无条件 `openssl rand` 生成新口令写进 `.env`,一旦重跑就让 Temporal 连不上
自己的库,而且报的是**认证失败**,看起来像配置写错而不像「口令被换了」。
已改成 `if [ ! -f /opt/temporal/.env ]` 守卫 —— cloud-init 的 user-data 是
「每实例一次」所以重启本不会重跑,但把安全性押在那个语义上不值得。

#### ⑥ 生效核实

**零数据丢失,每一层都用与部署不同的手段。**

| 判据 | 改动前 | 两次重启后 |
|---|---|---|
| 实例 ID | `i-06f0a3e4961b8061e` | **同一个** |
| 私有 IP | `10.20.1.125` | **同一个**(AgentCore 的 TEMPORAL_ADDRESS 不用改) |
| `LaunchTime` | 10:28:09 | 14:27:29 → 证实重启过 |
| **`clusterId`** | `811ac051-857b-46e8-8762-34ed81c34c74` | **完全一致 → 库没被重建** |
| `pgdata` | — | 73M,数据在 |
| `.env` 修改时间 | 10:38:10 | **仍是 10:38:10 → UserData 没重跑** |
| `temporal` / `dr-worker` | active | **自己回来的**,均 active |
| 容器 | 1.29.7 / 2.54.1 | 不变 |
| worker | poller n=1 | **n=1,自己重新接单** |
| 引导日志里的口令 | 0 次 | 0 次 |
| AgentCore→Temporal | 通 | **通,Cluster ID 一致** |

参数值现已是 `1.29.7` / `2.54.1`,**漂移彻底消除**。

#### ⑦ 剩余的同类债

`/opt/dr-worker` 仍是用 SSM 手工装的,**不在 IaC 里**。重启能保留它,
但真正重建实例时它不会自动回来。要彻底消除这类漂移,worker 的
provisioning 应当进 UserData 或做成独立的配置管理步骤。

另外 `PrivateIpAddress` 在 `createOnlyProperties` 里 —— 想把
`10.20.1.125` 固定进模板(免得重建后 AgentCore 的地址失效)**本身就要求替换**,
所以那件事必须和「有计划的重建」一起做。

---


### 4.10 放开第一条写权限(`eks:UpdateNodegroupConfig`)

**日期**:2026-09-24

#### ① 目标

按「逐个放开」的次序给 worker 第一条写权限。选拉起节点组这一条,
因为它是切换里**最可逆**的动作:数错了把 `desiredSize` 改回去就行,
不像数据库提升那样一旦丢数据就没法回头。

#### ② 前置状态

| 项 | 值 | 怎么查到的 |
|---|---|---|
| 节点组 ARN | `…/dr-korea-petsite/dr-korea-workers/4ad06992-…` | `describe-nodegroup`。末段 UUID 是 EKS 生成的,**拼不出来** |
| 当前 scaling | `min0 / desired0 / max3` | 同上 |
| 实例类型 | `t4g.xlarge` | 同上 |
| 集群认证模式 | `API` | `describe-cluster` |
| 节点角色访问条目 | **存在**,`type=EC2_LINUX`、组 `system:nodes` | `describe-access-entry` |
| 节点角色策略 | CNI / WorkerNode / ECR 只读 / SSM 四个都在 | `list-attached-role-policies` |

后两项是**演练前必须查的**:`AuthenticationMode=API` 时节点靠访问条目加入
集群。条目缺失或类型写成 `STANDARD` 的表现是「节点起来了但永远不 Ready」——
又一个「命令成功但没生效」的形状。

#### ③ 实际执行的命令

```bash
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions \
  --template-file infra/dr-korea/07-worker-permissions.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

#### ④ 生效核实 —— 用不改任何东西的手段

`aws iam simulate-principal-policy` 能在**不调用真实 API** 的前提下证明
权限边界。对一条写权限来说,这比「真去调一次」安全得多。

| 动作 | 期望 | 实测 |
|---|---|---|
| `eks:UpdateNodegroupConfig`(该节点组) | 允许 | **allowed** |
| `eks:UpdateNodegroupVersion` | 拒绝 | `implicitDeny` |
| `eks:DeleteNodegroup` | 拒绝 | `implicitDeny` |
| `eks:CreateNodegroup` | 拒绝 | `implicitDeny` |
| `rds:FailoverGlobalCluster` | 拒绝(留到最后) | `implicitDeny` |
| `s3:GetObject` on `plans/` | 允许 | `allowed` |
| `s3:GetObject` on `temporal-mcp/` | 拒绝 | `implicitDeny` |
| `s3:PutObject` | 拒绝 | `implicitDeny` |

倒数第二条是刻意的:同一个桶里 `temporal-mcp/` 前缀放的是 MCP server 的
**可执行代码包**,worker 没有任何理由读它。

#### ⑤ 失败过的做法

`--query` 的输出用 `sed 's/…/…/'` 加管道时,模式里含 `/`(如 `plans/`)会让
`sed` 报 `unknown option to 's'`。改用 `s|…|…|`。小事,但它让两条核实结果
静默丢失 —— 如果没注意到输出缺了两行,会以为那两项没测。

#### ⑥ 回滚

```bash
# 只摘这条权限：把 07 模板里 ScaleUpPilotLightNodegroup 那段删掉再 deploy。
# 整条策略回滚（⚠️ 销毁类，只记录不执行）：
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions
```

#### ⑦ 还没做的:真演练

`desiredSize 0 → 2` 会**新建两台 t4g.xlarge**,属于新增计费资源,
按纪律要先问用户。演练脚本与核实方式已定:

- 执行:`dry_run=False` 只跑 `scale_up_nodegroup` 一步
- 核实:**`describe-instances` 数 running 节点**,不看
  `update-nodegroup-config` 的返回(它只表示请求被受理)
- 收尾:演练完把 `desiredSize` 缩回 0

门禁 `tests/test_83_worker_permission_boundary.py`(12 个)锁住边界:
还没放的不许提前出现、已放的不许用通配 Resource。

---


### 4.11 worker provisioning 进 IaC

**日期**:2026-09-24

#### ① 目标

消掉 4.9 ⑦ 留下的债:`/opt/dr-worker` 是用 SSM 手工装的,不在 IaC 里。
重启能保留它,但**实例真被重建时它不会自动回来** —— 而 worker 不在时,
切换 workflow 会一直排队并且**看起来是 RUNNING**(实测过),不报任何错。
那是最糟的失效形态:你以为切换在进行,其实什么都没发生。

#### ② 前置状态

worker 代码只存在于两处:本仓库 `dr-plan-generator/worker/`,
以及那台实例的 `/opt/dr-worker/app/`(经 SSM base64 下发)。
没有任何自动化路径把前者变成后者。

#### ③ 实际执行的命令

```bash
# 代码进 S3（worker/ 前缀，与 plans/ 和 temporal-mcp/ 分开）
aws s3 cp dr-plan-generator/worker/<each> \
  s3://dr-korea-agentcore-926093770964-ap-northeast-2/worker/ --region ap-northeast-2

# 加 worker/ 前缀的只读权限
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions \
  --template-file infra/dr-korea/07-worker-permissions.yaml \
  --capabilities CAPABILITY_NAMED_IAM

# UserData 里接上 provisioning（触发一次重启）
aws cloudformation deploy --region ap-northeast-2 --stack-name dr-korea-temporal \
  --template-file infra/dr-korea/02-temporal.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides TemporalVersion=1.29.7 TemporalUiVersion=2.54.1
```

#### ④ 设计:脚本幂等,所以能在不重建的前提下被真正验证

**一段只在实例重建时才跑的 provisioning 代码,等于一段没被验证过的代码。**
所以 `provision-worker.sh` 刻意做成幂等,既由 UserData 调用,也能在活主机上
直接重跑做修复。

在活主机上连跑两遍的结果:

```
第一遍  已有 python3.12，跳过安装 / 同步代码 / 依赖有变，安装 / 单元未变 / ✅ 已接单
第二遍  已有 python3.12，跳过安装 / 同步代码 / 依赖未变，跳过 / 单元未变 / ✅ 已接单
```

**幂等的决定性证据:`dr-worker` 的启动时间两遍前后都是 `14:31:00` 没变** ——
脚本没有做任何无谓重启。无谓重启会打断正在跑的切换。

脚本自己的核实步骤也守本项目的判据纪律:查的是 **`pollers` 字段是否存在**
而不是数量(字段缺失时服务端什么都没报,报成「0 个 worker」是把未测量写成
测量值);等不到 poller 时明说「这**不等于**没有 worker,也可能是 Temporal
还没起来」。

#### ⑤ 失败过的做法

**① 我写的两条门禁断言与实现不对齐。** 断言写成匹配字面量
`python3.12` 与 `venv/bin/python`,而脚本用的是变量 `"$PY"` 与
`"$VENV/bin/python"`。**这是按想象中的实现写判据,本项目最高频的错法。**
修法是照抄脚本真实文本。

**② 缩进判断错了一次。** 以为 UserData 正文是 12 空格(`sed 's/^/  /'` 的
前缀骗了我),把插入的段落又缩进了 2 格;实际是 10 空格。
教训:**用带前缀的方式看缩进,然后又按看到的宽度去改缩进,前缀会被算进去。**

#### ⑥ 生效核实

重启后,每层都用与部署不同的手段:

| 判据 | 结果 |
|---|---|
| 实例 ID / IP | `i-06f0a3e4961b8061e` / `10.20.1.125`,**未被替换** |
| `uptime` | `up 1 minute`,证实重启过 |
| **`clusterId`** | `811ac051-857b-46e8-8762-34ed81c34c74`,**仍一致 → 库没被重建** |
| `temporal` / `dr-worker` | 均 active,**自己回来的** |
| worker | `pollers: n=1` |
| `.env` 修改时间 | 仍是 `10:38:10` → UserData 没重跑 |
| `pgdata` | 73M |
| 引导日志里的口令 | 0 次 |

权限边界用 `simulate-principal-policy` 复核:

```
worker/worker.py            allowed
plans/e2e-test.md           allowed
temporal-mcp/*.zip          implicitDeny   ← 代码包，worker 无理由读
写 worker/                  implicitDeny   ← 能写自己的代码就等于能改变
                                             自己下次启动后的行为
```

#### ⑦ 回滚

把 UserData 里 provisioning 那段删掉再 deploy(又一次重启)。
`/opt/dr-worker` 会留着,不受影响。

#### ⑧ 剩余同类债

`PrivateIpAddress` 仍未固定。它在 `createOnlyProperties` 里,
把 `10.20.1.125` 写进模板**本身就要求替换实例**,所以必须和一次
有计划的重建一起做。在那之前,实例若被替换,AgentCore 的
`TEMPORAL_ADDRESS` 会指向一个不存在的地址。

---


### 4.12 AgentCore 的 IP 耦合检查 + 一个会骗过反向验证的陷阱

**日期**:2026-09-24

#### ① 目标

AgentCore runtime 的 `TEMPORAL_ADDRESS` 里写的是 Temporal 实例的**私有 IP**。
这个耦合有一条安静的失效路径:

    实例被替换 → 新 IP → AgentCore 仍指向旧地址 → 所有 MCP 工具调用超时

而超时的表现是「工具调不通」,看起来像 AgentCore 挂了、像网络不通、
像 Temporal 挂了 —— **唯独不像「地址过期了」**。排查会绕很久。

`PrivateIpAddress` 在 `createOnlyProperties` 里,把 IP 固定进模板本身就要求
替换实例;在做那次有计划的重建之前,这个检查是唯一能及早发现耦合断裂的手段。

#### ② 前置状态 —— 四处 IP 当前一致

```
实例实际 IP          10.20.1.125   （describe-instances）
栈输出 PrivateIp     10.20.1.125
AgentCore 的地址     http://10.20.1.125:7243
06 模板的默认值      http://10.20.1.125:7243
```

#### ③ 实际执行

新增 `scripts/check_dr_ip_coupling.py`。真跑结果:

```
✅ ok: AgentCore 指向 http://10.20.1.125:7243，与实例当前 IP 一致
```

**它走「栈 → 实例 → describe-instances」而不是读栈的 Output** ——
Output 是栈上次更新时的值,实例的 describe 才是当下的事实。
本项目已实测六次「命令成功但没生效」,这类差别正是那些案例的来源。

#### ④ 判据:三态,而且退出码分开

| 结论 | 退出码 | 含义 |
|---|---|---|
| `ok` | 0 | 一致 |
| `mismatch` | 1 | **确认**不一致,需要处置 |
| `inconclusive` | 2 | 拿不到某一侧的值,**无法判断** |

第三态是重点。拿不到值就判 `mismatch` 会制造假告警,而**假告警会让人开始
忽略这个检查 —— 那时它就等于不存在了**。本项目同类缺陷已四次,这里不再犯第五次。

#### ⑤ 失败过的做法 —— 一个会骗过反向验证的陷阱

做反向验证时,我先用 `sed` 把 `HTTP_API_PORT = 7243` 改成 `8080`(跑出 pyc),
再用 `cp` 还原。结果**还原之后测试仍然挂**,而磁盘上的文件明明是 7243。

真因:**Python 判断 `.pyc` 是否过期用的是「源文件 mtime + 大小」。**
两次操作在同一秒内,而 `7243` 与 `8080` **长度完全相同** —— mtime 与 size
都对得上,那个 8080 版本的 pyc 被当成有效,加载出来的模块常量还是 8080。

后果比一条测试挂掉严重得多:

> **任何「改一下 → 跑测试 → 还原」的反向验证流程都可能被它骗过**,
> 让人以为守卫有效或无效。而本项目的纪律正是「修完要写能抓到它的测试
> 并反向验证」——这个陷阱直接打在那条纪律上。

两处处置:

1. 测试的 fixture 里主动删掉同名 pyc 再加载(`test_85` 已做),
   这样门禁不受外部状态影响。
2. 反向验证的操作流程里,**还原后加一次 `touch`** 打破「mtime 相同」的条件。

#### ⑥ 回滚

脚本是只读检查,删掉即可,不影响任何运行中的东西。

#### ⑦ 剩余

`PrivateIpAddress` 仍未固定。彻底解法有两条:
- 固定 IP(要求替换实例,须配合有计划的重建)
- 或改用 Route53 私有托管区的 DNS 名(新增计费资源,须先问用户)

在那之前靠本检查兜住。

---


### 4.13 节点组扩容真演练(`dry_run=False`,只放行一步)

**日期**:2026-09-24 ·  **用户已放行在韩国 region 开资源做验证**

#### ① 目标

第一次让 worker **真调 AWS**:把守夜灯节点组从 0 拉到 2,核实整条
「起 workflow → worker 真执行 → 独立核实」的链路。

#### ② 前置:先补一个安全机制,否则这个演练本身是危险的

原来只有一个全局 `dry_run` 开关。做节点组演练时把它设成 `False`,
**同一次运行就会把 `promote_database` 也真执行** —— 那是切换生产数据库。
一次节点组演练绝不该有能力做那件事。

所以加了**按步骤放行**:只有名字出现在 `execute_steps` 里的步骤才真执行,
其余一律 dry_run,即使 `dry_run=False`。两道闸门(全局开关 **且** 名字在
清单里)是刻意的 —— 单独任何一个被误设都不足以让危险步骤真跑。
漏写的后果是「那一步没真跑」(安全),而不是「意外跑了」(危险)。

#### ③ 实际执行

```bash
# 只放行 scale_up_nodegroup 一步
PAYLOAD='{"plan_ref":"e2e-test","dry_run":false,
          "execute_steps":["scale_up_nodegroup"],
          "decision_timeout_seconds":900}'
# → POST /api/v1/namespaces/default/workflows/<id>
# 决策点发 abort —— 演练不碰数据库
```

#### ④ 生效核实

```
dry_run: False | executed_steps: ['scale_up_nodegroup']
fetch_plan_body      executed=False   ← 全局 dry_run=False，但不在放行名单里
scale_up_nodegroup   executed=True  verified=True  detail: {"running_nodes": 2}
decision: abort | aborted: True       ← 在数据库提升前停住
status: COMPLETED | TIMED_OUT 事件: 0
```

**按步骤放行机制得到验证**:全局开了 `dry_run=False`,`fetch_plan_body`
仍然没真跑,`promote_database` 连机会都没有。

独立核实(与执行不同的手段)—— `describe-instances` 数真实节点:

```
running 节点数 = 2
ap-northeast-2b  10.20.2.103  t4g.xlarge
ap-northeast-2a  10.20.1.6    t4g.xlarge
```

跨两个可用区,与节点组的子网配置一致。

#### ⑤ 收尾:缩回守夜灯状态

```bash
aws eks update-nodegroup-config --region ap-northeast-2 \
  --cluster-name dr-korea-petsite --nodegroup-name dr-korea-workers \
  --scaling-config minSize=0,desiredSize=0,maxSize=3
```

节点组回到 `min0/desired0/max3` 且 `ACTIVE`。

#### ⑥ 演练暴露的两件事

**① 缩容不是瞬时的,而且「还在 running」不等于「缩容失败」。**

EKS 更新报 `Successful`、ASG `DesiredCapacity=0`,但 EC2 里仍有一台
`running`。真相在 **ASG 的生命周期状态**里:

```
LifecycleState: Terminating:Wait
Terminate-LC-Hook  transition=EC2_INSTANCE_TERMINATING
                   HeartbeatTimeout=1800  DefaultResult=CONTINUE
```

托管节点组会挂一个排空钩子,上限 **30 分钟**。所以:

> 缩容期间 `describe-instances` 显示 `running`,与「缩容没生效」
> **在 EC2 这一层无法区分** —— 区分的信号在 ASG 的 `LifecycleState`,
> 不在 EC2 的 `State`。

已核实节点组 `health.issues` 为空、私有 endpoint 开启、VPC DNS 开启,
所以不是加入失败。

**② 我的核实测的是「ASG 扩了没」,不是「集群有没有可用容量」。**

`scale_up_nodegroup` 的核实是数 EC2 实例数。那证明 ASG 扩容成功,
**但不证明集群获得了可调度容量** —— 节点可能起来了却没成为 `Ready`。
对灾备切换来说这个差别极大:你可能有两台 EC2 和零个可调度节点。

为什么暂时没做到:集群 `endpointPublicAccess=false`,从 VPC 外面查不到
k8s 节点状态。**正确的修法是让 worker 去查** —— 它就在 VPC 内,
能访问私有 endpoint。列为后续工作,并已在活动代码里记下这个局限。

#### ⑦ 回滚 / 清理

```bash
# 残留实例会在排空钩子超时（≤30 分钟）后自动终止。
# 想立刻结束（⚠️ 销毁类，只记录不执行）：
aws autoscaling complete-lifecycle-action --region ap-northeast-2 \
  --auto-scaling-group-name eks-dr-korea-workers-4ad06992-7957-b47d-7adf-72656f7ffdf0 \
  --lifecycle-hook-name Terminate-LC-Hook \
  --lifecycle-action-result CONTINUE --instance-id <id>
```

---


### 4.14 把扩容核实从「数 EC2」升级为「数 Ready 的 k8s 节点」

**日期**:2026-09-24

#### ① 目标

补掉 4.13 ⑥② 记下的核实缺陷:原来数 EC2 实例,那只证明 ASG 扩容成功,
**不证明集群获得了可调度容量**。对切换来说差别极大 —— 你可能有两台 EC2
和零个可调度节点,而步骤会报 `verified=True` 继续往下走。

#### ② 三个前置条件缺一不可(逐个实测出来的,不是设计时想到的)

| 条件 | 缺了会怎样 | 怎么发现的 |
|---|---|---|
| 访问条目 | 调不通 k8s API | `list-access-entries` 里没有实例角色 |
| 控制面 443 入站 | **`curl` 超时** | 集群安全组原本只放行来自自己的流量 |
| 能读 nodes 的 RBAC | **403 forbidden** | `AmazonEKSViewPolicy` 的资源表里没有 `nodes` |

第二条值得单记:表现是 `Connection timed out` 而不是 `refused` ——
**安全组静默丢包的形状**。而 DNS 是正常的(解析到 `10.20.1.77` /
`10.20.2.77`,VPC 内的私有 endpoint ENI),所以必须把「DNS 不通」与
「端口不通」分开查,否则会往错的方向排查。

#### ③ 为什么不用 AWS 托管的访问策略

```
AmazonEKSViewPolicy       实测 403 —— 官方文档的资源表里**没有 nodes**
                          （全是 namespace 内的资源）
AmazonEKSAdminViewPolicy  是 */* 的 get,list,watch，官方文档原文：
                          「Note this includes Kubernetes Secrets」
```

给一个只需要知道「节点 Ready 了没」的 worker 读全集群 Secret 不可接受 ——
切换后那个集群里会有 petsite 的真实凭据。

所以自定义了一个**只含 `nodes` 的 ClusterRole**,绑到 `dr-node-readers` 组。
不给 `watch`(会让只读身份长期占着 API server 连接)、不给 `pods`
(spec 里常带环境变量名之类的信息)。

**为什么绑组而不是用户名**:访问条目的 username 是
`arn:aws:sts::…:assumed-role/<role>/{{SessionName}}`,而 SessionName 运行时
才定(实测是实例 ID)。绑到会变的用户名上绑不住;组名稳定。

#### ④ 实际执行的命令

RBAC 需要 cluster-admin,而集群 endpoint 是私有的、操作方在 VPC 外。
用一次**引导式临时提权**:

```bash
# ① 临时给 worker 角色 cluster-admin
aws eks associate-access-policy --region ap-northeast-2 \
  --cluster-name dr-korea-petsite \
  --principal-arn <worker-role-arn> \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster

# ② 从实例上 POST ClusterRole + ClusterRoleBinding（不需要 kubectl：
#    aws eks get-token 出 bearer token，curl 直连私有 endpoint）
#    → 两个都 HTTP 201 Created

# ③ **立刻**摘掉
aws eks disassociate-access-policy --region ap-northeast-2 \
  --cluster-name dr-korea-petsite --principal-arn <worker-role-arn> \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy
```

摘完核实 `list-associated-access-policies` 返回**空数组** —— 现在权限只来自
`dr-node-readers` 组 + 自定义 ClusterRole。

#### ⑤ 生效核实 —— 最小权限的两侧都验

```
列节点     HTTP 200  kind: NodeList  节点数: 0   ← 守夜灯状态，正确答案
读 Secret  HTTP 403  Forbidden                  ← 只给了 nodes
```

在 worker 里直接调 `count_ready_nodes()` 的三态也真验过:

```
集群存在（零节点）    Ready 节点数: 0     错误: None
集群名不存在          Ready 节点数: None  原因: ResourceNotFoundException…
```

**第二行是关键**:查不到返回 `None` 而不是 `0`。返回 0 等于断言
「没有 Ready 节点」,那是把「没测到」写成测量值 —— 同类缺陷本项目已四次。

#### ⑥ 失败过的做法

**门禁断言写成「字符串不得出现」。** 我写
`assert "AmazonEKSSecretReaderPolicy" not in text`,但模板里提到它是在
**说明「未给它」**的注释里 —— 按字符串出现与否判断,等于禁止文档解释自己
为什么不用某样东西。改成只看 `PolicyArn:` 行上挂的是什么。
**这是本会话第四次「判据与实现不对齐」。**

**测试编号撞了两次。** `test_87` 与 `test_88` 都已被占用,最终用 90。
新增门禁前应当先 `ls tests/`。

#### ⑦ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-worker-k8s-readonly
# ClusterRole / ClusterRoleBinding 需另行删除（它们不由 CFN 管）
```

---


### 4.15 让门禁在 CI 里真的拦得住

**日期**:2026-09-24 ｜ **不是部署,是对「已部署结论」的可信度修复**

#### ① 发现

按红线②「查通过数而不只看没报错」核对 PR #9 的 CI 时对出来的:

```
main                   26 passed, 1263 skipped
加了 12 条纯离线门禁后   26 passed, 1275 skipped   ← 通过数一个没涨，+12 全进 skipped
```

**此前写的所有门禁在 CI 里一条都没执行过** —— test_80~86 那批约 72 条,
加上 test_90 的 12 条。它们只在有 Neptune 凭据的开发机上有效。

#### ② 根因

`tests/conftest.py`:

```python
@pytest.fixture(scope='session', autouse=True)
def cleanup_test_data(neptune_rca):   # ← 参数里要 neptune_rca
```

`autouse=True` 使它成为**每一个**测试的 setup 依赖,而 `neptune_rca` 里有
`assert isinstance(result, list)`。pytest 在第一个测试 setup 时急切求值它 →
没有 Neptune 就整套在 setup 阶段全灭,连「读一个 YAML、断言里面有某个字段」
这种纯离线门禁也一样。

清理逻辑本来就全套 `try/except`,所以把 client 解析挪到 `yield` 之后即可。
Neptune 可达时行为完全一致。

#### ③ 修复揭开了两层此前被吞掉的问题

**缺包。** workflow 手列 8 个包,而 `strands` 缺着 —— 40 个
`ModuleNotFoundError('strands')` 此前全被 setup 阶段的 ERROR 盖住。
改成装 `requirements-dev.txt`(仓库自己的权威清单):

```
手列 8 个包            1041 passed, 5 failed, 41 errors
-r requirements-dev    1069 passed, 1 failed,  0 errors
```

手列必然漂移:workflow 的注释里就记着有人从 CI 日志逐个追加了
pydantic/structlog/streamlit/moto 四个。它与真正的依赖清单没有机械联系。

**一条过期测试。** `test_59::test_m05` import `st_link_analysis`,
而那个包 2026-09-13 就被删了(缩放低于 0.625 时节点标签整体消失),
两个图谱页早已改用 `demo/components/graph_svg`。
它的 docstring 还写着「9_Interactive_Explorer.py 直接 import 这四个符号」——
而那一行现在是 `from components import graph_svg`。

两层遮蔽叠在一起才让它活到今天:开发机上残留着卸载前装的副本(本地绿),
CI 里所有测试卡在 setup(从没真跑)。已改成守 `graph_svg.render` 的真实签名,
外加「三个被删的包不许悄悄回来」。

#### ④ 下限从 24 上调到 1040

`GDP_OFFLINE_MIN_PASSED` 的注释自称「是这个 job 的全部价值所在」,
而它是 24、实际通过 26 —— 基本什么都不挡。修掉 conftest 后实测 1069,
设 1040 留余量。

#### ⑤ 一个反直觉的核实结果:门禁被它要抓的缺陷禁用了

反向验证时把 conftest 退回 `cleanup_test_data(neptune_rca)`,
`test_91` 的那一组**0 个挂靶**。不是正则写错 —— 是这一组自己的 setup
同样依赖那个 fixture,offline 处理器把它的 ERROR 转成了 skip。

**真正的机械防线是下限**:退化后通过数从 1069 掉到 26,低于 1040 →
pytest **退出码 1**(实测,绕开管道取的)。两条防线针对不同环境:
下限管 CI,`test_91` 那一组管有 Neptune 的开发机。

#### ⑥ 本轮判据错了三次

| 错法 | 真相 |
|---|---|
| 用 `AWS_ACCESS_KEY_ID=bogus` 模拟 CI 无凭据 | bogus 拿到 403,CI 是「凭据未解析到」,offline 处理器只认后者。测出 22 failed 全是假的 |
| 写 `DEMO` / `sys.path` 改 test_59 | 那文件没定义 `DEMO` 也没 `import sys`,真实名字是 `REQ` |
| 检测死包 import 用 `^\s*importlib\.…` | 实际是 `mod = importlib.import_module(…)`,调用在赋值号右边,锚到行首永不匹配 |

加上「门禁把自己举的反例判成违规」(排除自身才修好),
**判据类错误本会话已第八次,功能仍从未写错。**

---


### 4.16 带 Ready 核实的扩容真演练 + 缩容改看 ASG 生命周期

**日期**:2026-09-24

#### ① 扩容真演练(用新的 Ready 核实重跑)

上一次演练时 `count_ready_nodes` 还不存在,那次的 `verified=True` 其实只证明了
ASG 扩容成功。这次拿到了真证据:

```
step=scale_up_nodegroup
    executed=True  verified=True
    detail={"ready_k8s_nodes": 2, "running_nodes": 2}
```

`WORKFLOW_TASK_TIMED_OUT` 0 个、`ACTIVITY_TASK_FAILED` 0 个。
从 scheduled 到 completed **实测 48 秒**(含 `update_nodegroup_config`、
等 EC2 running、等节点 Ready)。

**独立核实**(与 workflow 不同的手段 —— SSM 上直查 k8s API):

```
HTTP 200   节点总数: 2
  ip-10-20-1-166.ap-northeast-2.compute.internal  Ready=True
  ip-10-20-2-38.ap-northeast-2.compute.internal   Ready=True
```

两条路径一致。`fetch_plan_body` 因 `plan_ref` 不存在而 `verified=False` +
「404 Not Found」—— 那是正确的:我们**测了**且确实不在,所以是 False 而非
inconclusive。

#### ② 缩容核实必须看 ASG 生命周期(活环境并排证据)

缩到 `desired=0` 之后:

```
ASG 层:  {'Terminating:Wait': 1}          ← 真相
EC2 层:  i-001ffcffddbe4b08b  running     ← 分不出来
         i-0555836edddf0d90e  terminated
```

排空钩子 `Terminate-LC-Hook` 的 `HeartbeatTimeout=1800`(30 分钟)、
`DefaultResult=CONTINUE`。**一台正在优雅排空的实例和一台缩容失败卡住的实例,
在 EC2 那一层长得完全一样。** 用 EC2 State 做判据,会把「正常排空中」报成
「缩容失败」,或者更糟:把「卡住了」报成「还在排空,再等等」。

三态也在活环境验过:节点组名给错 → `None` + 原因,**不是空字典**
(空字典会被读成「一台都没有了」)。

#### ③ 新增的权限与它的 `Resource: '*'`

`autoscaling:DescribeAutoScalingGroups`,**只能** `Resource: '*'`:

服务授权参考(`list_autoscaling.html` 的 Actions 表)里它的「资源类型」列是
**空的**,也没有任何 condition key —— 对比同页 `DeleteAutoScalingGroup` 是
`autoScalingGroup*`。空列意味着不支持资源级权限。

这是文档的硬约束,不是放宽标准。门禁的判据因此定成「理由在不在模板里」而不是
「值是不是 `*`」:直接禁 `*` 会让这条权限根本写不出来,而不留理由会让下一个人
把它当成「这里可以随便用 `*`」的先例。另有一条断言确保**别的语句仍然不许**
用 `*`。

线上核实(查 IAM 策略文档而非模板):

```json
{"Action": ["autoscaling:DescribeAutoScalingGroups"], "Resource": "*"}
```

#### ④ 本轮踩到的四个坑

| 坑 | 真相 |
|---|---|
| `2>/dev/null` 吞掉了 AccessDenied | ASG 查询「返回空」看起来像名字错,实际是**没权限**。自己给自己制造的盲区 |
| 用记忆里的 ASG 名 | 名字其实没变,但**该现查** —— `eks-<ng>-<uuid>` 的 uuid 随节点组重建而变 |
| 猜 Temporal REST API 的返回形状 | 我按 gRPC 的 `{payloads:[{data:base64}]}` 写,实际 REST **已解码**,`result` 直接是 list。`AttributeError: 'list' object has no attribute 'get'` |
| 猜文档 URL slug | `list_amazonec2autoscaling.html` 不存在,真名是 `list_autoscaling.html` |

`deploy` 还因策略有固定名而需要 `CAPABILITY_NAMED_IAM`(不是 `CAPABILITY_IAM`)。

#### ⑤ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
aws cloudformation delete-stack --region ap-northeast-2 \
  --stack-name dr-korea-worker-permissions
```

---


### 4.17 ECR 跨 region 复制 + 现有镜像一次性回填

**日期**:2026-09-25 ｜ **这是灾备方案里最致命的一个洞**

#### ① 为什么这是致命点

读东京 PetSite 活集群发现,6 个业务服务里 5 个用的镜像是

```
926093770964.dkr.ecr.ap-northeast-1.amazonaws.com/
  cdk-hnb659fds-container-assets-926093770964-ap-northeast-1:<sha256>
```

**镜像仓库和要灾备的那个 region 是同一个 region。** 东京挂了就拉不到镜像,
于是韩国节点扩起来了、数据库提升了,pod 却全都 ImagePullBackOff。

顺带确认了一件好事:东京 PetSite 的节点也是 `t4g.xlarge` /
`AL2023_ARM_64_STANDARD`,与韩国节点组**同架构**。若不同架构,复制过去也跑不起来。

#### ② 部署的两个栈(注意 region 不同)

| 栈 | region | 说明 |
|---|---|---|
| `09-ecr-replication.yaml` | **ap-northeast-1** | 复制配置是 **registry 级**,属于源 registry |
| `10-ecr-korea-repos.yaml` | ap-northeast-2 | 给一次性回填用(ECR push 不会自动建仓库) |

部署前核实过 `describe-registry` 返回 `rules: []` —— 这个资源是 registry 单例,
若已有配置会被覆盖。

查 CFN schema 得到的真实约束(不是从印象写的):
`ReplicationDestination` 的 `RegistryId` 是**必填**;
`FilterType` 的枚举**只有** `PREFIX_MATCH` 一个值。

#### ③ 关键事实:复制**不带走**已有镜像

官方文档(`AmazonECR/latest/userguide/replication.html`)原文:

> "Only repository content pushed or restored to a repository after replication
> is configured is replicated. **Any preexisting content in a repository isn't
> replicated.**"

**实测印证**:对一个早已存在的镜像调 `describe-image-replication-status`,
`replicationStatuses` 返回**空数组**。

所以「配完复制」和「镜像在灾备侧」是两件事。把它们当成一件做,
是这个功能最容易踩的坑:看到「配置成功」就以为镜像已经过去了。

#### ④ 一次性回填(7 个镜像)

本机 `sudo` 被禁(`no new privileges`),所以在韩国那台 Temporal 实例上做 ——
顺带好处是 push 在 region 内。工具用 `skopeo`(AL2023 仓库有 `2:1.22.2`,
registry 到 registry 拷贝,不需要 docker daemon)。

`aws ecr` 搬不了 layer(只能搬 manifest),所以必须用能搬 blob 的工具。

权限走**临时内联策略**(`EcrBackfillTemporary`):挂上 → 回填 → **立刻摘掉**,
摘完核实 `list-role-policies` 只剩 `dr-orchestrator-readonly`。

结果:**7 个全部成功、0 失败**。核实**比 digest** 而不是只看「拷贝成功」:

```
e849677d52a8  digest 一致 sha256:52af83bbd2885b98dda…
012a95fb34e6  digest 一致 sha256:2983db73c18055b27bc…
```

#### ⑤ 端到端验证:新推送真的会复制吗

「配置记录下来了」≠「复制会发生」。建了一个名字匹配 `PREFIX_MATCH` 前缀的
测试仓库 `pet-adoptions-history-repltest`,推一个镜像进去:

```
[15s] ap-northeast-2   COMPLETE
```

独立核实(与测试手段不同):

- **韩国侧仓库被自动创建了** —— 我从未在韩国建过这个仓库,印证文档说的
  「目标仓库在复制发生时自动创建」
- 两侧 digest 一致 `sha256:36b36613e2aa47f9b6939c7…`

测试完把两侧测试仓库都删了,生产用的两个仓库确认仍在。

#### ⑥ 本轮踩到的两个坑

**IAM 策略传播延迟。** 挂完 `put-role-policy` 立刻发 SSM 命令,skopeo 报
`initializing source docker://…` 失败。我一开始以为是 skopeo 或权限范围写错,
**真因是 IAM 最终一致性还没生效**。等 20 秒后同一条命令退出码 0。
**IAM 改完立刻用,失败信息不会告诉你「是因为还没传播」。**

**`2>&1 | cut -c1-160` 把错误截断了。** 第一次失败时我只留了 160 字,
关键部分刚好被切掉,于是判断方向错了。诊断时应当单独跑一次「只验证能不能读源」
并**完整输出 stderr** —— 这次就是这么定位到的。

#### ⑦ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
aws cloudformation delete-stack --region ap-northeast-1 --stack-name dr-ecr-replication
# 10-ecr-korea-repos 的仓库是 DeletionPolicy: Retain —— 删栈不会删掉回填的镜像。
# 真要删仓库需显式:
#   aws ecr delete-repository --region ap-northeast-2 --repository-name <name> --force
```

---


### 4.18 给 7 个 IRSA 角色补上韩国集群的 OIDC provider

**日期**:2026-09-25 ｜ **这是「只在真切换时才炸」的那一类缺陷**

#### ① 缺陷的形状

petsite 的 7 个业务服务全走 IRSA。信任链是:

```
pod → SA token（由**集群自己的** OIDC issuer 签发）
    → sts:AssumeRoleWithWebIdentity
    → IAM 校验签发者是不是一个**已注册的 OIDC provider**
```

实测 `list-open-id-connect-providers`:2 个 `ap-northeast-1` + 2 个 `us-west-2`,
**一个 `ap-northeast-2` 都没有**。

后果的形状很坏:把清单照抄到韩国,pod **能起来**、能过健康检查的前半段,
然后每一次 AWS 调用都 403。**切换前做静态检查完全看不见这个缺陷。**

#### ② 两个集群的 issuer

| | issuer |
|---|---|
| 东京 PetSite | `…ap-northeast-1…/id/D355BAF17E25A2395709BCD682D10AFD` |
| 韩国 | `…ap-northeast-2…/id/978D6D13181C50CF75450B933B976ADD` |

#### ③ 建 provider(`11-irsa-korea-oidc.yaml`)

从 API 模型查到 `CreateOpenIDConnectProvider` 的 required **只有 `['Url']`** ——
`ThumbprintList` 不必填,AWS 自己取。所以刻意**不写死指纹**:写死的值会随 CA
轮换而过期,而过期的表现也是 403,和「没注册」长得一样。

部完核实 AWS 取到的是 `06b25927c42a721631c1efd9431e648fa62e1e39` ——
与东京那个 provider 一致(同一个 Amazon 根 CA),算是个额外的 sanity check。

`Url` 在 `createOnlyProperties` 里,改它等于重建资源。

#### ④ 信任策略为什么不能用 CFN

那 7 个角色是 `ServicesEks2` / `Applications` 栈建的,**CFN 改不了自己不拥有的
资源**。所以走 `scripts/add_korea_irsa_trust.py`。

`iam:UpdateAssumeRolePolicy` **替换整个文档**,写错就是把东京生产站点的 IRSA
拆了。三条自保:

1. **只追加** —— 读出现有文档 → 判断有没有韩国那条 → 没有才 append。
   从不「按模板重新生成」一份
2. **改前备份** —— 每个角色的原始文档存成带时间戳的 JSON
3. **改后逐字核对** —— 读回来断言原有每一条都还在、只多了一条、
   韩国那条确实写进去了。任何一条不满足就报错退出

默认 dry-run,要 `--apply` 才真改,且 `--apply` 必须同时给 `--backup-dir`。

#### ⑤ 刻意与东京那条不同:加了 `:sub`

东京那条(CDK 生成的)**只限定 `:aud`** —— 也就是说该集群里**任何**
ServiceAccount 都能 assume 那个角色。我给韩国加的那条额外限定:

```
<issuer>:sub = system:serviceaccount:petadoptions:<sa-name>
```

没顺手把东京那条也收紧:那是别的栈管的资源,改它会和那个栈的下一次部署打架。

#### ⑥ 核实 —— 零节点就能验通

IRSA 的本质是「集群签发的 SA token 换 STS 凭据」,而 token 由**控制面**签发。
所以不需要起 pod、不需要节点、不需要找一个装了 AWS CLI 的镜像:

```
建 ns petadoptions                 HTTP 201
建 SA petsite-sa（带 role-arn）     HTTP 201
TokenRequest audience=sts…         HTTP 201，token 长度 977
sts assume-role-with-web-identity  ✅
  assumed: arn:aws:sts::…:assumed-role/Applications-PetSiteServiceAccount…/irsa-korea-verify
```

最后一步就是之前会 403 的那一步。

**双向验证**(只证明「能用」不够,还要证明 `:sub` 真的限制住了):

```
用 not-petsite-sa 的 token 换 petsite 的角色 → AccessDenied ✅（应当被拒）
用 petsite-sa   的 token 换 petsite 的角色 → 成功        ✅
```

**幂等验证**:脚本连跑两遍,第二遍「0 改动 / 7 跳过」。

**全量抽查**:7 个角色逐个确认「东京 1 条 + 韩国 1 条 + sub 与 SA 名一致」。

#### ⑦ 临时提权

建 ns/SA 需要 cluster-admin,而 endpoint 是私有的。沿用 4.14 的引导式提权:
临时关联 `AmazonEKSClusterAdminPolicy` → 操作 → **立刻摘掉**,
核实 `list-associated-access-policies` 返回空数组。

⚠️ 这次**主动等了 25 秒**再用 —— 上一轮(4.17)就是没等 IAM 传播而误判。

#### ⑧ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
# 信任策略:备份在 $KIROCREW_SCRATCH/irsa-backup/<role>.<stamp>.json
#   aws iam update-assume-role-policy --role-name <role> \
#     --policy-document file://<那个备份文件>
# provider 是 DeletionPolicy: Retain，删栈不会删它
```

---


### 4.19 放开 rds:FailoverGlobalCluster + 演练 signal 链路

**日期**:2026-09-25 ｜ **这是最后一条写权限**

#### ① 权限:三个 ARN,不是两个

查官方样例策略(`r53recovery/latest/dg/security_iam_region_switch_aurora.html`
的 Aurora Global Database execution block sample policy)得知,
`rds:FailoverGlobalCluster` **支持资源级权限**,且要带**三个** ARN:

```
arn:aws:rds::926093770964:global-cluster:petsite-global                    ← 全局集群
arn:aws:rds:ap-northeast-1:…:cluster:serviceseks2-databaseb269d8bb-…       ← 当前主集群
arn:aws:rds:ap-northeast-2:…:cluster:dr-korea-aurora-secondarycluster-…    ← 目标从集群
```

我原本只打算写全局集群 + 从集群。**漏掉主集群会让调用被拒,而报错只说没权限,
不会说少了哪个 ARN。**

另注:全局集群的 ARN **没有 region 段**(`arn:aws:rds::<acct>:…`,两个冒号连着)
—— 那不是笔误,全局集群不属于任何 region。

#### ② 一个 API 事实改了实现

查 botocore 模型:`FailoverGlobalCluster` 的 members 是
`['GlobalClusterIdentifier', 'TargetDbClusterIdentifier', 'AllowDataLoss', 'Switchover']`,
后两个**互斥**。文档写着「If you don't specify `AllowDataLoss`, the global
database cluster operation defaults to a **switchover**」。

原实现是「有序分支不传任何参数,靠 API 默认」。改成**两个分支都显式传**:

```python
if ordered:
    kwargs["Switchover"] = True
else:
    kwargs["AllowDataLoss"] = True
```

理由:这一步的整个设计前提是「有序 vs 丢数据」必须是一个**明确的裁决**,
而依赖一个 API 默认值恰好违背这一点。显式传参还让 CloudTrail 里能直接看出
当时是哪种语义,而不是「什么都没传,所以大概是 switchover」。

`would_run` 的命令串也跟着改成带 `--switchover` —— 否则 dry_run 打印的命令
和真执行路径**不是同一条命令**,而 `would_run` 的全部价值就在于「照着它跑能复现」。

#### ③ signal 链路演练(四种裁决)

全部在 `dry_run=True` 下跑,不真动数据库拓扑:

| 裁决 | 结果 |
|---|---|
| `ordered` | `verified=True`,`would_run` 带 `--switchover`,`aborted=False` |
| `allow_data_loss` | `verified=True`,`would_run` 带 `--allow-data-loss` |
| `abort` | `aborted=True`,0 个失败事件 |
| `bogus-value` | **被忽略**,workflow 不崩,继续等合法裁决 |

四种都是 0 个失败事件。`detail` 里带了全局集群成员的实时 `IsWriter`
(东京 `true` / 韩国 `false`),这是「提升前的基线」。

#### ④ 本轮抓到一个真缺陷:改了代码却没生效

演练第一遍打印出:

```
activities.py md5: 0ae29ad154c2      ← 与本地不一致
含显式 Switchover: 0
```

第一层原因是我**改了本地文件却没上传 S3**。上传后再跑:

```
activities.py md5: aa1f4bb158ee      ← 与本地一致 ✅
含显式 Switchover: 1                 ← 新代码在磁盘上 ✅
would_run: … 没有 --switchover       ← 行为还是旧的 ❌
```

**第二层原因才是真缺陷**:`provision-worker.sh` 原来**只在 systemd 单元变化时
重启**。应用代码变了不重启,而 **Python 进程还拿着内存里的旧模块**。

最坏的部分是它的核实判据:

```
磁盘 md5 与本地一致           ✅ 看起来部署成功
worker 在队列上接单           ✅ 核实判据也通过
实际跑的还是旧代码             ❌
```

那个判据(「worker 已在 dr-plan-queue 上接单」)在两种情况下都通过 ——
**又一个「分不出来」的判据,本项目同类问题第五次。**

修法:记录所有 `*.py` 的内容哈希(排序后拼接再哈希,与文件顺序无关;
**不用 mtime**,因为 `s3 sync` 会重写 mtime 而内容可能没变,那会导致无谓重启),
重启条件改成**单元变化 OR 代码变化**。

核实(三态都验了):

```
第一遍  应用代码未变 → 不重启，启动时间 07:17:54
第二遍  应用代码未变 → 不重启，启动时间 07:17:54   ← 幂等
人为改代码 → 「代码有变（… → 45f93c80…）」→ 重启 → 启动时间变了  ← 反向验证
```

#### ⑤ 刻意**没有**做的事

**没有真的提升韩国从集群。** 那会让它脱离全局数据库、把东京主库从 writer 变成
reader —— 属于「影响东京生产可用性」,须先问用户。
本轮只验到「worker 具备提升能力 + signal 链路正确」。

#### ⑥ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
# 收回这条写权限：把 07-worker-permissions.yaml 里 PromoteSecondaryCluster
# 那段删掉再 deploy（--capabilities CAPABILITY_NAMED_IAM）
```

---


### 4.20 固定 Temporal 实例的私有 IP（含一次有计划的实例重建）

**日期**:2026-09-25 ｜ **这是 provisioning 进 IaC 之后第一次真正被考验**

#### ① 消除的是什么耦合

AgentCore runtime 的 `TEMPORAL_ADDRESS` 里写着 Temporal 实例的私有 IP。
实例一旦被替换,IP 就变,而 AgentCore 那边不会自动跟着改 —— 表现是
temporal-mcp 的每次调用都超时,**而控制面显示 READY、日志里也没有明显报错**。

在此之前靠 `scripts/check_dr_ip_coupling.py` 兜住,但那只是**检测**,不是消除。

#### ② 一个必须先想清楚的陷阱:不能沿用当前 IP

`PrivateIpAddress` 在 `createOnlyProperties` 里,写进模板**本身就要求替换实例**。

而 CFN 替换资源是**先建新再删旧**。所以如果把模板里的 IP 写成**旧实例正占着的
`10.20.1.125`**,新实例创建时会因地址被占用而失败 —— 而那个失败发生在栈更新
中途,回滚起来比一次干净的替换麻烦得多。

所以选了 `10.20.1.10`:子网是 `10.20.1.0/24`(当时 245 个可用),
`.0`~`.3` 与 `.255` 是 AWS 保留,`.10` 当时空闲(逐个核对过子网内所有 ENI:
`.38`/`.77`/`.123`/`.125`/`.139`/`.170`)。

#### ③ 部署前用变更集确认动作,不靠推测

```
logical: TemporalInstance   action: Modify   replace: True
```

只影响这一个资源。

⚠️ `create-change-set` **没有** `--use-previous-parameters` 这个选项(我先写了它,
被 ValidationError 顶回来)。正确写法是逐个参数
`ParameterKey=<k>,UsePreviousValue=true`;新加的参数不写,让它用模板默认。

#### ④ 重建结果

| | 替换前 | 替换后 |
|---|---|---|
| 实例 ID | `i-06f0a3e4961b8061e` | `i-09380e417a0177ed4`(旧的已 terminated) |
| 私有 IP | `10.20.1.125` | **`10.20.1.10`**(固定) |
| Temporal cluster ID | `811ac051-…` | **`682cc7c5-fce2-47af-ac15-dd0e0902f7b6`** |

cluster ID 变了正是预期:库是重建的。Temporal 的 `retentionTtl` 本就 86400s,
丢掉的 workflow 历史是一天内的。

#### ⑤ 自动恢复核实 —— 零手工介入

```
temporal / dr-worker / docker        全部 active
3 个容器                             1.29.7 / 2.54.1 / postgres:16-alpine（无 tag 漂移）
Temporal HTTP API                    200，default namespace REGISTERED
venv                                 Python 3.12.14，temporalio 1.33.0
pollers                              n=1
端到端 dry_run workflow              COMPLETED，0 失败、0 WORKFLOW_TASK_TIMED_OUT
口令是否泄进日志                      0 命中（set +x 的保护在重建后仍生效）
```

`.code.sha256` 在新机器上算出来与旧机器一致 —— 顺带证明那个指纹是**内容哈希**、
跨机器可复现(而不是掺了 mtime)。

#### ⑥ 又一次踩到「`deploy` 不看模板 Default」

改完 `06-agentcore-runtime.yaml` 的默认值后 `deploy` 报
**No changes to deploy**。原因是 `deploy` 沿用现有参数值(`UsePreviousValue`),
模板的 `Default` **只对新建栈生效**。必须显式
`--parameter-overrides TemporalAddress=http://10.20.1.10:7243`。

**这是同一个坑第二次**(第一次是 4.x 的镜像 tag 漂移)。

#### ⑦ 耦合检查在这次替换里起了作用

替换后立刻跑 `check_dr_ip_coupling.py`:

```
❌ mismatch: AgentCore 指向 http://10.20.1.125:7243，但实例当前 IP 对应的
   应是 http://10.20.1.10:7243。所有 MCP 工具调用会超时，而超时看起来像
   网络或服务故障，唯独不像「地址过期」。
```

改完再跑:`✅ ok`。

#### ⑧ 决定性端到端核实

真调一次 AgentCore(它在 VPC 内),看它能不能连上新 IP 的 Temporal:

```
get_cluster_info → Cluster ID: 682cc7c5-…  Server Version: 1.29.7
```

⚠️ 调用时踩了两个坑:
- `invoke_agent_runtime` 缺 `accept` header 会返回 **406**。
  MCP 的 streamable HTTP 要求 `accept: application/json, text/event-stream`。
- 我猜工具名叫 `describe_cluster`,真名是 **`get_cluster_info`**
  (`tools/list` 列出来的 23 个工具里)。

#### ⑨ 回滚

```bash
# ⚠️ 销毁类命令 —— 只记录不执行
# 取消固定 IP：把 02-temporal.yaml 里 PrivateIpAddress 那行删掉再 deploy
#   —— 注意那**又是一次实例替换**，并且新 IP 是随机的，
#      06 的 TemporalAddress 要跟着改（--parameter-overrides，别指望 Default）
```

---


### 4.21 在韩国真起一次 petsite —— 以及一个差点写错的结论

**日期**:2026-09-25 ｜ **这一节最重要的部分是「对照组救了我」**

#### ① 先清点:region 内配置到底是什么

实测东京 `/petstore` 前缀下有 **41 个参数**,韩国 **0 个**。
但真正的问题不是「参数没复制」—— 看参数名就知道那些**值**指向东京的资源:

```
rdsendpoint / rds-reader-endpoint / rdssecretarn   东京 Aurora 与 Secrets
queueurl / snsarn / petadoptionsstepfnarn          东京 SQS / SNS / StepFunctions
dynamodbtablename / s3bucketname                   东京的表和桶
dataprotection/key-*  ×4（SecureString）            ASP.NET Data Protection 密钥
agent/waggleairuntimearn                           东京的 AgentCore runtime
```

**在「东京挂了」的场景下,把这些值复制到韩国等于让韩国去连一堆不存在的后端。**
真正的灾备需要韩国侧自己的 DynamoDB / SQS / SNS / StepFunctions / S3 / API GW,
或者用全局版本的服务 —— 那是一个独立的工作项,不是配置复制。

这把缺口分析第 ④ 条从「依赖面很大」变成了可估的清单。

#### ② 演练做了什么

扩一个节点(`t4g.xlarge`),用从东京读来的 `last-applied-configuration` 改造出
`infra/dr-korea/petsite-korea-drill.yaml`(镜像换成 ap-northeast-2、
replicas 1、去掉 CDK 的 prune 标签、`startupProbe.failureThreshold` 60→6),
经 k8s API 应用到 `petadoptions`。

#### ③ 证实了的事

```
pod 状态            Running，restarts=0，持续 140s
镜像                从 ap-northeast-2 的 ECR 拉起来的 ← 回填 + 复制的最终验证
启动日志            "Found credentials using the AWS SDK's default credential search"
                    ← IRSA 在 pod 里真的工作
SDK 解析的 region   ap-northeast-2  ← 清单里没有 AWS_REGION，SDK 走 IMDS 拿到韩国
SSM provider        "Systems Manager configuration added with prefix: /petstore"
                    ← 指向韩国，而韩国有 0 个参数
```

这是 ECR 复制与回填那条链路的**最后一环**:此前只证明了两侧 digest 一致,
现在证明了**韩国的 kubelet 能真的把它拉起来**。

#### ④ ⚠️ 我差点写错的结论 —— 对照组救了我

pod proxy 打出来:

```
/health/status   200  "Alive"
GET /            302
/adoptionlist    404
```

我本来要写「韩国的 petsite 返回 302/404,应用起不来」。
**做了东京对照组之后发现:东京一模一样。**

```
             /health/status   GET /   /adoptionlist
韩国            200 (5B)        302        404
东京（对照）     200 (5B)        302        404
```

所以那三个响应**什么问题都没证明**。`/adoptionlist` 那个 404 更是我自己猜的路径,
它说明的只是「我猜错了路由名」。

**没有对照组,我就会把一个错误结论写进灾备手册。**

#### ⑤ 真正的 DR 发现:失效形态不是崩溃

`/health/status` 返回的是硬编码的 `"Alive"`(5 字节),**完全不碰配置**。

所以在一个**零配置**的 petsite 上:

```
pod 状态        Running          ✅
restarts        0                ✅
readiness 探针   通过             ✅
k8s 层面的一切    全绿             ✅
```

**k8s 层面的灾备就绪检查在一个什么都没配好的 petsite 上是全绿的。**
这比崩溃坏得多 —— 崩溃会告警,而这个不会。

对我们自己的核实链路也是个提醒:`count_ready_nodes()` 只回答「集群有可调度容量」,
它**不回答**「应用能服务」。这两件事之间还隔着配置与后端依赖,
而中间没有任何一个自动判据能替我们跨过去。

#### ⑥ 不能下的结论

**没有证明 petsite 在韩国能服务用户,也没有证明它不能。** 现有探针在两个 region
上没有区分力 —— 要区分需要一个真正渲染后端数据的页面,而那需要应用的路由表。
按判据纪律,这一项是 **inconclusive**,不是「通过」也不是「失败」。

#### ⑦ 过程中的两个小坑

- 演练清单先放到 S3 的 `drill/` 前缀 → **403 Forbidden**。worker 角色只允许
  `plans/*` 与 `worker/*`。**处置是把文件挪进 `worker/`,不是为演练放宽权限边界。**
- 从 Temporal 实例直连 pod IP 全是 `HTTP 000` —— pod 的 ENI 挂的是集群安全组,
  只放行来自自己的流量。**那是「没测到」,不是「应用坏了」。**
  改走 k8s API 的 pod proxy(`/api/v1/namespaces/<ns>/pods/<pod>:<port>/proxy/`),
  用已验证过的控制面通路,不用改任何安全组。

#### ⑧ 清理

Deployment 已删(剩余 pod 0)、临时 cluster-admin 已摘(关联列表空)、
节点组缩回 `desired=0`、S3 上的演练文件已删、
sync 进 `/opt/dr-worker/app` 的演练文件已清。

顺带验证了一个早先的设计决定:代码指纹只哈希 `*.py`,所以那个 `.yaml` 被 sync
进 app 目录**没有**触发无谓重启(`.code.sha256` 未变、worker 未重启)。

---


### 4.22 补上第 8 个 IRSA 消费者(LB Controller)—— 并修掉让我漏掉它的根因

**日期**:2026-09-25 ｜ **手册第五节缺口③ 关闭**

#### ① 为什么这一项必须最先做

没有它,后面的入口工作全白做:LB Controller 拿不到凭据时的表现是
**「装上了、pod 起来了、一个 ALB 也不建」** —— 而灾备站点没有 ALB 就没有入口。

#### ② 做了什么

给 `ServicesEks2-LoadBalancerServiceAccountB6807779-QEjXooFf4b6b` 的信任策略
追加韩国 OIDC 那一条(2 → 3 条语句),`:aud` + `:sub` 双限定。

#### ③ 决定性证据(与部署不同的手段)

部署是脚本做的,所以**不能只看脚本自己的核对报告**。改用真做一次 token 交换:

```
韩国 kube-system/alb-ingress-controller 的 token（控制面签发，零节点也能签）
  → arn:aws:sts::926093770964:assumed-role/ServicesEks2-LoadBalancer…/korea-lbc-verify  ✅
同命名空间下一个 bogus SA 的 token
  → AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity               ✅
```

后者证明 `:sub` 限定真的生效 —— 不是「该集群任何 SA 都能 assume」。
反向验证用的 bogus SA 已删除(`DELETE HTTP 200`)。

#### ④ 修掉根因,而不只是补上漏项

漏掉它的根因不是忘了某一项,而是**命名空间隐含在脚本常量里**
(`NAMESPACE = "petadoptions"`),让人只会去想「petadoptions 下有哪些 SA」。

所以这次一并改了结构:

| 改动 | 为什么 |
|---|---|
| 映射从 `{sa: role}` 改成 `[{namespace, name, role, kind}]` | 让「这是哪个命名空间的」成为读映射时看得见的信息 |
| 删掉脚本里的 `NAMESPACE` 常量 | 它就是那个盲区 |
| `korea_statement()` 把 namespace 收成**必填位置参数** | 有默认值会把「忘了写」变成「静默用了 petadoptions」,而那个错误的表现是永远 403 |
| `kind: business\|infra` | 保留「7 个业务 SA」这个判据的语义,同时容纳基础设施 SA |

#### ⑤ ⚠️ 这条信任的影响面

托管策略 `ServicesEks2-LoadBalancerSAPolicy6C6E33B0-PNHgXHQuKjvt` 共 15 条语句,
其中 9 条 `Resource` 是 `*`,**0 条按 region 限定**。所以韩国集群的 controller
在凭据层面**也能操作东京的 ALB**。

接受的理由:守夜灯平时零节点、controller 不运行,且这是 demo 环境。
要收紧应当给策略加 `aws:RequestedRegion` 条件,**而不是不加信任**
(不加的后果是韩国建不出入口)。

#### ⑥ 一条门禁按设计挂靶了

`test_99` 里那条「映射里仍然缺 LB controller」是上一轮**故意**写成
「会在缺口修好时挂掉」的,好让修的人必须回来改手册。

**那个机制真的起作用了** —— 补完信任策略后它立刻挂靶,于是手册第五节③
与第三节能力矩阵一起改成了「已覆盖」。现在它翻面守反向:别把覆盖又弄丢。

#### ⑦ 两次判据自身的错误

- `test_script_has_no_namespace_constant` 第一版用文本匹配 `NAMESPACE = "`,
  结果**匹配到了解释「为什么删掉它」的那段注释** —— 判据分不出「常量存在」
  与「注释提到常量」。这类判据还有个更糟的后果:**想把教训写进注释就会踩到
  自己的门禁**。改用 AST 只看模块级赋值。
- `test_scopes_by_sub_not_only_aud` 断言的是 `{NAMESPACE}` 字面量,
  我把它改成 `{namespace}` 后挂掉 —— 这条是真实失败,判据照实更新。

---


### 4.23 韩国入口链路打通 —— 并终于回答了「petsite 能不能服务」

**日期**:2026-09-25 ｜ **手册第五节缺口② 关闭** ｜ 栈 `dr-korea-alb`

#### ① 建了什么

```
ALB        dr-korea-petsite-alb    internal / active / 2a+2b
DNS        internal-dr-korea-petsite-alb-263460690.ap-northeast-2.elb.amazonaws.com
目标组      dr-korea-petsite-tg     port 8080  type=ip  健康检查 /health/status
           dr-korea-pethistory-tg  port 80    type=ip  健康检查 /health/status
安全组      集群 SG 放行来自 ALB SG 的 80/8080
controller Helm chart 3.0.0（**与东京同版本**）+ SA alb-ingress-controller
```

#### ② 决定性证据(与部署不同的手段)

controller 日志说它注册了目标,**但日志不是判据**。查 `elbv2 describe-target-health`:

```
10.20.1.88:8080  healthy   ← 连续 6 次稳定
```

再走一遍真流量(从 Temporal 实例打 internal ALB):

```
GET /              → HTTP 302，4–37ms
GET /health/status → HTTP 200 "Alive"
对照：pod proxy      → HTTP 200（绕过 ALB，同一结果）
```

#### ③ 终于解释清了那个 302

前两轮一直没解释清 `GET /` 为什么返回 302。这次拿到了 `Location` 头:

```
Location: /?userId=user88001
```

**那是 petsite 自己的会话分配行为** —— 把 `/` 重定向到带 `userId` 的自己。
完全正常。

**所以东京那个目标组的健康检查从一开始就配错了**:

```
东京 Servic-PetSi-7JEWC19HNKSR   健康检查 = GET / 期望 200
实测                             0/2 healthy，Target.ResponseCodeMismatch
```

两个独立测量互相印证(ALB 自己的健康检查器直连 pod 报 mismatch;pod proxy 手工
GET 得到 302),所以那个 302 是应用真实行为,不是 pod proxy 的路径重写。
韩国因此**刻意不照抄**,改用 `/health/status`。

> 我没有动东京的配置 —— 那是生产变更,不在授权范围。

#### ④ ⚠️ 「petsite 能不能服务」不再是 inconclusive:**不能**

4.21 时这一项记的是 inconclusive,因为当时的探针在两个 region 上没有区分力。
现在有了真的 ALB 通路,跟随重定向走完:

```
GET / (跟随重定向)  → HTTP 200，10527 字节
页面 <title>        → "Error - Observability PetAdoptions"
```

**它返回 HTTP 200,内容却是应用自己的错误页。**

根因链条完整:

```
PetSite.Configuration.ParameterRefreshManager
  → Fetching parameter from SSM: /petstore/searchapiurl
  → ssm.ap-northeast-2.amazonaws.com → ParameterNotFound
  → HomeController 渲染错误页
  → 以 HTTP 200 返回
```

IRSA 正常、region 解析正确(`ap-northeast-2`)、SDK 拿到凭据 —— **参数就是不存在**。

##### 这比 4.21 记的那个陷阱更坏一层

4.21 记的是「健康探针会骗人」。现在知道**状态码也会骗人**:

| 信号 | 零配置的 petsite 上 |
|---|---|
| pod `Running` / restarts 0 | ✅ 绿 |
| readiness 探针 | ✅ 绿 |
| `/health/status` | ✅ 200 "Alive" |
| ALB 目标健康 | ✅ healthy |
| **`GET /` 的 HTTP 状态码** | ✅ **200** |
| 页面 `<title>` | ❌ `Error - …` |

**从 pod 到 ALB 到 HTTP 状态码,整条链路全绿,而用户看到的是错误页。**
唯一能区分的判据是**页面内容**。

#### ⑤ 一个真实的守夜灯缺陷:缩容到零会死锁 30 分钟

缩回 `desiredSize=0` 后节点卡在 `Terminating:Wait` 超过 6 分钟。查出真因:

```
PDB     kube-system/coredns   maxUnavailable=1   allowed=0   expected=2
coredns 2 副本 → 1 个 Running 在唯一的节点上，1 个 Pending（无处可调度）
节点     unschedulable=True，污点 node.kubernetes.io/unschedulable=NoSchedule
事件     FailedScheduling: 0/1 nodes are available
```

**coredns 的 PDB 永远无法满足**:2 副本要求至少 1 个可用,而唯一的节点正在排空、
第二个副本无处可去 → 驱逐被拒 → 排空永不完成。

PDB 算术:

```
副本=2 可用=1 maxUnavailable=1 → 允许驱逐 0 个   ← 死锁
副本=1 可用=1 maxUnavailable=1 → 允许驱逐 1 个   ← 可驱逐
```

钩子 `Terminate-LC-Hook` 的 `HeartbeatTimeout=1800`、`DefaultResult=CONTINUE`,
所以**不干预的话节点会卡满 30 分钟才被强制终止** —— 每次缩容白付半小时 EC2。

##### 因果验证(不是相关性)

把 coredns 降到 1 副本:

```
干预前   allowed=0  expected=2
干预后   allowed=1  expected=1
随后     Terminating:Wait → Terminating:Proceed → 实例消失（约 2 分钟）
```

卡了 6 分钟的节点在干预后 2 分钟内就终止了 —— **根因确认**。

**待办**:扩容路径应当把 coredns 恢复成 2 副本,缩容路径应当先降到 1。
目前 coredns 留在 1 副本(零节点时不需要 DNS 高可用)。

#### ⑥ ⚠️ 静息状态有个陈旧目标

零节点后目标组里**仍留着** `10.20.1.88 unhealthy`:

摘除目标是 controller 干的,而 **controller 自己也在那个被排空的节点上** ——
它先死了,没人来摘。

所以「ALB 目标 unhealthy」在静息状态是**常态**,它**分不出**
「守夜灯正常休眠」与「切换失败了」。又是同一类缺陷:一个信号覆盖了两种
截然不同的状态。

预期扩容时 controller 回来会 reconcile 掉陈旧目标并注册新的,
**但这一点尚未验证** —— 下一次演练时核实。

#### ⑦ 两个 CFN 字符集坑

- 安全组的 `GroupDescription` **只接受 ASCII**
  (`Character sets beyond ASCII are not supported`)。
- 规则的 `Description` 允许集是 `a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*`,
  **不含 `>`** —— 连 `->` 都不能写。

中文只能待在注释里。第一次失败时 ALB 报了个没有细节的 `Internal Failure`,
修完描述后自己就成了 —— 那是并行失败的连带效应。

#### ⑧ 改了一个设计决定

原计划是「controller 清单存 S3,由 workflow 在扩容后 apply」。
实际做完发现更简单的做法:**k8s 对象(含 Deployment)预置在集群里,节点缩到 0**。
零节点时 pod 只是 `Pending`,不占任何成本,而扩容时自动起来,
切换时**不需要 apply 任何东西**。S3 里的清单保留作为集群对象丢失时的兜底。

#### ⑨ 清理

节点组 `desiredSize=0`、存活 EC2 **0**、临时 cluster-admin 已摘并核实空数组。
ALB 与目标组**刻意保留**(那是预置的入口,ALB 约 \$16/月)。

---


### 4.24 搬齐 6 个周边工作负载 —— 三个缺陷全在我自己的生成器里

**日期**:2026-09-25 ｜ **手册第五节缺口① 基本关闭(6/7 就绪,1 个开放项)**

#### ① 做法:生成而不是手写

手写清单是**会悄悄过期的快照**。所以写了 `scripts/gen_korea_workloads.py`,
从东京活集群读、生成 `infra/dr-korea/15-korea-workloads.yaml`,
文件头记生成时间与来源。东京变更后重跑,diff 直接告诉你变了什么。

产出:8 个 ServiceAccount + 1 个 ConfigMap + 6 个 Deployment + 6 个 Service。

#### ② 结果

```
list-adoptions      1/1 ✅      pay-for-adoption  1/1 ✅
petfood             1/1 ✅      search-service    1/1 ✅
traffic-generator   1/1 ✅      petsite           1/1 ✅
pethistory          0/1 ❌  ← 开放项，见 ⑥
```

#### ③ 缺陷一:ServiceAccount 对象根本没建 —— 我只验了 IRSA 的一半

```
ReplicaFailure | FailedCreate |
  pods "list-adoptions-…" is forbidden:
  serviceaccount "list-adoptions-sa" not found
```

4.18 那次我验过全部 7 个角色的**信任策略**(IAM 侧),却只在集群里建了
`petsite-sa` 一个。IRSA 是**两侧契约**:

| 侧 | 4.18 的状态 |
|---|---|
| IAM 角色信任韩国 OIDC + `:sub` 对上 | ✅ 7 个都做了 |
| k8s 里存在带 `role-arn` 注解的 SA | ❌ 只有 1 个 |

**失效形态极刁:pod 根本不会被创建。** `kubectl get pod` 里一个异常 pod
都看不到 —— 我那个遍历 pod 的诊断步骤打印了空白,看着像「一切正常」。
**任何遍历 pod 的健康检查对这个缺陷完全失明**,证据只在
Deployment / ReplicaSet 的 conditions 上。

**结构性修法**:SA 对象改由 `irsa-korea-mapping.json` 生成 —— 与信任策略**同源**,
两半永远不会再走散。并且 SA 排在 Deployment 之前(apply 按文件顺序)。

##### 附带:补上 SA 之后 pod 仍然不出来

旧 ReplicaSet 处在指数退避里(事件 `count=17`),条件文本是**陈旧的**,
还在说 SA 不存在。`rollout restart` 建新 ReplicaSet 绕开退避后 pod 立刻出来
—— 因果验证。

**这在真切换时会表现成「6 个服务起不来,错误信息指着你已经修好的东西」。**

#### ④ 缺陷二:白名单拷贝悄悄丢了 `enableServiceLinks`

petfood `CrashLoopBackOff`,退出码 1:

```
Error: LoadError { message: "Failed to deserialize server config:
  invalid type: string \"tcp://172.20.21.75:80\",
  expected an integer for key `port` in the environment" }
```

namespace 里有名为 `petfood` 的 Service → k8s 注入 service-link 环境变量
`PETFOOD_PORT=tcp://172.20.21.75:80`,而 petfood 的配置前缀正好是 `PETFOOD_`,
于是 `port` 读到一个字符串。

##### 对照组推翻了我的第一个假设

我先猜是「pod 比 Service 先建」(service-link 变量是 pod 创建时的快照)。
查东京:

```
enableServiceLinks: False    ← 东京**显式关掉了**
pod 建于 18:31 > svc 建于 18:13   ← 所以不是创建顺序
```

**真因是我的生成器用白名单拷 pod spec**,只拷了
`serviceAccountName / volumes / nodeSelector / tolerations / securityContext`,
把 `enableServiceLinks: False` 丢了。

而那条报错**指不到「你丢了 enableServiceLinks」**。

**结构性修法**:改成**黑名单** —— 深拷整个 pod spec,只去掉明确有害的
(`nodeName` 会把 pod 钉在东京的节点上;`serviceAccount` 是废弃别名)。
白名单的问题是「忘了的字段静默消失」;黑名单反过来,新字段默认被带上。

> ⚠️ 这个丢失影响了 **5 个** Deployment,但只有 petfood 崩 ——
> 另外 4 个也被注入了污染的环境变量,只是它们不在意。
> **「其它几个起来了」完全不能说明这一个也会起来。**

> ⚠️ 顺带发现东京的潜在脆弱点:东京靠显式 `enableServiceLinks: False` 避开这个坑,
> 如果哪天有人漏了这个字段重建 petfood,**东京也会崩成一样的形状**。

#### ⑤ 缺陷三:拷了卷,没拷卷引用的 ConfigMap

```
FailedMount: configmap "otel-config" not found
```

pod 永远停在 `ContainerCreating`,而**原因只出现在 pod 事件里** ——
容器状态里是空的 `ContainerCreating`,Deployment conditions 也不提。

**结构性修法**:生成器扫描 `volumes` / `envFrom` / `env.valueFrom`,
把引用到的 ConfigMap 从东京搬过来。
**Secret 只列名字不拷内容** —— 不把密钥写进 git 跟踪的文件。

> 只有 pethistory 挂 `otel-config`,另外 5 个的 otel sidecar 用默认配置。
> 又一次「其它几个没事」说明不了这一个。

#### ⑥ 开放项:pethistory 起不来,根因**未确定**

已知的:

```
镜像            从韩国 ECR 拉取成功（104ms，83.7MB）—— ECR 链路没问题
容器            started，restarts=1
监听            **从不监听 8080**，startup probe connection refused
容器日志         **一行都没有**
对照：东京        同一个容器有 200 行访问日志 —— 所以它是会打日志的
```

所以进程起来了、在监听之前就卡住或退出了,且没有任何输出。
**这与缺口⑤(region 内后端)一致,但我不声称已确定根因** —— 没有日志就没有证据。
留到做缺口⑤ 时一并查。

#### ⑦ 我自己的判据又错了两次

- **镜像预检**假设所有镜像都在私有 ECR,于是把
  `public.ecr.aws/aws-observability/aws-otel-collector:v0.47.0` 报成「韩国缺失」。
  **那是判据的错,不是真缺口** —— 公共镜像每个 region 都能拉。
  已修:按 registry 主机名区分私有/公共。
- **pod 状态检查**用 `phase != Running` 过滤,漏掉了「Running 但未就绪」。
  pethistory 就是那个形状,于是「有问题的 pod」打印了空白。
  **判据必须按就绪数,不按 phase。**

#### ⑧ 清理

按 4.23 查出的顺序:先把 coredns 降到 1 副本(`PDB allowed` 0→1),
再 `desiredSize=0`。临时提权已摘。

---


### 4.25 缺口⑤ 调研 + 写入 19 个可移植参数(零新增计费资源)

**日期**:2026-09-25 ｜ 手册第五节缺口⑤ **调研完成,只做了免费的那一档**

#### ① 41 个参数的分档

按**值的形状 + 已知资源类型**分,不按名字:

| 档 | 数量 | 处置 | 新增计费资源 |
|---|---|---|---|
| A 集群内 DNS(`*.svc.cluster.local`) | 10 | 逐字复制 | 无 |
| B 字面值/开关 | 6 | 逐字复制 | 无 |
| C 韩国已有资源 | 3 | 写韩国的值 | 无 |
| D region 级但值里看不出来 | 6 | **需要韩国资源** | 有 |
| E 值里含 `ap-northeast-1` | 12 | **需要韩国资源** | 有 |
| F SecureString | 4 | 另行决定 | 无 |

**A+B+C = 19 个已写入,零新增计费资源。**

#### ② 最有价值的发现:10 个是集群内 DNS,完全可移植

`search-service.petadoptions.svc.cluster.local` 这类名字由**所在集群**的
CoreDNS 解析,与 region 无关。韩国集群里同名 Service 已建好(4.24),
所以逐字复制就是对的。

**而 petsite 报错缺的正是其中之一** —— 4.23 查到的根因是
`/petstore/searchapiurl` → `ParameterNotFound` → 渲染错误页。

> ⚠️ 但**尚未验证**写入这 19 个之后 petsite 是否就能正常渲染 ——
> 那需要再扩一次容实测。这一节不声称已修好。

#### ③ ⚠️ 一个纯按值形状分类会漏掉的陷阱

这 6 个的**值里不含 region 字样**(就是个裸名字或裸 ID):

```
dynamodbtablename              ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM
s3bucketname                   serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb
agent/waggleai/guardrailid     u4jw0mo0r7pq
agent/waggleai/memoryid        WaggleAIMemory-HuA0HS92Yd
agent/waggleai/nutritionkbid   QOWP8XIMMU
searchimage                    petsearch-java:latest
```

按形状分类会把它们判成「可移植」,**但它们指向的资源全是 region 级的**。
照抄过去的表现是运行时 `ResourceNotFound`。

所以 D 档是**手工列出来的**,不靠形状推断 —— 脚本里写明了这一点。

> 附带发现:`searchimage` 指向 `petsearch-java:latest`,而**两个 region 的
> ECR 都没有这个仓库**。那是东京本身就有的悬空引用,不是韩国的缺口。

#### ④ pethistory 开放项的根因(四个测量支持,未直接观测)

pethistory 的环境变量里写死了:

```
AWS_REGION      ap-northeast-1              ← 写死东京
RDS_SECRET_ARN  arn:aws:secretsmanager:ap-northeast-1:…:secret:DatabaseSecret…
UPDATE_ADOPTION_URL  https://9dw5r2dqlb.execute-api.ap-northeast-1.amazonaws.com/prod/
```

从韩国 VPC 实测:

```
东京 Aurora 端点解析     → 11.0.3.64（东京 VPC 私网地址）  ✅ DNS 能解析
连 5432                 → ❌ 连不上（无 VPC 对等）
东京 Secrets Manager     → 可达（HTTP 404 = 到达了端点）   ✅ 取密钥这步能过
对照：韩国自己的库        → ✅ 连得上（证明测法有效）
```

所以它取到东京密钥后**卡在连东京的库上** —— 与「started、零日志、从不监听 8080」
的表现一致。

**根因未直接观测到**(没看到栈),但四个测量互相支持。

> 修法**不是**让韩国连上东京 —— 真灾难时东京就是没了。
> 而是给韩国自己的 Secrets Manager 密钥(含韩国的库端点)。

#### ⑤ ⚠️ 演练本身制造了一个数据保护密钥环分叉

核实韩国 SSM 时发现多出一个我没写的参数:

```
/petstore/dataprotection/key-846044d6-da20-441f-bdcc-84492ffcfd0c
  类型     SecureString
  改于     2026-09-25T08:24:56
  改动者   assumed-role/Applications-PetSiteServiceAccount…
```

那正是 4.21 第一次韩国演练里 petsite 启动的时刻 ——
**petsite 自己生成了一个新的 ASP.NET Data Protection 密钥并写进了韩国 SSM。**

```
东京 dataprotection 密钥  4 个
韩国                     1 个（petsite 自己建的）
```

**密钥环不一致的后果**:切换后东京签发的 cookie 与防伪令牌在韩国**验不过** ——
用户被登出,表单 POST 返回 400。而这不会有任何告警。

要让会话跨切换存活,韩国必须持有**东京那 4 个密钥**。那是一个明确的
DR 设计决定,不是副产品。

> 顺带:petsite 的角色挂着 `AmazonSSMFullAccess`(还有 SNS/SQS 的 FullAccess)——
> 这解释了它为什么能写 SSM,也是一处过宽的权限。demo 环境,记录备考。

#### ⑥ 我自己的核实又错了两次

- `get-parameters-by-path` **是分页的**,`--query 'length(Parameters)'`
  会**按页各算一次**(打印了两次「10」)。正确做法是取 `Parameters[].Name`
  再数行。差点据此以为只写进去 10 个。
- 检查参数「不存在」时用输出文本判断,而错误信息在 **stderr**,
  `head -1` 拿到空行 → `case` 落进了「存在」分支,把 3 个正确未写的参数
  报成了「被误写」。**存在性检查要用退出码,不要用输出文本。**

#### ⑦ 剩下的需要建什么(未执行,待决定)

| 资源 | 用途 | 空闲成本 |
|---|---|---|
| DynamoDB 表 | `dynamodbtablename` | 按请求计费,空闲≈0 |
| SQS 队列 | `queueurl` | ≈0 |
| SNS 主题 | `snsarn` | ≈0 |
| StepFunctions 状态机 | `petadoptionsstepfnarn` | ≈0 |
| S3 桶 | `s3bucketname` | ≈0 |
| Secrets Manager 密钥 | `rdssecretarn`(pethistory 要用) | ≈\$0.40/月 |
| API Gateway | `updateadoptionstatusurl` | ≈0 |
| 内网压测 ALB | 5 个 `agent/*url` | ≈\$16/月 |
| Bedrock guardrail | `guardrailid` | ≈0 |
| AgentCore memory + gateway + runtime | `memoryid` / `gatewayurl` / `runtimearn` | 待查 |
| **Bedrock 知识库** | `nutritionkbid` | **需向量库,最贵的一项** |

前 7 项加起来空闲成本不足每月 1 美元;后 4 项(WaggleAI 相关)里
**知识库需要向量存储,是数量级更高的一项**。

---


### 4.26 韩国 petsite **真的能服务了** —— 三个页面实测渲染正确

**日期**:2026-09-25 ｜ 栈 `dr-korea-backends` ｜ 用户指示「尽量用 AWS 托管服务」

#### ① 结果

```
Deployment           7/7 就绪
首页                 HTTP 200  "Home - Observability PetAdoptions"          219 KB
/PetListAdoptions    HTTP 200  "Pet Adoption List - …"        0.55s  10 KB
/FoodService         HTTP 200  "Pet Food Store - …"                  21 KB
/Checkout            HTTP 200  "Error - …"     ← 仍失败，需 StepFunctions + API GW
```

**判据是页面标题,不是状态码** —— 4.23 记过:错误页也返回 200。

#### ② 建了什么(空闲成本合计不足每月 1 美元)

栈 `dr-korea-backends`:DynamoDB(`PAY_PER_REQUEST`,刻意与东京的预置容量不同)、
S3(加密+全阻公开)、SQS、SNS,外加密钥的资源策略。

#### ③ 用户指示「尽量用 AWS 托管服务」—— 查清了哪些能用、哪些不能

| 托管能力 | 能不能用 | 依据 |
|---|---|---|
| SSM Parameter Store | ✅ 已在用 | 参数本来就存在托管服务里 |
| Parameter Store 跨 region 复制 | ❌ 不存在这个能力 | 只能脚本同步 |
| Secrets Manager | ✅ 已在用 | |
| **Secrets Manager 跨 region 复制** | ❌ **不适用** | 副本是只读同值副本;而 pethistory **从密钥里读 `host`**(源码 `config.py` 第 63 行),副本的 host 会留在东京 |
| **RDS 托管主密码** | ❌ **官方明确不支持** | `AuroraUserGuide/rds-secrets-manager.html`:"isn't supported for … **DB clusters that are part of an Aurora global database**" |
| **Secrets Manager 资源策略** | ✅ **采用了** | `secretsmanager/latest/userguide/auth-and-access_resource-policies.html`:"you can attach policies to secrets **or** identities" |

所以韩国必须有自己的密钥,而**授权用资源策略而不是改生产角色**:

- 不动东京 `Applications` 栈建的那 7 个角色
- 授权与被授权资源同生共死,不会留下指向不存在资源的孤儿语句
- 范围天然最小(资源策略只能作用于这一个密钥)

密钥的**值**只能用脚本设(密码必须与东京一致,写进模板等于提交进仓库),
但**资源与授权都在 CFN 里**。

#### ④ 关键修法:删环境变量,而不是改写它们

读 `petadoptionshistory-py/config.py` 源码(不是猜的):

```python
if cfg['update_adoption_url'] == None or cfg['rds_secret_arn'] == None:
    return fetch_config_from_parameter_store(cfg['region'])
```

**应用本来就支持从 Parameter Store 取配置** —— 只在环境变量**缺失**时才走。
清单里写死了东京的值,所以那条路从没被走过。

所以生成器改成**删掉** `RDS_SECRET_ARN` / `UPDATE_ADOPTION_URL`,让它回落;
而 `AWS_REGION` / `S3_REGION` 必须**改写**(它们决定去哪个 region 读参数)。
**删 vs 改写的区别不能凭感觉定 —— 要看源码。**

#### ⑤ ⚠️ 又一次「全绿但不能服务」,而且这次藏得更深

资源策略第一版只授权了 pethistory 一个角色。结果:

```
7/7 Deployment 就绪          ✅
pethistory 日志正常           ✅
首页渲染正确                  ✅
/PetListAdoptions            ❌ 挂满 60 秒后 ALB 返回 504
```

list-adoptions 撞的是**同一个** `AccessDenied`,但它的表现不是启动失败
而是**请求超时** —— 所以从「7/7 就绪」和任何 pod 级检查里**完全看不出来**。

把授权扩到 7 个业务角色后:`/PetListAdoptions` **0.55 秒 HTTP 200**。
因果确认。

> 这是本会话第几次「全绿但不能服务」已经数不清了。这次的新形态是:
> **缺陷只在某一条请求路径上显形,而那条路径不在任何健康检查里。**

#### ⑥ pethistory 的根因从推断变成了直接观测

4.25 记的是「四个测量支持,未直接观测」。删掉环境变量之后它第一次打出了栈:

```
botocore.exceptions.ClientError: AccessDeniedException calling GetSecretValue
  User: assumed-role/Applications-petadoptionshistoryapplicationPetSiteS-…
  not authorized on resource: …secret:dr-korea/petadoptions/database-…
```

有意思的是:**原来的「零日志」与现在的「明确报错」是同一个依赖的两种表现** ——
指向东京时它卡在连库上(无输出),指向韩国时它在取密钥时就快速失败(有栈)。
**快速失败比静默挂住好得多**,而这个改善是免费附带的。

#### ⑦ 数据保护密钥环已对齐

把东京 4 个 SecureString 复制到韩国,**逐个用 SHA-256 指纹核对**(不打印值),
4/4 一致。所以切换后东京签发的 cookie 与防伪令牌在韩国能验过。

> 韩国现在 5 个 —— 多的那个是 4.21 演练时 petsite 自己生成的(见 4.25)。

#### ⑧ 还差什么

`/Checkout` 仍渲染错误页,需要:StepFunctions 状态机 + **它引用的 3 个 Lambda**
(`ServicesEks2-StepFnlambdastep{priceGreaterThan,priceLessThan,readDDB}`)
+ API Gateway。WaggleAI 那一档(Bedrock guardrail/memory/知识库 + AgentCore)
**留给用户决定** —— 知识库需要向量存储,是数量级更高的成本台阶。

`dr-korea-pethistory-tg` 的目标状态是 `unused` —— 那是**正确**的:
目标组没有挂到任何监听器规则上(韩国只给 petsite 建了监听器)。

---


### 4.27 领养工作流搬到韩国 —— 以及 `/Checkout` 的真因原来不在这里

**日期**:2026-09-25 ｜ 栈 `dr-korea-stepfn`

#### ① 建了什么

StepFunctions 状态机 `dr-korea-petadoptions` + 它引用的 **3 个 Lambda**。

搬状态机不是「建一个状态机」:实测东京那份定义引用了 3 个函数,
所以必须连带搬。三份代码都极小(东京部署包各 1128 字节),
来源是 `one-observability-demo` 仓库
`PetAdoptions/cdk/pet_stack/resources/stepfn_lambdas/lambda_step_*.py`,
所以**内联在模板里** —— 看得见,也不会与仓库悄悄漂移。

#### ② ⚠️ 一处必须成对处理的东西(主动避开的坑)

东京那 3 个函数带 `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`,
而那个 wrapper 来自 **ADOT Lambda 层**:

```
arn:aws:lambda:ap-northeast-1:901920570463:layer:aws-otel-python-arm64-ver-1-32-0:1
arn:aws:lambda:ap-northeast-1:580247275435:layer:LambdaInsightsExtension-Arm64:42
```

**只带环境变量不带层,函数会起不来**(wrapper 脚本不存在),而层 ARN 是
region 专属的、照抄东京的在韩国无效。

处置是**两个都不带** —— 它们纯粹是观测用的,而用户早先明确灾备 region
不需要那一套。**这是「只拷契约一半」那类缺陷的又一个实例,这次是主动避开。**

#### ③ 决定性证据:真跑一次状态机

不看 CFN 报告,直接 `start-execution`:

```
输入   {"petid":"001","pettype":"puppy"}
状态   SUCCEEDED
输出   ProcessGreaterThan55 - Execution complete   ← 与 price=89 一致，Choice 分支走对了
```

#### ④ 数据:26 条复制过去了,但**这是权宜之计**

`readDDB` 要查表,而韩国表是空的。复制了东京 26 条(宠物目录,属于参考数据),
复制后**重新扫两边比对**,26/26 内容一致。

> **正确答案是 DynamoDB 全局表**(托管的跨 region 复制)。
> 但转全局表需要**两处改动东京生产表**:
> ① 开启 Streams(实测东京那张表 `StreamSpecification` 是 `null`)
> ② 添加韩国副本
>
> 两者都是对生产资源的变更,**必须先问用户**。
>
> 一次性复制的局限必须写清:**不是持续同步**。东京改了数据韩国不会跟着变,
> 而且**看不出来** —— 表里有数据、查询能返回,只是返回的是旧的。

#### ⑤ `/Checkout` 的真因原来不在 StepFunctions

建完状态机、写好 `/petstore/petadoptionsstepfnarn`、重启 petsite 之后:

```
首页                200  "Home"                ✅
/PetListAdoptions   200  "Pet Adoption List"   ✅  0.18s
/FoodService        200  "Pet Food Store"      ✅  0.23s
/Checkout           200  "Error - …"           ❌  仍然错
```

petsite 日志给出了真因:

```
Error fetching cart data for user: user00911
System.Net.Http.HttpRequestException: Response status code does not indicate success:
  500 (Internal Server Error)
```

**不是 StepFunctions,是 petfood 的购物车 API 返回 500。**

查 petfood 的环境变量:

```
AWS_REGION               ap-northeast-2                              ← 我改对了
PETFOOD_REGION           ap-northeast-1                              ← **漏了**
PETFOOD_FOODS_TABLE_NAME ServicesEks2-ddbpetfoodfoods…（东京表）
PETFOOD_CARTS_TABLE_NAME ServicesEks2-ddbpetfoodcarts…（东京表）
PETFOOD_EVENT_BUS_NAME   ServicesEks2petfoodeventbus…（东京总线）
```

所以 petfood 还需要**韩国自己的两张表 + 一个 EventBridge 总线**。
实测东京那两张表的规格:

```
ddbpetfoodfoods   键 id(HASH)                      9 条    PAY_PER_REQUEST
ddbpetfoodcarts   键 user_id(HASH) + item_id(RANGE) 0 条    PAY_PER_REQUEST
总线              ServicesEks2petfoodeventbus12F76D34
```

> ⚠️ 我的 `REWRITE_ENV` 只列了 `AWS_REGION` 与 `S3_REGION`,**漏了
> `PETFOOD_REGION`**。这说明「按名字列白名单」同样会漏 ——
> 更稳的做法是**凡是值等于 `ap-northeast-1` 的环境变量都要报出来**,
> 让人显式决定,而不是只改我想到的那几个。

#### ⑥ 我自己的探测又错了一次

想绕过 petsite 直接打 petfood 的 `/api/cart`,用了 pod proxy 的 **80 端口**,
得到 `connection refused`。那是**我测错了** —— petfood 的容器不监听 80
(Service 是 80 → targetPort)。`connection refused` 在这里是「没测到」,
不是「应用坏了」。**又一次。**

#### ⑦ 清理

节点组 `desiredSize=0`、临时提权已摘并核实空数组。
新增资源空闲成本:3 个 Lambda(不调用不计费)+ 1 个 STANDARD 状态机
(按状态转换计费,空闲 0)。

---


### 4.28 补齐 petfood 后端 —— 我把一个好页面弄坏了,又修回来

**日期**:2026-09-25 ｜ 栈 `dr-korea-backends` 扩充

#### ① 建了什么

```
dr-korea-petfood-foods     键 id(HASH)                        + 2 个 GSI
dr-korea-petfood-carts     键 user_id(HASH) + item_id(RANGE)
dr-korea-petfood-eventbus  EventBridge 事件总线
```

授权沿用 4.26 的思路:**资源策略,不改生产角色**。
DynamoDB 支持资源策略(`developerguide/access-control-resource-based.html`),
EventBridge 总线也支持。动作集照东京那个角色的内联策略**逐条抄**(实测 10 个)。

环境变量改写补齐 4 项:`PETFOOD_REGION` + 三个 `PETFOOD_*_NAME`。

#### ② ⚠️ 我把 `/FoodService` 从好弄坏了 —— 抄了主键就以为抄完了表

第一版只抄了 `KeySchema`,**没抄二级索引**。结果:

```
改动前   /FoodService  200  "Pet Food Store"   ✅
改动后   /FoodService  200  "Error - …"        ❌  ← 我造成的回退
```

而报错是 `ValidationException`("The provided key element does not match the
schema"),**指向的是另一张表(carts)** —— 完全看不出真因在 foods 表缺索引。

东京那张表有两个 GSI:

```
FoodTypeIndex   food_type(HASH) + price(RANGE)   投影 ALL
PetTypeIndex    pet_type(HASH)  + name(RANGE)    投影 ALL
```

补上之后 `/FoodService` 恢复 200 "Pet Food Store" —— 因果确认。

> **「抄了主键就算抄完了表」是这次的错。** 表的契约还包括:属性定义、
> GSI/LSI、投影、流。缺任何一个都可能**只在某一条查询路径上显形**。

#### ③ 两个 DynamoDB 的硬约束(都是实测报错换来的)

```
Cannot perform more than one GSI creation or deletion in a single update
   → 首次建两个索引必须**分两次部署**
Number of attributes in KeySchema does not exactly match number of
attributes defined in AttributeDefinitions
   → AttributeDefinitions 必须**恰好**等于所有 KeySchema 用到的属性；
     第一趟只有一个索引时，另一个索引专用的属性也要一起去掉
```

第二条我第一次没看到,**因为用 `tail -2` 把错误文本截掉了** ——
那正是「排查命令别截断 stderr」那条规矩的代价。

#### ④ `/Checkout` 的真因:应用自身的缺陷,东京也一样

四个页面现在:

```
首页                200  "Home"                ✅
/PetListAdoptions   200  "Pet Adoption List"   ✅  0.20s
/FoodService        200  "Pet Food Store"      ✅  0.12s
/Checkout           200  "Error - …"           ❌
```

petfood 日志:`ValidationException` on `dr-korea-petfood-carts`,
路径 `GET /api/cart/:user_id`。

读源码(`petfood-rs/src/repositories/cart_repository.rs:338`):

```rust
.get_item()
    .key("user_id", AttributeValue::S(user_id.to_string()))   // ← 只给分区键
```

**`GetItem` 必须给全主键**,而 carts 表是复合主键(`user_id` + `item_id`)。
两侧表结构**逐字相同**(并排比对过 KeySchema / AttributeDefinitions / GSI / LSI),
所以**这是 demo 应用自身的缺陷,东京会以完全相同的方式失败。**

##### 刻意不把韩国表改成单键

改成 `user_id` 单键能让韩国的 `/Checkout` 好起来 —— 但那会让**灾备站点的行为
与生产不一致**,违背贯穿全程的「演练的必须是同一个东西」。
所以保持与东京一致,把这条作为 demo 的已知缺陷交给用户。

##### ⚠️ 东京的活体对照**没做成**,不能当证据

想用 pod proxy 打东京同一接口,结果 `401` 且 pod 名取空。

> **⚠️ 归因订正(2026-09-25,写在 4.29 那次演练之后)**
> 当时我把原因记成「token 在脚本中途失效」,**那是错的**。
> 真因是**结构性的**:`list-access-entries` 实测东京 `PetSite` 集群共 20 个
> 访问条目,**没有一个是 Temporal 角色**;而韩国集群里有。
> 所以 DR worker **从设计上就打不到东京集群** —— 重试多少次都是 401。
>
> 记错归因的代价是把一个「本来就不可能成功」的测法记成了「偶发失败、可重试」。而「东京日志 0 命中」同样**分不出**「没有这个错」与「日志取失败」。

**所以结论是建立在源码 + 两侧表结构上的,不是建立在那次失败的对照上。**
这两者强度不同,必须分清。

#### ⑤ 我自己的探测错误(又一次)

上一轮用 pod proxy 的 **80 端口**打 petfood 得到 `connection refused`。
这次查到真实值:`Service port 80 → targetPort 8080`。**那次是我测错了。**

#### ⑥ 数据同步脚本已参数化

`sync_korea_ddb_items.py` 现在支持多表,**键结构逐表声明不推断**
(拿错字段的表现是 KeyError 或者「比对永远认为全都缺失」)。

已同步:`petadoptions` 26 条、`petfood-foods` 9 条,写后都**重新扫两边比对**。
`petfood-carts` **刻意不同步** —— 它是用户数据不是参考数据,
切换后应当由用户重新加购。

#### ⑦ 清理

节点组 `desiredSize=0`、临时提权已摘并核实空数组。
新增空闲成本:两张 PAY_PER_REQUEST 表 + 一个事件总线 ≈ 0。

---


### 4.29 真跑了一次完整的数据库切换演练(含回切)

**日期**:2026-09-25 16:38–16:53 UTC ｜ 第⑥项 ｜ 用户已逐字放行

#### ① 事前(红线要求的「事前」)

回切命令**在动手之前**写进了 ledger。事前状态:

```
petsite-global   available   aurora-postgresql 16.11
  东京 serviceseks2-databaseb269d8bb-efjeyzicx2ak        IsWriter=true
  韩国 dr-korea-aurora-secondarycluster-5ctcqnmbkro4     IsWriter=false
复制延迟           平均 ~80 ms，峰值 ~1 s（15 分钟窗口）
```

#### ② 三个从文档核实到的 CLI 细节(不是猜的)

出处 `AmazonRDS/latest/AuroraUserGuide/aurora-global-database-disaster-recovery.html`:

| 细节 | 我本来会怎么弄错 |
|---|---|
| 有专用命令 `switchover-global-cluster` | 会用 `failover-global-cluster --switchover` |
| `--region` 是**主库所在的 region** | 会填目标 region |
| `--target-db-cluster-identifier` 必须是 **ARN** | 会填裸标识符 |

**回切时 `--region` 要跟着主库走** —— 那时主库已经在韩国,所以回切用
`--region ap-northeast-2`。这一点顺序反了就会用错。

#### ③ 提升前基线(这是让结论成为因果的关键)

```
pg_is_in_recovery                        t
CREATE TABLE  →  ERROR: cannot execute CREATE TABLE in a read-only transaction
库                                       adoptions / postgres / rdsadmin
```

**没有这个基线,「提升后能写」就只是一个孤立观察。**

#### ④ 提升与验证

```
16:38:34  提交 switchover
16:39:23  韩国实例 failover 完成
16:39:29  韩国成为新主
16:39:31  switchover 完成                     ≈ 57 秒
```

提升后同一套探测:

| | 提升前 | 提升后 |
|---|---|---|
| `pg_is_in_recovery` | `t` | **`f`** |
| `CREATE TABLE` | 只读事务被拒 | **`CREATE TABLE`** |
| `INSERT` | — | **返回 id 1,读回成功** |

业务库 `adoptions` 同样可写,里面是真实的 `transactions` /
`transactions_history` 两张表 —— **复制的是真数据,不是空壳**。
演练表建完即 `DROP`,不给将来要回切的库留垃圾。

#### ⑤ 回切(红线要求「事后必须回切」)

```
16:52:12  提交回切
16:52:34  等待数据同步（事件原文 "Waiting for data synchronization"）
16:52:35  韩国旧主成功降级
16:52:56  东京实例 failover 完成
16:53:02  东京成为新主
16:53:04  完成                               ≈ 52 秒
```

核实(用与操作不同的手段 —— 查 RDS 事件与集群成员,不看切换命令的回显):

```
petsite-global   available
  东京   IsWriter=true    writer 实例 serviceseks2-databasewriter2462cc03-fwgfu4gossqe
  韩国   IsWriter=false
```

#### ⑥ ⚠️ 东京侧的**行为**验证做不到 —— 这是结构性的,不是偶发

想查东京在演练窗口内是否真的写失败过(既是行为证据,也是演练的真实影响面),
结果是 `401`。查清了真因:

```
list-access-entries  东京 PetSite    20 个条目，无 Temporal 角色
list-access-entries  韩国 dr-korea   有 dr-korea-temporal-TemporalRole-…
```

**DR worker 从设计上就打不到东京集群。** 所以:

- 「东京恢复为 writer」有证据:RDS 事件 + 集群成员 + 实例角色三处一致
- 「东京**真的可写**」**没有行为证据** —— 没有从韩国 VPC 到东京库的网络通路,
  也没有东京集群的访问权。**这一条记 inconclusive,不记通过。**

这正是本会话反复出现的纪律:**「没测到」不等于「好了」,也不等于「坏了」。**

#### ⑦ 为演练加的那条授权

Temporal 角色原先读不到韩国 DB 密钥(`AccessDeniedException`),
因为 4.26 的资源策略只覆盖 7 个业务角色。补了第 8 条 `AllowDrWorkerReadForDrill`。

**它不是为了绕过检查**:切换手册要求提升后核实可写,没有这条权限那一步
只能记 inconclusive。范围仍最小 —— 只读、只这一个韩国密钥、东京密钥不受影响。

#### ⑧ 我这轮的三次测量失误

| 失误 | 后果 | 教训 |
|---|---|---|
| 轮询用的 JMESPath 写错,一直打印 `None None` | 以为切换花了 11 分钟,实际 57 秒 | **读不出值要先怀疑查询,而不是先下结论** |
| 按 `sed 's/^/  /'` 的**显示缩进**数 YAML 层级 | 插入的语句缩进差 2,YAML 解析失败 | 数缩进要看 `repr`,不看加过前缀的显示 |
| 手抄带全角括号的中文注释当锚点 | 连续两次 `count == 0` | 又一次撞上**手抄标点** —— 改成按行号定位并先核对该行内容 |

前两次都被 `assert` 拦住了,没有静默写坏 —— 这是「扰动必须先确认原串存在」
那条纪律的直接收益。

---

## 六、待记录

- [ ] `temporal-mcp` → ap-northeast-2 的 AgentCore(阶段 C)
- [ ] dr-plan-generator 经 temporal-mcp 生成并保存计划(阶段 D)

**韩国侧不建 DeepFlow**(用户决定),**不建 Neptune**(用户要求)。
清单见 `todo/KOREA-DR-MANIFEST_20260924.md`。

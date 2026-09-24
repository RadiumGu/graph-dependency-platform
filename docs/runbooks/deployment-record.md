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

## 六、待记录

- [ ] `temporal-mcp` → ap-northeast-2 的 AgentCore(阶段 C)
- [ ] dr-plan-generator 经 temporal-mcp 生成并保存计划(阶段 D)

**韩国侧不建 DeepFlow**(用户决定),**不建 Neptune**(用户要求)。
清单见 `todo/KOREA-DR-MANIFEST_20260924.md`。

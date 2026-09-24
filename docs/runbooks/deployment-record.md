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

## 六、待记录

- [ ] `temporal-mcp` → ap-northeast-2 的 AgentCore(阶段 C)
- [ ] dr-plan-generator 经 temporal-mcp 生成并保存计划(阶段 D)

**韩国侧不建 DeepFlow**(用户决定),**不建 Neptune**(用户要求)。
清单见 `todo/KOREA-DR-MANIFEST_20260924.md`。

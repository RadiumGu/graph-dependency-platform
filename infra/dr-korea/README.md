# 韩国(ap-northeast-2)守夜灯灾备站点 —— 精简 DR 栈

用户决定「**另写精简 DR 栈**」而不是复用 petsite 的 CDK。
本目录就是那个精简栈。

清单(资源、成本、暴露面、与主站差异)见
`../../todo/KOREA-DR-MANIFEST_20260924.md`。
每次部署的实际步骤记进 `../../docs/runbooks/deployment-record.md`。

---

## 为什么不复用 petsite 的 CDK

`/home/ec2-user/works/one-observability-demo/PetAdoptions/cdk/pet_stack` 实测:

    硬编码 ap-northeast-1      16 处（services-eks.ts 7、agents/agent-config.ts 7 …）
    硬编码账号 926093770964     3 处（三个 targetGroupArn 字面量）
    NodeGroup 钉死 AZ          ap-northeast-1a / ap-northeast-1c

参数化那 16 处要动的是**正在跑生产的栈**,爆炸半径大;
而灾备侧本来只要主站的一个子集(无 Neptune、无 ECS、无 4 个 WaggleAI runtime)。
那 16 处里有 7 处在 `agents/agent-config.ts`,而灾备侧根本不部那些 agent。

## 为什么用 CloudFormation 而不是 CDK

| 理由 | 说明 |
|---|---|
| 不需要 bootstrap | CDK 在新 region 要先 `cdk bootstrap`,那会建资产桶 + 几个 IAM 角色 —— 拆站点时又多几样要清 |
| 不需要 npm 工具链 | 本仓库的 `infra/` 是 Lambda + shell,没有 CDK 依赖。加一套 node_modules 只为两个栈不划算 |
| 拆除是一条命令 | `delete-stack` 逆序两次即可,不用记 CDK 的上下文状态 |
| 模板本身就是记录 | 部署记录要求「实际执行的命令逐条可抄」,YAML + 两条 CLI 比 CDK 的合成产物更直接 |

代价:EKS 与 Aurora 全局数据库的 CFN 写法比 CDK 啰嗦。接受。

---

## 栈的分批与依赖

    01-network.yaml    VPC / 子网 / NAT / SSM 端点        ← 无依赖
    02-temporal.yaml   Temporal EC2（t4g.large）          ← 依赖 01 的导出值
    03-eks.yaml        EKS 控制面 private + 节点组 0      ← 待写，依赖 01
    04-aurora-dr.yaml  全局数据库的韩国从集群              ← 待写，依赖 01

分批不是为了好看:**Aurora 从集群必须等韩国有了子网组才能建**,这是硬依赖。
而 Temporal 要先跑起来,阶段 B(验证 temporal-mcp)才有对象。

## ⚠️ 一处对清单的更正:NAT 必须落在公有子网

清单第一版写「私有子网 × 2 + NAT 网关」,不完整。NAT 需要 IGW 路由才能出站,
所以**必须**有一个公有子网。本栈建三个:

    PublicSubnetNat    10.20.0.0/24   仅放 NAT 网关，不放任何工作负载
    PrivateSubnetA     10.20.1.0/24   工作负载
    PrivateSubnetC     10.20.2.0/24   工作负载

私有子网没有 IGW 路由、`MapPublicIpOnLaunch: false`,所以「工作负载零公网入站」
这个结论不变。但公有子网确实存在,不该让清单读起来像是没有。

**本站点唯一的公网 IP 是 NAT 的 EIP**(出站用,不接受主动入站)。
用户已确认接受。

## 为什么 NAT 和 SSM 端点都要

看起来重复 —— 有 NAT 的时候 SSM 本来就能通。但:

**装完 Temporal 后可以删掉 NAT(省约 41/月),而 SSM 端点让管理通道不受影响。**
若只留 NAT 不建端点,拆 NAT 的那一刻就失去管理通道 ——
实例没有公网 IP、也没有密钥对,那时只能重建。

---

## 部署

```bash
# ① 网络
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-network \
  --template-file infra/dr-korea/01-network.yaml \
  --no-fail-on-empty-changeset

# ② Temporal（会读 ① 的导出值）
aws cloudformation deploy --region ap-northeast-2 \
  --stack-name dr-korea-temporal \
  --template-file infra/dr-korea/02-temporal.yaml \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset
```

### 生效核实(必须用与部署**不同**的手段)

`deploy` 返回成功只证明 CFN 建出了资源,**不证明 Temporal 在跑**。

```bash
# 取私有 IP
aws cloudformation describe-stacks --region ap-northeast-2 \
  --stack-name dr-korea-temporal \
  --query 'Stacks[0].Outputs[?OutputKey==`TemporalHttpApi`].OutputValue' --output text

# 经 SSM 进实例（没有公网入站，这是唯一通道）
aws ssm start-session --region ap-northeast-2 --target <instance-id>

# 实例内：三个容器都要 Up
sudo docker compose -f /opt/temporal/docker-compose.yml ps
# 引导日志（UserData 的输出都在这里）
sudo tail -40 /var/log/temporal-bootstrap.log
# HTTP API 真的在 7243 上应答 —— 这才是 temporal-mcp 能用的证据
curl -s localhost:7243/api/v1/namespaces | head -c 400
```

⚠️ **最后那条 curl 才是生效证据。** 容器 `Up` 只说明进程活着;
`/api/v1/namespaces` 有应答才说明 HTTP API 真的开着、端口真的是 7243。

(HTTP API 在官方 docker 配置里默认开启 —— 见 temporal 仓库
`docs/architecture/nexus.md`:"It is on by default in all of the Temporal
provided configurations, including the default configuration for the docker
image"。所以模板里没有额外开关,只是把 7243 暴露出来。)

## 拆除(逆序)

```bash
aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-temporal
aws cloudformation delete-stack --region ap-northeast-2 --stack-name dr-korea-network
```

⚠️ **销毁类操作由用户执行** —— 本项目纪律:agent 不跑 delete/terminate。
⚠️ 删 Temporal 栈会连带删掉那块 50GiB EBS(`DeleteOnTermination: true`),
**workflow 历史在那块盘上**。若要留,先导出。

---

## 刻意留在阶段 D 再定的事

**worker 的写权限。** `02-temporal.yaml` 里的实例角色**只给了只读**
(`eks:Describe*` / `rds:Describe*` / `ec2:Describe*` / CloudWatch 读)。

Temporal worker 最终需要「扩 EKS 节点组、提升 Aurora 从集群」这类写权限,
但那些权限的边界要等阶段 D 把 DR workflow 的步骤定下来才知道该给哪些。
**现在就给一个宽权限,等于在没有需求的时候先开口子。**

先给只读让 worker 能读两个 region 的状态(健康判断需要),
写权限在阶段 D 按实际步骤逐条追加。

## 固定版本,不用 latest

    Temporal        1.29.0
    Temporal UI     2.42.0
    compose 插件     v2.40.0
    AMI             用 SSM 公共参数取（AMI ID 是 region + 时间的函数，写死就开始过期）

`latest` 会让两次部署得到不同的东西,而部署记录的价值建立在「可重现」上。

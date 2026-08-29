# 跨 VPC 内网压测/流量生成入口

**日期**：2026-08-29 15:18 UTC
**区域**：ap-northeast-1 **账号**：926093770964

## 需求与约束

给东京区 PetSite 施加压力，但原 traffic-generator 与被测服务在**同一个 EKS 集群**内，抢占同一批节点的 CPU 与网络；要求把压力源移到**另一个 VPC**，且**不经公网**。

## 现状盘点结论

| 项 | 实测结果 |
|---|---|
| 区内 VPC | 仅两个：`vpc-010ab37a3f9f74725`（11.0.0.0/16，PetSite）、`vpc-06731f30388b57818`（10.1.0.0/16，agent-vpc-v2） |
| 互联 | `pcx-09179d94866c4afd6` **active**，CIDR 不重叠 |
| 路由 | **双向齐备**：PetSite 侧 4 张路由表全有 `10.1.0.0/16 → pcx`；agent-vpc 侧仅 1 张 |
| Transit Gateway | 无（也不需要） |
| PetSite 入口 | **只有一个 ALB 且是 `internet-facing`** ← 这是「不走公网」的唯一障碍 |

### 硬约束：agent-vpc-v2 只有一个子网能到 PetSite

11 个子网里只有 **`subnet-057b2d3519422d28b`**（`openclaw-private-1a-v2`，10.1.2.0/24，**AZ 1a**）的路由表 `rtb-0991788724ba03f2b` 同时具备 `11.0.0.0/16 → pcx` 与 `0.0.0.0/0 → nat`。

- 主路由表 `rtb-071d0d8e6e5e2c49f` 虽有对等路由，但**没有任何子网关联**，等于不生效。
- 其余 9 个子网（`tidb-poc-*`、`openclaw-public-*`）**没有对等路由**，放在那里根本到不了 PetSite。

**副作用**：该子网只在 1a，而内网 ALB 跨 1a/1c，因此约一半请求跨 AZ（peering 跨 AZ $0.01/GB/方向）。要消除需在 1c 的某张路由表（如 `rtb-0743294041b53df7d`）补 `11.0.0.0/16 → pcx` 并在 1c 再放一台。

## 采用方案：内网 ALB + 第二组 TargetGroupBinding

选它的关键依据是发现 PetSite 的 ALB→Pod 绑定用的是 **`TargetGroupBinding` CRD**（不是 Ingress），且**一个 Service 可被多个 TargetGroupBinding 引用**，所以可以完全不碰生产链路地并挂一套内网入口。

```
agent-vpc-v2 (10.1.0.0/16)                PetSite VPC (11.0.0.0/16)
┌──────────────────────────┐              ┌────────────────────────────────┐
│ EC2 petsite-loadgen      │   VPC 对等   │ 内网 ALB petsite-internal-lt   │
│ c7g.xlarge  10.1.2.66    │─pcx-09179d──▶│  :80   → petsite-lt-tg         │
│ subnet-057b2d3519422d28b │              │  :8081 → petsite-lt-search-tg  │
│ docker × N 个容器        │              └───────────┬────────────────────┘
└──────────────────────────┘                          │ TargetGroupBinding
                                            ┌─────────▼──────────┐
                                            │ service-petsite:80 │
                                            │ search-service:80  │
                                            └────────────────────┘
```

### 为什么不用 PrivateLink

peering 已通、CIDR 不冲突，PrivateLink 会多加 NLB 和 VPC Endpoint 两层；**压测场景下 Endpoint 自身有带宽与连接数上限，很可能成为被测对象之外的瓶颈，污染测试结论**。

### 为什么不打 internet-facing ALB 的私有 ENI IP

其 SG 在 `:80`/`:443` 开了 `0.0.0.0/0`，技术上从 10.1 能通，但属未文档化行为、ENI IP 随伸缩变化、且失去 ALB 多节点 DNS 轮询。只适合临时验证连通性。

## 资源清单

| 资源 | 标识 | 说明 |
|---|---|---|
| 内网 ALB | `petsite-internal-lt` / `internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com` | `scheme=internal`，落在 `subnet-0f801fa79077eb277`(1a) + `subnet-047a94f9c5ab6302a`(1c) 两个 PrivateSubnet |
| 目标组 | `petsite-lt-tg` | HTTP:8080，`target-type=ip`，HC `/`，克隆原 TG 参数 |
| 目标组 | `petsite-lt-search-tg` | HTTP:80，`target-type=ip`，**HC `/health/status`**（`/` 返回 404，不能沿用 petsite 那套） |
| ALB 安全组 | `sg-06d40c8bcd96d347d` | 入站仅 `tcp/80` + `tcp/8081` ← `10.1.0.0/16` |
| 集群 SG 补规则 | `sg-02df8bc13ac85c4cc` | `tcp/8080 ← sg-06d40c8bcd96d347d`（petsite）、`tcp/80 ← 同`（search，targetPort 是 80 不是 8080） |
| TargetGroupBinding | `petadoptions/petsite-loadtest-tgb`、`petadoptions/search-loadtest-tgb` | 与 CDK 管的 `petsite-tgb`/`pethistory-tgb` 并存 |
| IAM | 角色 + 实例配置 `petsite-loadgen-ec2-role` | 仅 `AmazonSSMManagedInstanceCore` + `AmazonEC2ContainerRegistryReadOnly` |
| 负载机 SG | `sg-059b4eaa1565c7c09` | **入站规则 0 条**，SSM 走出站 |
| EC2 | `i-05f0b897988a48d17` | c7g.xlarge，`10.1.2.66`，1a，无公网 IP，IMDSv2 强制，20GB gp3 加密 |

清单文件：`todo/loadtest/petsite-loadtest-tgb.yaml`、`todo/loadtest/loadgen-userdata.sh`

### 两个刻意的设计选择

1. **新建独立 SG 而非复用现有 ALB 的 `sg-0ae59648161ef4d63`**。复用能省掉集群 SG 那两条规则（该 SG 已被集群 SG 以 `-1` 全协议放行），但它入站开了 `0.0.0.0/0`。选了最小权限。
2. **TargetGroupBinding 不带 CDK 的 `aws.cdk.eks/prune-*` label**。现有 `petsite-tgb` 带该 label 由 CDK 管理；手工资源带上会被下次 CDK 部署清掉。

## 验证证据

从 EC2 内部实测（`10.1.2.66`）：

```
ALB DNS 解析        → 11.0.2.144 / 11.0.3.53        私有 IP
到 11.0.2.230 路由  → via 10.1.2.1 dev ens5 src 10.1.2.66    VPC 路由器(对等),非 NAT/IGW
到公网 ALB IP 的连接 → 无
```

经内网 ALB 的功能验证：

```
:80   /                    200  130,086 字节  119ms
:8081 /health/status       200                7.4ms
:8081 /api/search?         200   48,415 字节（真实宠物 JSON）
```

流量启停对照（内网 ALB `RequestCount`，每分钟）：

```
14:51    0    ← 容器启动前
14:53   83    ← EC2 单容器启动
14:56  118
14:58  149    ← 扩到 2 容器 + 集群内生成器缩到 0
15:00  226
15:01  186
```

5 分钟内 `HTTPCode_Target_2XX_Count = 717`，`Target 5XX` 与 `ELB 5XX` 均为**无**，`TargetResponseTime` 平均 302ms。

## 运行与伸缩

```bash
aws ssm start-session --target i-05f0b897988a48d17
sudo trafficgen-scale 8      # 伸缩到 8 个容器
docker logs trafficgen-1
```

容器环境变量（**不需要任何 SSM 权限**，原因见根因文档）：

```
petsiteurl                = http://internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com
searchapiurl              = http://internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com:8081/api/search?
trafficdelaytime          = 1
AWS_REGION                = ap-northeast-1
TRAFFIC_GENERATOR_HEADER  = X-Traffic-Generator:ec2-crossvpc
```

## 能力边界

这套东西**恢复的是真实业务背景流量，不是压测能力**：

- 单容器是**串行**循环，`trafficdelaytime=1` 即约 1 轮/秒，无并发控制、无梯度、无阈值、不输出延迟统计。
- 加压只能靠堆容器数，8 个容器也就百来 RPS 量级。
- 每轮都调 `/housekeeping/` 与 `deletepetadoptionshistory`，对 tier0 Aurora 是持续写入+删除。`trafficdelaytime=1` 已是原 20s 的 **20 倍**，再乘容器数就是几十倍 DB churn，**拉高前应逐档观察 Aurora CPU 与 `DatabaseConnections`**。

正式梯度压测应上 k6（`ramping-arrival-rate` 开环模型，避免 coordinated omission），可直接复用本文档这套内网入口的 `:80` 与 `:8081`。

## 成本

| 项 | 约 |
|---|---|
| c7g.xlarge | $0.145/小时 ≈ $105/月 |
| 内网 ALB | $16/月 + LCU |
| 跨 AZ peering 流量 | $0.01/GB/方向，约一半请求跨 AZ |

`aws ec2 stop-instances --instance-ids i-05f0b897988a48d17` 即停计算费用；容器带 `--restart unless-stopped`，开机自动恢复。

## 完整拆除顺序

见 `todo/loadtest/petsite-loadtest-tgb.yaml` 末尾注释，加上：

```bash
aws ec2 terminate-instances --instance-ids i-05f0b897988a48d17
aws ec2 delete-security-group --group-id sg-059b4eaa1565c7c09
aws iam remove-role-from-instance-profile --instance-profile-name petsite-loadgen-ec2-role --role-name petsite-loadgen-ec2-role
aws iam delete-instance-profile --instance-profile-name petsite-loadgen-ec2-role
# 先 detach 两个托管策略再 delete-role
```

# 图谱 MCP Server —— 部署到 Amazon Bedrock AgentCore Runtime

把依赖图谱的 **24 条预置查询**暴露成 MCP 工具，供 AWS DevOps Agent 使用。

---

## 为什么要做这件事

LLM agent 被问「什么依赖 X」时一定会给出答案，因为它不会说「我不知道」。
这张图能真的回答，而且能回答别人回答不了的部分：

- 这条边 `confirmed`，置信度 0.9975，由实验 `exp-...-fis-rds-reboot-20260831-110323`
  确认，观测方退化 63.91%
- 这条边 `refuted`——图上曾声称存在，故障注入证明不成立，**不得用于推理**
- 这条边 `untested`——可以用，但必须声明未经验证

所以本 server 的责任不只是提供数据，还包括把**证据纪律**交给 agent。
这段纪律写在 MCP `initialize` 的 `instructions` 字段里，客户端会把它交给模型。

### 实测：这件事确实有用，但「有用」不等于「会被用上」

2026-09-07，同一个问题问 AWS DevOps Agent 12 次（本 server 已关联在 agent space
`petsite-devops`，24 条查询全部可用）：

| 提问方式 | 会去查图谱 | 比例 |
|---|---|---|
| 不提图谱 | 2/8 | **25%** |
| 点名要求查图谱 | 4/4 | **100%** |

查了的那些引用真实实验 ID（`exp-pay-for-adoption-http-chaos-20260905`）与真实退化
幅度（0.4%），并在证据不足时**主动拒绝下结论**——原话是「无法区分『依赖不传导』
与『注入根本没打到』，不能证伪，故不下结论」。这正是本 server 想要的行为。

没查的那些依据是「FIS 实验模板存在」。**模板是意图，不是结果**：它说明有人打算测，
不说明测过了。这些回答一样成功返回、排版精美、语气自信。

**结论：`instructions` 字段不是可靠通道。** 该字段和工具描述里都已经写着「讨论任何
依赖关系之前先调它」，自发查询率仍只有 25%——这个 agent 是 skill 优先架构（每次调用
都有 `load_skill`），而 agent space 里 7 个 skill 没有一个讲依赖图谱。要让纪律真正
生效得进它先读的那一层。待办见 `todo/demo-site-rebuild/PLAN.md` T13。

逐字全文、每次的 executionId、耗时与判别信号在
`demo/fixtures/agent_unaided_answer.json`，可用
`aws devops-agent list-pending-messages` 按 executionId 逐条取回核对。
展示页是 RCA 的第五个 Tab，门禁 `tests/test_57_rca_agent_tab.py`。

### 一条未能核实的旧记录（保留，但不作为论据）

本文件此前开头写着：2026-09-01 在 SAP 系统上做盲发现验证时，AWS DevOps Agent 从
CloudTrail 挖出了 FIS 实验与发起者（这部分做得很好），但同时**编造了 CWAgent 的
指标值**、用一个**虚构的 iowait 数字**排除了存储瓶颈；为此在 AGENTS.md v2 里加了
最高优先级的 Evidence integrity 规则。

**2026-09-07 复核：这条记录在本仓库无法核实。** 它 2026-09-05 随 `59f41b5` 以散文
形式进入仓库，没有随附对话记录、executionId、指标名或那个 iowait 数值；引用的
`AGENTS.md v2` 既不在本仓库，`petsite-devops` 里也没有 `agents_md` 资产；`todo/`
下最早的记录是 09-04。它发生在另一个系统、另一个 agent space。

它可能是真的——但按本项目自己的判据，**一条无法被第三方核对的论断不能放在承重
位置**，那正是本 server 要求 agent 不要做的事。所以它降级为标注过的轶事：说明设计
动机从哪来，不用来证明任何结论。上面那 12 次采样才是承重证据。

---

## 架构

```
DevOps Agent（petsite-devops，ap-northeast-1）
      │  MCP over HTTP，OAuth bearer token
      ▼
AgentCore Runtime（serverProtocol=MCP，networkMode=VPC）
      │  ① 先做 JWT 校验（验签 / issuer / audience / 过期）
      │  ② 通过后代理到容器 0.0.0.0:8000/mcp
      ▼
graph-mcp 容器（mcp/agentcore_app.py）
      │  24 个只读工具 = QUERY_CATALOG 现算
      ▼
Neptune petsite-neptune（vpc-010ab37a3f9f74725，同 VPC 私有访问）
```

### 为什么选 AgentCore Runtime 而不是 Gateway

| | AgentCore Runtime | AgentCore Gateway |
|---|---|---|
| MCP 协议 | 自己实现（本仓 `server.py`） | 由 Gateway 从 Lambda/OpenAPI 目标自动生成 |
| `initialize.instructions` | **完全可控** | 由 Gateway 生成，无法注入自定义纪律 |
| 结论 | ✅ 选它 | ❌ 会丢掉本方案的核心价值 |

证据纪律必须能进 `instructions`，这是选 Runtime 的决定性理由。

### 为什么 DevOps Agent 只能走 OAuth/bearer

实测 `devops-agent` API 版本 **2026-01-01**（botocore 1.42.97）的
`RegisterService.serviceDetails.mcpserver.authorizationConfig` 只有五种：
`oAuthClientCredentials` / `oAuth3LO` / `apiKey` / `bearerToken` /
`authorizationDiscovery`。**没有 SigV4。**

而 AgentCore Runtime 的 `InvokeAgentRuntime` 默认要 SigV4——两边对不上。
接口是 `authorizerConfiguration.customJWTAuthorizer`：给 Runtime 配一个
Cognito JWT authorizer，DevOps Agent 就能以 `oAuthClientCredentials` 连上。

> ⚠️ 更正一处旧记录：先前 SAP 工作里记的 `configuration.mcpserversigv4`
> 在当前 API 版本里**不存在**。别按 SigV4 设计。

---

## 前提条件

| 项 | 值 / 说明 |
|---|---|
| 区域 | `ap-northeast-1`（与 Neptune、DevOps Agent space 同区） |
| Neptune | `petsite-neptune`，VPC `vpc-010ab37a3f9f74725`，子网 `subnet-0f801fa79077eb277` / `subnet-047a94f9c5ab6302a` |
| DevOps Agent space | `petsite-devops` / `60c2f48f-b6e3-4dce-a0a3-4144228b2051` |
| 架构 | **arm64**（AgentCore Runtime 要求） |
| Docker | 本机没有（见 `todo/cdk-live-reconcile-stage1_20260830-1440.md`），构建需在有 Docker 的主机上做 |

---

## 部署步骤

### 1. 构建并推送镜像（arm64）

```bash
cd <repo>            # 构建上下文必须是仓库根
ACCT=926093770964
REGION=ap-northeast-1
REPO=graphdp-mcp
TAG=$(git rev-parse --short HEAD)        # 用内容标识，不要用 :latest

aws ecr create-repository --repository-name $REPO --region $REGION 2>/dev/null || true
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCT.dkr.ecr.$REGION.amazonaws.com

docker build --platform linux/arm64 -f mcp/Dockerfile -t $REPO:$TAG .
docker tag $REPO:$TAG $ACCT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG
docker push $ACCT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG
```

> **不要用固定 `:latest`。** 本仓已踩过：镜像内容变而 tag 不变时，
> 编排层看不到 spec 变化、不触发滚动更新，`imagePullPolicy: Always`
> 也救不了（它只在创建容器时生效）。

### 2. 建执行角色

Runtime 需要一个角色，信任 `bedrock-agentcore.amazonaws.com`，权限包含：

- `neptune-db:ReadDataViaQuery`（限 `petsite-neptune` 集群资源）
- `ecr:GetAuthorizationToken`、`ecr:BatchGetImage`、`ecr:GetDownloadUrlForLayer`
- `logs:CreateLogStream`、`logs:PutLogEvents`
- VPC 模式还需 `ec2:CreateNetworkInterface` / `DescribeNetworkInterfaces` /
  `DeleteNetworkInterface`

**刻意不给**任何写图权限（`neptune-db:WriteDataViaQuery`）、不给 FIS/EKS 权限——
这个 server 是只读的，镜像里也没有能发起故障注入的代码。

### 3. 建 Cognito JWT authorizer 所需的资源

DevOps Agent 侧用 client-credentials 流，所以需要：

- 一个 Cognito user pool + resource server（定义 scope，如 `graphmcp/invoke`）
- 一个 **machine-to-machine** app client（client credentials 授权类型）
- discovery URL：`https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/openid-configuration`

### 4. 创建 AgentCore Runtime

```bash
aws bedrock-agentcore-control create-agent-runtime \
  --region ap-northeast-1 \
  --agent-runtime-name graph-dependency-mcp \
  --description "图谱 24 条只读查询的 MCP server，带出处与证据纪律" \
  --agent-runtime-artifact '{
    "containerConfiguration": {
      "containerUri": "926093770964.dkr.ecr.ap-northeast-1.amazonaws.com/graphdp-mcp:<TAG>"
    }
  }' \
  --role-arn arn:aws:iam::926093770964:role/<GraphMcpAgentCoreRole> \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --network-configuration '{
    "networkMode": "VPC",
    "networkModeConfig": {
      "subnets": ["subnet-0f801fa79077eb277","subnet-047a94f9c5ab6302a"],
      "securityGroups": ["<允许出站到 Neptune 8182 的 SG>"]
    }
  }' \
  --authorizer-configuration '{
    "customJWTAuthorizer": {
      "discoveryUrl": "https://cognito-idp.ap-northeast-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration",
      "allowedClients": ["<M2M_CLIENT_ID>"]
    }
  }' \
  --environment-variables '{
    "NEPTUNE_ENDPOINT":"petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
    "REGION":"ap-northeast-1",
    "AWS_DEFAULT_REGION":"ap-northeast-1"
  }'
```

`NEPTUNE_ENDPOINT` **不要带 `:8182`** —— 客户端会再拼一次端口。

安全组要求：Neptune 的 SG（`sg-00590f44d50a19e5f`）需允许来自 Runtime SG 的
8182 入站。

### 5. 注册到 DevOps Agent 并关联

```bash
SPACE=60c2f48f-b6e3-4dce-a0a3-4144228b2051

# 注册为 mcpserver 类型
aws devops-agent register-service --region ap-northeast-1 \
  --service mcpserver \
  --service-details '{
    "mcpserver": {
      "name": "graph-dependency",
      "endpoint": "<AgentCore Runtime 的 MCP 端点 URL>",
      "description": "依赖图谱：24 条只读查询，含故障注入验证判定",
      "authorizationConfig": {
        "oAuthClientCredentials": { ... }
      }
    }
  }'

# 关联到 space —— tools 白名单 + 全部标 READ_ONLY
aws devops-agent associate-service --region ap-northeast-1 \
  --agent-space-id $SPACE \
  --service-id <上一步返回的 serviceId> \
  --configuration file://mcp/devops-agent-association.json
```

工具白名单见 `mcp/devops-agent-association.json`。**24 个工具全部
`toolClassification: READ_ONLY`** —— MCP 工具层是独立于 IAM 的第二个权限平面，
必须按工具名显式白名单，不能只依赖 IAM 收紧。

---

## 验证

### 本地（不经 AgentCore）

```bash
cd mcp
NEPTUNE_ENDPOINT=<cluster-endpoint> REGION=ap-northeast-1 \
  PORT=8010 BIND_HOST=127.0.0.1 python3 agentcore_app.py

# 健康检查（刻意不碰 Neptune）
curl -s http://127.0.0.1:8010/ping

# initialize —— 检查 instructions 里有证据纪律
curl -s -X POST http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 覆盖率
curl -s -X POST http://127.0.0.1:8010/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"q23_verification_coverage","arguments":{}}}'
```

实测记录（2026-09-05 06:02，本机 → 活图谱）：健康检查 200 且不碰 Neptune、
`Mcp-Session-Id` 被正确忽略、`tools/list` 返回 24 个、`q23` 返回真实覆盖率、
通知回 202 无体、坏 JSON 回 `-32700`。

### 单元测试

```bash
python3 -m pytest tests/test_42_mcp_server.py tests/test_43_mcp_agentcore_transport.py -q
# 40 + 14 = 54 passed
```

其中三条测试专门钉住容易被无意破坏的东西：
`test_defaults_match_agentcore_contract`（端口 8000 / 绑 0.0.0.0 / 路径 /mcp
是 AgentCore 硬要求）、`test_health_check_does_not_require_neptune`（否则图谱
抖动会让 runtime 被判不健康重启）、`test_platform_injected_session_id_is_not_rejected`。

### 接上 DevOps Agent 之后

在 `petsite-devops` 上重跑一次盲发现调查，重点看它是否还会编造依赖关系。
判据不是「它答得好不好」，而是：

1. 结论里有没有引用具体的 `verify_status` 与实验 ID
2. 对 `untested` 的边有没有声明「未经验证」
3. 有没有出现本 server 没返回过的指标数值（这是 2026-09-01 那次的失败模式）

---

## 两个容易搞错的地方

**① `q20` 与 `q22` 不是一回事。** 名字里都有 verification：

| | 层次 | 字段 | 回答的问题 |
|---|---|---|---|
| `q20_dependency_verification` | 观测层 | `drift_status` / `runtime_verified` / `verified_by` | 这条边最近有没有被观测到？ |
| `q22_edge_verification_verdicts` | 干预层 | `verify_status` / `verify_degradation` / `verify_experiment` | 在目标端注入故障，源端会不会退化？ |

一条边可以「天天被观测到」同时「注入实验里未能确认」——那种矛盾正是本平台
想暴露的东西。工具描述与 `instructions` 里都已划清，并有测试
（`test_q20_and_q22_are_not_conflated`）守着。

**② `refuted` 可能为 0，那不代表没做验证。** 2026-09-05 引入了「独立证据门禁」：
只要该边被任何独立观测源看到过（如 DeepFlow 调用计数非零），就永不得判 refuted。
这是保守设计——宁可判未定，也不误删一条真实的边。`instructions` 里已写明，
免得 agent 把 `refuted=0` 读成「这套机制没在跑」。

---

## 非 AgentCore 的备选路径

`mcp/handler.py` 是 Lambda（Function URL / API Gateway）传输层，用
api-key（Secrets Manager 取值，`X-API-Key` 头）鉴权，与该 agent space 上
已有的 `api-cn` server 一致。

两者的关键差异：**AgentCore 路径下 MCP 服务自身不做认证**（Runtime 在请求
到达容器前已完成 JWT 校验，它就是安全边界）；Lambda 路径下必须自己校验，
所以 `handler.py` 在未配置密钥时**拒绝**而不是放行——一个能读全图依赖关系的
端点不该因为忘配密钥就变成匿名可读。

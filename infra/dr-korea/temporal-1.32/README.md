# 韩国灾备站点运维脚本

## 先看这张表：每个脚本在**哪台机器**上跑

这一栏被搞错过两次，所以放在最前面。分工不是随意的，见下面「为什么分开」。

| 脚本 | 在哪跑 | 用什么身份 | 需要的权限 |
|---|---|---|---|
| `deploy-worker.sh --publish` | 操作者机器 | IAM 用户 | `s3:PutObject` on `worker/*` |
| `deploy-worker.sh --provision` | **韩国 EC2**（经 SSM） | 实例角色 | `s3:GetObject` on `worker/*` |
| `provision-mcp-credential.sh` | 操作者机器 | IAM 用户 | `cognito-idp:DescribeUserPoolClient`、`secretsmanager:CreateSecret` |
| `iam-grants.sh` | 操作者机器 | IAM 用户 | `iam:PutRolePolicy` |
| `network-korea-to-neptune.sh` | 操作者机器 | IAM 用户 | `ec2:CreateRoute`、`ec2:AuthorizeSecurityGroupIngress` |
| `rebuild.sh` | **韩国 EC2** | root | 本地 docker |

韩国 EC2 是 `i-09380e417a0177ed4`（`dr-korea-temporal`，10.20.1.10，无公网 IP，经 SSM 进）。

## 为什么要分开 —— 这是权限分离，不是不方便

实测（2026-09-26，`simulate-principal-policy`）韩国实例角色对这三项全是 `implicitDeny`：

```
cognito-idp:DescribeUserPoolClient   implicitDeny
secretsmanager:CreateSecret          implicitDeny
iam:PutRolePolicy                    implicitDeny
```

**这是对的，不要去补。** 两处具体后果：

1. **worker 不能改写自己的代码来源。** 实例角色对 `worker/*` 只有 `GetObject`。
   一个能改写自己下次要执行什么的进程，它的「已审核、已演练」结论一文不值 ——
   计划审批、演练闸门全都还在，而被执行的代码可以在两次审批之间被执行者自己换掉。
   这就是 `deploy-worker.sh` 必须显式指定 `--publish` / `--provision` 的原因。

2. **worker 不能自己向 Cognito 索取凭证。** 如果它能调 `describe-user-pool-client`，
   它就能直接取到 client secret，那 Secrets Manager 这一层完全没有意义。
   worker 只该**读**存好的那一份，不该能创建或轮换它。

## 首次接通图谱 MCP 的顺序

顺序承重 —— 每一步都依赖前一步，且第 4 步的验证会因为缺第 3 步而失败。

```bash
# ── 操作者机器 ──
cd infra/dr-korea/temporal-1.32

# 1. 把 Cognito app client 的 secret 搬进 Secrets Manager（东京）
#    注意：那个 client secret **早就存在**，是 Cognito 建 app client 时生成的。
#    本脚本只是把它搬到 worker 能读的地方，不是造一个新凭证。
bash provision-mcp-credential.sh --dry-run
bash provision-mcp-credential.sh --apply     # 结尾会真的换一次 token 作为判据

# 2. 授权：B 方案唯一需要的新权限
#    走 JWT 时**不需要** bedrock-agentcore:InvokeAgentRuntime ——
#    调用由 Bearer token 授权而非 SigV4
bash iam-grants.sh --apply mcp-secret-read

# 3. 发布 worker 代码
DR_CODE_BUCKET=dr-korea-agentcore-926093770964-ap-northeast-2 \
  bash deploy-worker.sh --publish

# ── 韩国 EC2（经 SSM）──
# 4. 部署并验证（含冒烟：起一条真的 DrPlanWorkflow）
sudo bash deploy-worker.sh --provision
```

## 判据纪律

这套脚本里所有验证都刻意做成**三态**：成功 / 明确失败 / **无法判断**。
第三种以退出码 2 结束，不当成通过。

原因是本项目反复撞上同一类故障 —— 一个判据在成功和失败时给出相同的输出：

| 假判据 | 它分不出什么 |
|---|---|
| 「worker 在队列上接单」 | 新代码 / 旧代码（进程还拿着旧模块） |
| 「安全组规则存在」 | 通 / 不通（只有真发一次 TCP 连接才知道） |
| 「`simulate-principal-policy` 报 allowed」 | 策略里有这条 / 真实请求会被放行（neptune-db 是字面匹配） |
| 「secret 建好了」 | 值对 / 值空、键名错、scope 不符 |
| 「`git` 提交里有这个修复」 | 改的是被构建的那份源码 / 不被构建的副本 |

最后一行是 2026-09-26 真实发生的：`SearchController.java` 有两份，
改了不被构建的那份，修复静默无效。**这也是「不另建第二个 MCP runtime」的理由** ——
两个 runtime 同时提供「图谱事实」，后果一样。

## 已定案的两个架构决定

**不直连 Neptune。** 理由是查询质量：直连意味着 worker 自己写 openCypher，
那样快照记录的不是「图谱事实」而是「某次临时查询的偶然结果」。
改经东京 `graph_dependency_mcp` 的受审 `QUERY_CATALOG`，结果带溯源
（`graph_contract_version` / `query` / `params` / `queried_at`）。
`network-korea-to-neptune.sh` 保留作为记录，但**不再是推荐路径**，
它加的 2 条路由 + 1 条安全组规则应当 `--revert`。

**用现有的 Cognito m2m client，不另建 SigV4 runtime。** 见上面判据纪律的最后一行。

# DR Plan Generator — Agent Instructions

> 通用 Agent 指令文件，适用于 OpenClaw / Claude Code / kiro-cli 及任何支持 Markdown 指令的 AI Agent。

## 激活条件

用户提到以下关键词时激活本指令：
- 容灾切换 / DR 计划 / failover plan / 灾备 / 切换演练
- 影响评估 / 单点故障 / SPOF 检测
- RTO / RPO / 回滚计划
- DR drill / disaster recovery / switchover plan

## 工作目录

```bash
cd <project-root>/dr-plan-generator
```

## 前置条件

- Python 3.12+
- `pip install -r requirements.txt`（boto3, requests, pyyaml）
- 环境变量 `NEPTUNE_ENDPOINT`（Neptune 集群端点）— 不设置时可用 mock 数据
- 环境变量 `REGION`（默认 ap-northeast-1）

---

## 交互流程

### Step 1: 理解需求

询问并确认三个关键参数：

| 参数 | 说明 | 选项 |
|------|------|------|
| **scope** | 故障范围（必须） | `region` / `az` / `service` |
| **source** | 故障源（必须） | Region 名 / AZ 名 / 服务名列表 |
| **target** | 切换目标（可基于图谱建议） | 目标 Region / AZ |

**常见场景快速映射**：
- "AZ1 挂了" → `--scope az --source apne1-az1`
- "东京 Region 不可用" → `--scope region --source ap-northeast-1`
- "petsite 和 petsearch 要切" → `--scope service --services petsite,petsearch`

### Step 2: 影响评估（推荐先做）

运行影响评估，帮用户了解故障范围再做决策：

```bash
python3 main.py assess --scope <scope> --failure <source> --format json
```

**解读输出并向用户展示**：
- 受影响服务数量和 Tier 分布（Tier0 最关键）
- 单点故障风险（⚠️ 重点警告）
- 预估 RTO/RPO
- 如果发现 SPOF，**主动警告用户**并建议后续架构改进

### Step 3: 确认参数

基于影响评估结果，与用户确认：
- 目标 Region/AZ（可以基于图谱分析给出建议）
- 是否排除某些服务（`--exclude petfood,pethistory`）
- 确认后进入计划生成

### Step 4: 生成计划

```bash
python3 main.py plan \
  --scope <scope> \
  --source <source> \
  --target <target> \
  [--exclude service1,service2] \
  --format markdown
```

**展示计划摘要**：
- Phase 数量和每个 Phase 的作用
- 总步骤数
- 预估 RTO
- 需要人工审批的关键步骤
- 单点故障风险

### Step 5: 迭代调整

用户可能要求：
- **排除服务** → 加 `--exclude` 参数重新生成
- **调整切换策略** → 修改后重新生成
- **查看某个 Phase 细节** → 从输出中摘取展示
- **换输出格式** → `--format json` 给程序消费

### Step 6: 后续操作（询问用户）

| 操作 | 命令 |
|------|------|
| 生成回滚计划 | `python3 main.py rollback --plan plans/<plan-id>.json` |
| 验证计划 | `python3 main.py validate --plan plans/<plan-id>.json` |
| 导出 chaos 验证实验 | `python3 main.py export-chaos --plan plans/<plan-id>.json --output ../chaos/code/experiments/dr-validation/` |
| 换 JSON 格式 | `python3 main.py plan ... --format json` |

---

## CLI 完整参考

### plan — 生成切换计划

```bash
python3 main.py plan \
  --scope region|az|service \
  --source <故障源> \
  --target <切换目标> \
  [--exclude svc1,svc2] \
  [--format markdown|json] \
  [--output-dir plans] \
  [--non-interactive]
```

### assess — 影响评估

```bash
python3 main.py assess \
  --scope region|az|service \
  --failure <故障源> \
  [--format markdown|json]
```

### validate — 验证已有计划

```bash
python3 main.py validate --plan <plan.json>
# 返回 PASS/FAIL + 问题列表
```

### rollback — 生成回滚计划

```bash
python3 main.py rollback \
  --plan <plan.json> \
  [--format markdown|json] \
  [--output-dir plans]
```

### export-chaos — 导出 chaos 验证实验

```bash
python3 main.py export-chaos \
  --plan <plan.json> \
  --output <实验输出目录>
```

---

## 计划结构说明

生成的计划包含 5 个 Phase，Agent 在展示时应解释每个 Phase 的作用：

| Phase | 名称 | 作用 | 关键点 |
|-------|------|------|--------|
| Phase 0 | Pre-flight Check | 预检 | 连通性、Replication Lag、DNS TTL 降低 |
| Phase 1 | Data Layer | 数据层切换 | RDS failover、DynamoDB Global Table、SQS — **最关键，出错影响最大** |
| Phase 2 | Compute Layer | 计算层切换 | EKS 微服务按 Tier 顺序扩容、Lambda 验证 |
| Phase 3 | Network Layer | 流量层切换 | ALB 健康检查、Route 53 DNS 切换 |
| Phase 4 | Validation | 切换后验证 | 端到端验证、性能基线对比 |

**切换顺序原则**：数据先行（L0）→ 计算跟上（L1+L2）→ 流量最后（L3）
**回滚顺序原则**：流量先撤（L3）→ 计算停掉（L2+L1）→ 数据回切（L0）

---

## 输出文件位置

- 计划文件：`dr-plan-generator/plans/<plan-id>.md` 和 `.json`
- 示例文件：`dr-plan-generator/examples/` （可作为参考展示给用户）

---

## 常见问题处理

| 问题 | 解决 |
|------|------|
| Neptune 连接失败 | 检查 `NEPTUNE_ENDPOINT` 环境变量；可用 examples/ 中的 mock 数据演示 |
| 图谱数据过旧 | 建议先跑 ETL：`aws lambda invoke --function-name neptune-etl-from-aws --region ap-northeast-1 /tmp/out.json` |
| 某资源类型没有切换命令 | step_builder 生成 generic step 标记为 `manual_review`，提示用户手动补充 |
| 计划验证发现环路 | 检查 Neptune 图谱中是否有错误的循环依赖边 |
| RTO 估算不准 | 默认时间基于经验值，用户可根据实际 DR 演练数据调整 `rto_estimator.py` 中的 `DEFAULT_TIMES` |

---

## 对话示例

```
用户: AZ1 挂了，帮我出切换计划
Agent: 先做个影响评估...
       → 运行: python3 main.py assess --scope az --failure apne1-az1 --format json
       "AZ1 影响 7 个服务（含 4 个 Tier0），14 个资源。
        ⚠️ petsite-db (RDS) 只在 AZ1，是单点故障风险。
        建议切到 AZ2+AZ4。要排除什么服务吗？"

用户: 排除 petfood，其他都切
Agent: → 运行: python3 main.py plan --scope az --source apne1-az1 --target apne1-az2,apne1-az4 --exclude petfood
       "计划已生成：5 Phase、18 Step，预估 RTO 32 分钟。
        Phase 1 数据层有 3 个步骤需要审批（RDS failover + DynamoDB + SQS）。
        要看详细步骤还是生成回滚计划？"

用户: 生成回滚计划
Agent: → 运行: python3 main.py rollback --plan plans/dr-az-xxx.json
       "回滚计划已生成，14 个步骤，全部需要审批。
        要导出为 chaos 验证实验来提前验证吗？"
```

# PetSite 故障恢复与根因分析系统 — 技术设计文档

> 版本：v0.1（草稿）
> 作者：小乖乖
> 日期：2026-02-28
> 依赖：Amazon Neptune 知识图谱、DeepFlow、CloudWatch、AWS SDK

---

## 一、背景与目标

### 1.1 当前能力

PetSite 已具备：
- **Neptune 知识图谱**：171 节点、309 条边，实时反映服务依赖拓扑、AZ 分布、恢复优先级
- **DeepFlow**：L7 流量实时可观测，微服务调用链、错误率、延迟
- **CloudWatch**：基础设施指标（CPU、内存、ALB 4xx/5xx）
- **ETL 自动更新**：图谱每 5 分钟/2 小时自动刷新

### 1.2 需要解决的问题

> "不管发现什么，先尽可能快速恢复业务，之后再做故障诊断"
> "但很多严重的问题，可能还是要快速定位再去恢复"

这两句话揭示了系统的核心设计张力：

| 场景 | 策略 |
|------|------|
| 影响范围清晰、有成熟恢复手段 | **先恢复，后诊断**（Restore-First） |
| 根因不明、盲目恢复可能扩大故障 | **快速定位，精准恢复**（Diagnose-First） |
| 级联故障、多服务异常 | 按 recovery_priority 分层恢复 |

### 1.3 设计目标

1. **故障感知**：自动检测异常，识别受影响的业务能力（BusinessCapability）
2. **快速恢复**：基于图谱依赖关系，生成恢复行动建议，支持一键/自动执行
3. **根因分析**：利用 DeepFlow 调用链 + 图谱依赖，定位根因节点
4. **闭环记录**：每次故障自动归档，积累修复知识库

---

## 二、核心概念

### 2.1 故障分类（决定走 Restore-First 还是 Diagnose-First）

```
┌──────────────────────────────────────────────────────┐
│                    故障严重度                          │
├─────────────┬──────────────┬──────────────────────────┤
│  P0 全站中断  │  P1 核心链路  │  P2 非核心/单服务异常       │
├─────────────┴──────────────┴──────────────────────────┤
│                    恢复策略                            │
├─────────────┬──────────────┬──────────────────────────┤
│ Diagnose-   │  两路并行     │  Restore-First            │
│ First       │              │                           │
└─────────────┴──────────────┴──────────────────────────┘
```

**P0**（petsite/petsearch/payforadoption 全部不可用）→ 先定位，因为盲目重启可能让情况更糟
**P1**（Tier0 单服务或 Tier1 多服务）→ 并行：立即执行成熟恢复手段 + 同步开始诊断
**P2**（Tier1/Tier2 单服务）→ 先恢复（重启/扩容），后诊断

### 2.2 Neptune 图谱在系统中的角色

```
故障告警
    │
    ▼
影响图谱查询 ──→ 哪些 BusinessCapability 受影响？
    │              哪些上游/下游服务也会受波及？
    │              受影响服务的 fault_boundary / recovery_priority？
    ▼
恢复策略生成 ──→ 按 recovery_priority 排序
    │              根据 fault_boundary（az/region）决定恢复范围
    │              查找历史相似故障的恢复经验（知识库）
    ▼
执行 & 验证 ──→ 执行恢复动作
               实时监控验证恢复效果
               若失败则升级策略
```

---

## 三、系统架构

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        信号输入层                                 │
│  CloudWatch Alarm  │  DeepFlow 异常检测  │  用户反馈/手动触发     │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     故障感知引擎（Fault Detector）                 │
│  • 异常信号聚合与去重                                              │
│  • 故障严重度评估（P0/P1/P2）                                      │
│  • 触发后续流程                                                    │
└──────────────┬──────────────────────────────────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌──────────────────────────────────────────────┐
│ 根因分析引擎  │  │            快速恢复引擎（Recovery Engine）      │
│ (RCA Engine) │  │                                              │
│              │  │  ┌─────────────────────────────────────┐   │
│ • 图谱路径   │  │  │        图谱查询层（Neptune）           │   │
│   分析       │  │  │  • 受影响服务 & BusinessCapability    │   │
│ • DeepFlow  │  │  │  • 依赖链路（上下游）                  │   │
│   调用链回放  │  │  │  • fault_boundary / priority        │   │
│ • 历史故障   │  │  └─────────────────────────────────────┘   │
│   模式匹配   │  │                   │                          │
│              │  │                   ▼                          │
│ 输出：       │  │  ┌─────────────────────────────────────┐   │
│ 根因节点     │  │  │       恢复策略生成器                   │   │
│ 置信度       │  │  │  • Playbook 匹配（已知故障模式）       │   │
│ 修复建议     │  │  │  • 动态策略生成（图谱推断）             │   │
└──────┬───────┘  │  └─────────────────────────────────────┘   │
       │          │                   │                          │
       │          │                   ▼                          │
       │          │  ┌─────────────────────────────────────┐   │
       │          │  │       执行层（Action Executor）       │   │
       │          │  │  • 建议模式：输出操作清单，人工确认    │   │
       │          │  │  • 半自动：低风险操作自动执行          │   │
       │          │  │  • 全自动：预定义 Playbook 全自动      │   │
       │          │  └─────────────────────────────────────┘   │
       │          └──────────────────────────────────────────────┘
       │                             │
       └──────────┬──────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                      闭环记录层                                   │
│  • 故障时间线归档                                                  │
│  • 恢复有效性评分                                                  │
│  • 知识库更新（成功/失败的恢复经验）                                 │
│  • 存储：S3 + Neptune（新增 IncidentNode）                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 四、核心模块设计

### 4.1 故障感知引擎（Fault Detector）

**输入信号：**
```python
{
    "source": "cloudwatch_alarm | deepflow | manual",
    "affected_resource": "arn:aws:... | pod_name | service_name",
    "metric": "error_rate | latency_p99 | availability",
    "value": 0.95,        # 错误率 95%
    "threshold": 0.05,    # 正常阈值 5%
    "timestamp": "2026-02-28T00:00:00Z"
}
```

**严重度评估逻辑（基于 Neptune）：**
```gremlin
// 查找受影响的 BusinessCapability
g.V().has('name', $affected_service)
 .in('Serves', 'DependsOn')
 .hasLabel('BusinessCapability')
 .values('name', 'recovery_priority')
```

- 影响 Tier0 BusinessCapability → P0/P1
- 仅影响 Tier1/Tier2 → P1/P2
- fault_boundary=az 且单 AZ 故障 → 可快速切换，降级处理

---

### 4.2 图谱查询层（Neptune Queries）

**核心查询集合：**

**Q1. 影响面评估**
```gremlin
// 给定故障节点，找所有受影响的下游服务和 BusinessCapability
g.V().has('name', $failed_node)
 .repeat(out('CallsTo', 'DependsOn').simplePath())
 .until(hasLabel('BusinessCapability').or().loops().is(5))
 .path()
```

**Q2. 恢复路径推断**
```gremlin
// 找所有 Tier0 服务的 fault_boundary 和 az 分布
g.V().has('recovery_priority', 'Tier0')
 .project('name', 'fault_boundary', 'az', 'replicas')
 .by('name').by('fault_boundary').by('az').by('replicas')
```

**Q3. 上游依赖查询**（找根因候选）
```gremlin
// 找直接依赖了故障服务的所有节点
g.V().has('name', $failed_service)
 .in('CallsTo', 'DependsOn')
 .values('name', 'type', 'recovery_priority')
```

---

### 4.3 恢复策略生成器（Playbook Engine）

#### 预定义 Playbook（已知故障模式）

**Playbook 1：单 AZ 不可用**
```yaml
trigger:
  condition: fault_boundary == "az" AND single_az_down
steps:
  1. 确认其他 AZ 副本健康
  2. 将故障 AZ 从 ALB target group 摘除（aws elbv2 deregister-targets）
  3. 扩容健康 AZ 副本（kubectl scale）
  4. 验证流量切换完成
  5. 告警降噪（屏蔽故障 AZ 告警 30 分钟）
risk: LOW  # 不影响数据，可自动执行
```

**Playbook 2：Pod CrashLoopBackOff**
```yaml
trigger:
  condition: pod_status == "CrashLoopBackOff"
steps:
  1. 获取 pod logs（最近 100 行）→ AI 分析错误类型
  2. if OOM → 临时增加 memory limit，触发 rollout restart
  3. if ConfigError → 检查 ConfigMap/Secret 最近变更，回滚
  4. if 未知 → 升级人工处理
risk: MEDIUM
```

**Playbook 3：数据库连接池耗尽**
```yaml
trigger:
  condition: rds_connections > 90% OR timeout_errors > threshold
steps:
  1. 立即：重启受影响的应用 Pod（释放连接）
  2. 短期：调大 RDS max_connections（参数组）
  3. 后续诊断：分析 slow query log，找连接泄漏
risk: LOW（重启 Pod 不丢数据）
```

**Playbook 4：全站 5xx 率飙升**
```yaml
trigger:
  condition: alb_5xx_rate > 20% AND duration > 2min
steps:
  1. 查 DeepFlow：哪个服务返回 5xx（调用链分析）
  2. 查 Neptune：该服务的 recovery_priority
  3. if Tier0 → 立即执行 rollout restart + 扩容
  4. if 配置变更导致 → 回滚 deployment（kubectl rollout undo）
  5. if 外部依赖 → 启用 circuit breaker / fallback
risk: DEPENDS（根据步骤 2 结果判断）
```

#### 动态策略生成（无匹配 Playbook 时）

当没有匹配的 Playbook，系统基于图谱推断：

1. 查找故障节点的 `recovery_priority`
2. 查找同类型节点的历史恢复方法（知识库）
3. 基于 `fault_boundary` 判断影响范围
4. 生成候选恢复动作列表，标注风险等级
5. **始终先建议，等待人工确认（无历史 Playbook 不自动执行）**

---

### 4.4 根因分析引擎（RCA Engine）

**分析流程：**

```
Step 1: 时间线对齐
  • 收集故障发生前 30 分钟内的所有变更事件
  • CloudTrail（配置变更）+ ECR（镜像推送）+ kubectl events

Step 2: DeepFlow 调用链分析
  • 找最早出现错误的服务（而非最晚被发现的）
  • 查询：flow_log.l7_flow_log WHERE resp_status >= 500 ORDER BY start_time

Step 3: 图谱路径分析
  • 从故障表象节点出发，沿 DependsOn/CallsTo 反向遍历
  • 找到没有上游故障但自身异常的节点 = 根因候选

Step 4: 置信度评分
  criteria:
    - 时间线最早出现异常: +40分
    - 有近期配置变更: +30分
    - 历史曾发生同类故障: +20分
    - 是其他故障节点的共同依赖: +10分

Step 5: 输出
  {
    "root_cause_candidates": [
      {"node": "payforadoption", "confidence": 0.85, "evidence": [...]}
    ],
    "blast_radius": ["petsite", "PetAdoptionFlow"],
    "recommended_fix": "...",
    "similar_incidents": ["INC-2026-02-24"]
  }
```

---

### 4.5 执行层（Action Executor）

**三种执行模式：**

| 模式 | 触发条件 | 说明 |
|------|---------|------|
| **建议模式**（默认） | 首次故障 / 无匹配 Playbook | 输出操作清单，等待人工确认 |
| **半自动** | 有匹配 Playbook + 风险 LOW | 自动执行，同时通知 Slack |
| **全自动** | 预授权 Playbook + P2 级别 | 全自动执行，事后报告 |

**执行安全机制：**
- 每个自动操作写入审计日志
- 支持一键回滚（`--dry-run` 模式预览）
- 同一服务 30 分钟内自动操作不超过 3 次（防止反复重启循环）
- P0 级别永远不全自动，必须人工确认

**Slack 通知格式：**
```
🚨 [P1] payforadoption 错误率 87%
影响业务：PetAdoptionFlow (Tier0)
根因候选：payforadoption pod CrashLoopBackOff (置信度 85%)
已执行：rollout restart ✅
等待确认：扩容至 6 副本 [确认] [跳过]
```

---

## 五、数据模型扩展

### 5.1 Neptune 新增节点：Incident

```gremlin
// 故障节点
mergeV([
  'label': 'Incident',
  'id': 'inc-2026-02-28-001'
]).option(onCreate, [
  'severity': 'P1',
  'start_time': '2026-02-28T00:00:00Z',
  'status': 'resolved',
  'ttfr': 420,         // Time to First Response (秒)
  'mttr': 1800,        // Mean Time to Recover (秒)
  'root_cause': 'payforadoption OOM',
  'resolution': 'rollout restart + memory limit increase'
])

// 故障影响关系
addE('TriggeredBy').from(V('inc-...'))to(V('payforadoption'))
addE('Impacted').from(V('inc-...'))to(V('PetAdoptionFlow'))
```

**用途：**
- 历史故障模式匹配
- MTTR 趋势分析
- 同一服务重复故障告警（3次/周 → 触发深度 RCA）

---

## 六、开发阶段规划

### Phase 1：基础框架（2周）

**目标：可用的建议模式**

- [ ] 故障感知：CloudWatch Alarm → Lambda → 触发分析
- [ ] Neptune 查询层封装（Q1/Q2/Q3 基础查询）
- [ ] 影响面评估（受影响的 BusinessCapability + Tier）
- [ ] Slack 通知：故障告警 + 影响面报告
- [ ] 基础 Playbook 匹配（4个 Playbook 先实现）
- [ ] 输出：Slack 中展示恢复建议清单（人工执行）

**技术选型：**
- Lambda（Python）+ Neptune（openCypher）+ DeepFlow API
- 触发器：CloudWatch Alarm SNS → Lambda
- 通知：Slack Webhook

### Phase 2：执行自动化（2周）

**目标：半自动执行低风险恢复**

- [ ] Action Executor 实现（kubectl / aws cli 封装）
- [ ] 半自动执行（LOW 风险 Playbook）
- [ ] Slack 交互式确认（按钮确认 / 跳过）
- [ ] 执行审计日志
- [ ] 回滚机制

### Phase 3：RCA 引擎（2周）

**目标：自动根因分析**

- [ ] DeepFlow 调用链查询集成
- [ ] 时间线对齐（CloudTrail + kubectl events）
- [ ] 置信度评分算法
- [ ] RCA 报告生成（Markdown 格式）
- [ ] Incident 节点写回 Neptune

### Phase 4：知识库 & 持续优化（持续）

- [ ] 历史故障知识库（基于 Incident 节点）
- [ ] Playbook 自动推荐（相似故障匹配）
- [ ] MTTR 趋势 Dashboard
- [ ] 全自动模式（P2 场景）

---

## 七、关键技术挑战

### 7.1 故障信号噪声过滤

- 问题：告警风暴（1个根因产生 N 个告警）
- 方案：基于图谱依赖关系去重，同一依赖链上的告警合并为一个 Incident

### 7.2 图谱实时性

- 问题：ETL 每 5 分钟/2 小时更新，故障时图谱可能不是最新的
- 方案：故障触发时强制刷新 DeepFlow ETL，获取最新调用关系

### 7.3 自动执行的安全边界

- 问题：自动重启可能丢失 in-flight 请求，自动扩容有成本
- 方案：
  - 重启前检查 PDB（PodDisruptionBudget），确保最小可用副本
  - 扩容设置上限（不超过 max_replicas × 2）
  - 所有自动操作必须有对应的自动回滚条件

### 7.4 P0 级别的两难

- 根因不明时无法精准修复
- 盲目重启可能触发数据不一致
- **方案：P0 始终走 Diagnose-First，但 RCA 引擎目标 < 3 分钟出结果**

---

## 八、技术栈

| 组件 | 技术选型 | 理由 |
|------|---------|------|
| 故障感知 | CloudWatch Alarm + SNS + Lambda | 已有基础设施 |
| 图谱查询 | Amazon Neptune（openCypher） | 已有图谱 |
| 调用链分析 | DeepFlow API（ClickHouse）| 已有数据 |
| 执行层 | Python Lambda + boto3 + kubectl | 已有权限 |
| 通知 | Slack Webhook / OpenClaw 消息 | 已有渠道 |
| 审计日志 | CloudWatch Logs + S3 | 已有基础设施 |
| 知识库存储 | Neptune（Incident 节点）+ S3（详细报告）| 复用图谱 |

---

## 九、成功指标

| 指标 | 当前基线 | Phase 1 目标 | Phase 3 目标 |
|------|---------|-------------|-------------|
| 故障感知到通知时间 | 手动（分钟级） | < 2 分钟 | < 1 分钟 |
| 影响面识别准确率 | 手动判断 | — | > 90% |
| MTTR（P1 故障） | ~30 分钟 | ~20 分钟 | < 10 分钟 |
| RCA 准确率 | 手动 | — | > 70% 置信度 |
| 自动恢复覆盖率 | 0% | 0%（建议模式）| > 60%（P2）|

---

*下一步：大乖乖确认设计方向后，开始 Phase 1 代码实现。*

---

*参考：*
- *Neptune 图谱设计：`see graph-dp-cdk repo`*
- *ETL 代码：`graph-dp-cdk/lambda/etl_deepflow/` & `etl_aws/`*
- *DeepFlow 查询：`docs/tech-notes.md`*

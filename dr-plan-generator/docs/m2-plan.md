# M2 迭代计划 — 完整切换 + 服务注册表

> 版本：v1.0  
> 日期：2026-03-28  
> 前置：M1 基础能力已完成（70→105 个测试通过）  
> 目标：从"AZ 演示级别"提升到"可用于客户新环境的完整 DR 计划工具"

---

## 状态总览

| 类别 | 已完成 | 待做 | 说明 |
|------|--------|------|------|
| 服务注册表 | ✅ | — | service_types.yaml + registry_loader + 消费方重构 |
| AWS 故障隔离参考 | ✅ | — | 双语 reference 文档 |
| PRD v1.2 | ✅ | — | 注册表设计 + 风险分级 |
| Region 级切换 | ⚠️ 骨架 | 完善 | 基础流程通了，缺 Region 特有逻辑 |
| 回滚生成器 | ⚠️ 骨架 | 完善 | rollback_generator.py 存在，缺测试 |
| 影响评估 | ⚠️ 骨架 | 完善 | impact_analyzer.py 存在，缺测试 |
| SPOF 检测 | ✅ 核心 | 增强 | 已用注册表驱动，缺 Multi-AZ 感知 |
| RTO 估算 | ⚠️ 骨架 | 完善 | rto_estimator.py 存在，需接入注册表 |
| Step Builder 扩展 | ⚠️ 7 种 | 扩展 | 缺 6+ 种常见类型的专用 builder |
| JSON 输出 | ⚠️ 骨架 | 完善 | json_renderer.py 存在，缺测试 |
| 计划验证 | ✅ 核心 | 增强 | 13 个测试通过，可增加更多场景 |
| 测试覆盖 | 105 个 | +60~80 | 8 个模块无测试覆盖 |

---

## 任务清单

### P0 — 必须完成（核心功能）

#### T1: Region 级切换完善 ⬜
**文件**: `graph/queries.py`, `planner/plan_generator.py`, `planner/step_builder.py`  
**现状**: 基础流程可生成 Region 切换计划，但 Region 特有逻辑不完整  
**待做**:
- [ ] Region 切换时自动识别跨 Region 资源（DynamoDB Global Table、Aurora Global Database、S3 CRR）
- [ ] Phase 0 增加：跨 Region 复制延迟检查（replication lag）
- [ ] Phase 1 增加：Region 级数据层切换命令（Aurora Global DB failover、DynamoDB Global Table region switch）
- [ ] 区分"热备"（DR Region 已有基础设施）vs "冷备"（需要预置）
- [ ] Region 切换计划标注控制面/数据面风险等级（🟢🟡🔴）

**完成标准**: Region example 包含跨 Region 资源识别 + 复制延迟检查 + 风险标注

#### T2: Step Builder 扩展 ⬜
**文件**: `planner/step_builder.py`  
**现状**: 7 种专用 builder（RDSCluster, RDSInstance, DynamoDBTable, Microservice, LoadBalancer, LambdaFunction, K8sService）  
**待做**:
- [ ] `_build_ec2instance_step` — EC2 实例（启动备用 / AMI 恢复 / ASG 调整）
- [ ] `_build_neptunecluster_step` — Neptune 集群（promote read replica）
- [ ] `_build_sqsqueue_step` — SQS 队列（验证/重建 + 死信队列处理）
- [ ] `_build_s3bucket_step` — S3 桶（CRR 验证 + 端点切换）
- [ ] `_build_targetgroup_step` — Target Group（deregister/register targets）
- [ ] `_build_stepfunction_step` — Step Functions（验证状态机 + 切换触发器）
- [ ] `_build_elasticachecluster_step` — ElastiCache（Global Datastore failover）
- [ ] `_build_ekscluster_step` — EKS 集群（节点组扩缩 + workload 验证）

**完成标准**: generic fallback 的 WARNING 数量从当前 6 种降到 ≤ 2 种；每个新 builder 有对应测试

#### T3: 回滚生成器完善 + 测试 ⬜
**文件**: `planner/rollback_generator.py`, `tests/test_rollback.py`  
**现状**: 120 行代码，基本逻辑已实现（反转 Phase 顺序 + swap command/rollback_command）  
**待做**:
- [ ] 数据层回滚特殊处理（避免数据丢失 — 回滚前加 snapshot 步骤）
- [ ] 回滚计划所有步骤 requires_approval=True（PRD 要求）
- [ ] 回滚顺序验证（流量→计算→数据，与切换相反）
- [ ] 新增 `tests/test_rollback.py`（≥10 个测试）

**完成标准**: 回滚 example 生成正确，10+ 测试通过

#### T4: 影响评估完善 + 测试 ⬜
**文件**: `assessment/impact_analyzer.py`, `tests/test_impact.py`  
**现状**: 127 行代码，基础评估逻辑  
**待做**:
- [ ] 接入注册表（故障域判断用 registry 而非硬编码）
- [ ] 按 Tier 分组的受影响服务列表
- [ ] BusinessCapability 影响链分析
- [ ] 关键路径识别（Tier0 服务的最长依赖链）
- [ ] 新增 `tests/test_impact.py`（≥10 个测试）

**完成标准**: `main.py assess` 输出完整影响报告，10+ 测试通过

#### T5: RTO 估算接入注册表 ⬜
**文件**: `assessment/rto_estimator.py`, `tests/test_rto.py`  
**现状**: 97 行代码，硬编码 DEFAULT_TIMES  
**待做**:
- [ ] 从注册表读取各类型的预估切换时间
- [ ] 注册表 service_types.yaml 增加 `estimated_switchover_seconds` 字段
- [ ] 并行组时间取 max 而非 sum
- [ ] 新增 `tests/test_rto.py`（≥8 个测试）

**完成标准**: RTO 估算基于注册表数据，8+ 测试通过

---

### P1 — 应该完成（质量提升）

#### T6: 测试补全（8 个无测试模块）⬜
**文件**: 新增 `tests/test_*.py`  
**现状**: 105 个测试，但 8 个模块无覆盖  
**待做**:
- [ ] `tests/test_plan_generator.py` — 端到端计划生成（≥8 个测试）
- [ ] `tests/test_rollback.py` — 回滚生成器（≥10 个测试，含 T3）
- [ ] `tests/test_impact.py` — 影响评估（≥10 个测试，含 T4）
- [ ] `tests/test_rto.py` — RTO 估算（≥8 个测试，含 T5）
- [ ] `tests/test_json_renderer.py` — JSON 输出（≥5 个测试）
- [ ] `tests/test_markdown_renderer.py` — Markdown 输出（≥5 个测试）
- [ ] `tests/test_spof_detector.py` — SPOF 检测独立测试（≥8 个测试，当前仅在 registry 测试中有集成测试）

**完成标准**: 总测试数从 105 提升到 ≥160，所有核心模块有独立测试文件

#### T7: JSON 输出增强 ⬜
**文件**: `output/json_renderer.py`  
**现状**: 52 行基础序列化  
**待做**:
- [ ] 增加 JSON Schema 定义（`docs/plan-schema.json`）
- [ ] 输出包含 metadata（生成时间、图谱快照时间、工具版本、注册表版本）
- [ ] 支持 `--format json` CLI 参数输出纯 JSON 到 stdout（方便管道处理）

**完成标准**: JSON 输出符合 schema，可被外部自动化系统消费

#### T8: 计划验证增强 ⬜
**文件**: `validation/plan_validator.py`  
**现状**: 201 行，13 个测试  
**待做**:
- [ ] 增加"控制面依赖检查" — 标注哪些步骤依赖控制面操作
- [ ] 增加"静态稳定性评分" — 根据控制面依赖数量给计划打分
- [ ] 增加"未识别资源类型"汇总
- [ ] 新增对应测试（≥5 个）

**完成标准**: 验证报告包含风险分级信息

---

### P2 — 可以推迟（到 M3）

#### T9: 并行优化器 ⬜
**文件**: `planner/parallel_optimizer.py`（不存在，需新建）  
**说明**: PRD 中设计了并行步骤检测，但当前 Kahn's 算法已自然支持并行组。独立优化器可推到 M3。

#### T10: CLI `assess` 命令完善 ⬜
**文件**: `main.py`  
**说明**: 当前 `assess` 命令基础可用，完善依赖 T4 影响评估完成后。

#### T11: Markdown 输出增强 ⬜
**文件**: `output/markdown_renderer.py`  
**说明**: 增加控制面/数据面标注、风险等级颜色、注册表未覆盖类型告警章节。

---

## 依赖关系

```
T5 (RTO 接入注册表) ← 独立
T2 (Step Builder 扩展) ← 独立
T1 (Region 切换完善) ← 依赖 T2 部分完成
T4 (影响评估) ← 独立
T3 (回滚完善) ← 独立
T6 (测试补全) ← 依赖 T2-T5 完成
T7 (JSON 增强) ← 独立
T8 (计划验证增强) ← 独立
```

**推荐执行顺序**: T2 → T5 → T1 → T3 → T4 → T6 → T7 → T8

---

## 已完成项（M2 已做的部分）

- [x] **Service Registry** — `registry/service_types.yaml` + `registry_loader.py`（30 种 AWS 服务类型，267+240 行）
- [x] **消费方重构** — graph_analyzer、spof_detector、step_builder 改用注册表
- [x] **Registry 测试** — 35 个新测试全部通过
- [x] **CLI 扩展** — `--custom-registry` 参数
- [x] **AWS 故障隔离参考** — 双语 reference 文档（305 行 × 2）
- [x] **PRD v1.2** — 注册表设计 + 风险分级规则
- [x] **SPOF 修复** — DynamoDB/SQS/S3 不再被误标为 AZ SPOF
- [x] **Example 重新生成** — 3 个 example 更新

---

## 完成标准（M2 整体）

| 指标 | 目标 |
|------|------|
| 测试总数 | ≥ 160（当前 105） |
| 专用 Step Builder | ≥ 13 种（当前 7 种） |
| 无测试模块 | 0 个（当前 8 个） |
| Region 切换 | 包含跨 Region 资源识别 + 风险标注 |
| 回滚计划 | 数据层特殊处理 + 全部需审批 |
| 影响评估 | Tier 分组 + BusinessCapability 链 + 关键路径 |
| JSON 输出 | 有 Schema 定义 + metadata |

---

*创建日期：2026-03-28 | 最近更新：2026-03-28*

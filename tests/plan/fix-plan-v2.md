# Neptune Schema 修复计划 v2

**日期**: 2026-04-16
**来源**: neptune-schema-fix-plan-20260416.md
**项目**: `/home/ubuntu/tech/graph-dependency-platform`

---

## 总览

| Phase | 优先级 | 内容 | 改动量 | 风险 | 状态 |
|-------|--------|------|--------|------|------|
| Phase 1 | P0 | 数据一致性修复 | ~50 行 / 4 文件 | 🟢 低 | 待执行 |
| Phase 2 | P1 | Deployment 节点 + Manages 边 | ~120 行 / 3 文件 | 🟡 中 | 待执行 |
| Phase 3 | P1 | K8sService Routes 边 | ~10 行 / 1 文件 | 🟢 低 | 待执行 |
| Phase 4 | P2 | Namespace 节点 | ~30 行 / 2 文件 | 🟢 低 | 待执行 |
| Phase 5 | P2 | HPA 节点 | ~60 行 / 2 文件 | 🟡 中 | 待执行 |

---

## 执行策略

**每个 Phase 独立 commit + deploy，遵循标准交付流程：**
```
开发 → 本地验证 → 推 GitHub → CDK deploy → 线上验证
```

**全部用 Level 1（edit/write/exec）执行**，每个子任务 < 50 行改动。

---

## Phase 1: P0 — 数据一致性修复

### Task 1.1: Pod.restarts 类型修复
- **文件**: `infra/lambda/etl_aws/neptune_client.py` (upsert_vertex)
- **改动**: 增加 NUMERIC_PROPS 白名单，数值属性不加引号
- **验收**: Neptune 中 Pod.restarts 为 int 类型

### Task 1.2: Microservice.namespace 修复
- **文件**: `infra/lambda/etl_aws/config.py` + `business_layer.py`
- **改动**: 增加 MICROSERVICE_NAMESPACE 映射，替代硬编码 'default'
- **验收**: Neptune 中 petsite 等服务的 namespace 为 'petadoptions'

### Task 1.3: Microservice.has_db_dependency 同步
- **文件**: `infra/lambda/etl_aws/handler.py` (Step 13b 末尾)
- **改动**: AccessesData 边写完后 batch 更新 has_db_dependency
- **验收**: 有数据库连接的 Microservice has_db_dependency=true

### Task 1.4: 清理 error_rate=-1 和 replica_count=-1
- **文件**: `infra/lambda/etl_aws/handler.py` (末尾 cleanup step)
- **改动**: 删除 -1 占位值属性
- **验收**: Neptune 中无 error_rate=-1 的 Microservice

### Task 1.5: Microservice.ip 历史堆积修复
- **文件**: `infra/lambda/etl_aws/collectors/eks.py` + `handler.py`
- **改动**: collect_eks_pods 增加 pod_ip 字段，Step 8b 后覆盖写入 IP 列表
- **验收**: Microservice.ip 只包含当前 Running Pod 的 IP

---

## Phase 2: P1 — Deployment 节点 + Manages 边

### Task 2.1: Deployment 采集器
- **文件**: `infra/lambda/etl_aws/collectors/eks.py`
- **改动**: 新增 collect_k8s_deployments() 函数
- **前置**: 检查 Lambda EKS RBAC 权限（apps/v1 deployments）

### Task 2.2: handler.py Step 8f + schema 更新
- **文件**: `handler.py` + `rca/neptune/schema_prompt.py`
- **改动**: 新增 Step 8f 写入 Deployment 节点 + Manages/Implements 边

### Task 2.3: GC 更新
- **文件**: `handler.py` (graph_gc.py 或内联)
- **改动**: 增加 Deployment 节点的 GC 逻辑

---

## Phase 3: P1 — K8sService Routes 边

### Task 3.1: 增加 Routes 边
- **文件**: `handler.py` (Step 8e) + `schema_prompt.py`
- **改动**: K8sService→Pod 增加 Routes 边（~10 行）

---

## Phase 4: P2 — Namespace 节点

### Task 4.1: Namespace 节点 + 资源关联
- **文件**: `handler.py` + `schema_prompt.py`
- **改动**: 新增 Step 8a 创建 Namespace 节点 + OwnedBy 边

---

## Phase 5: P2 — HPA 节点

### Task 5.1: HPA 采集器 + 节点写入
- **文件**: `collectors/eks.py` + `handler.py` + `schema_prompt.py`
- **前置**: 检查 autoscaling/v2 API 权限
- **改动**: 新增 collect_k8s_hpas + Step 8g

---

## 五要素权衡

| 原则 | 评估 |
|------|------|
| 可维护性 | ✅ 每个 Phase 独立 commit，可单独回滚 |
| 可测试性 | ✅ 每个 Phase 部署后通过 Neptune 查询验证 |
| 可部署性 | ✅ CDK deploy 保证线上一致，无需手动 zip |
| 可扩展性 | ✅ 新节点类型不影响现有查询，按需扩展 |
| 可用性 | ✅ 所有新 step 都是 non-fatal try/except |

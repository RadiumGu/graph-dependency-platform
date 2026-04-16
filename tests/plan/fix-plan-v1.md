# Bug 修复计划 v1

**日期**: 2026-04-16
**来源**: FINAL-SUMMARY.md（测试猫 Sprint 0-9 测试报告）
**目标项目**: `/home/ubuntu/tech/graph-dependency-platform`

---

## Bug 清单

| Bug ID | 优先级 | 状态 | 文件 | 问题 |
|--------|--------|------|------|------|
| BUG-S0-01 | P0 | ✅ 已修复 | `rca/neptune/schema_prompt.py` | FEW_SHOT_EXAMPLES[1] `type(r)` 引用未绑定变量 |
| BUG-S0-02 | P0 | ✅ 已修复 | `rca/neptune/schema_prompt.py` | GRAPH_SCHEMA 缺 K8sService 节点和对应边定义 |
| BUG-S0-03 | P1 | ✅ 已修复 | `infra/cdk.context.json` | VPC ID 已是真实值，无需修复 |
| BUG-NEW-01 | P1 | ✅ 已修复 | `collectors/eks.py` + `handler.py` | collect_k8s_services 只查 default namespace，导致 K8sService 节点写不进 Neptune |
| BUG-NEW-02 | P1 | ✅ 已修复 | `rca/neptune/schema_prompt.py` | Involves/MentionsResource 边在 Neptune 存在但 schema 未定义 |

---

## 修复计划

### Task 1: BUG-S0-01 — FEW_SHOT 语法修复（Level 1）

**文件**: `rca/neptune/schema_prompt.py` L135
**问题**: FEW_SHOT_EXAMPLES[1] 的 cypher 中关系是匿名的（`-[:Calls|AccessesData|...]->`)，但 RETURN 子句引用了 `type(r)`，`r` 未绑定。
**修复**: 将匿名关系改为具名关系 `-[r:Calls|AccessesData|PublishesTo|InvokesVia|DependsOn]->`
**改动量**: 1 行
**验收**: `FEW_SHOT_EXAMPLES[1]['cypher']` 包含 `-[r:` 且包含 `type(r)`

### Task 2: BUG-S0-02 — K8sService schema 补录（Level 1）

**文件**: `rca/neptune/schema_prompt.py` GRAPH_SCHEMA
**问题**: ETL Step 8e 写入 K8sService 节点和 Implements/BelongsTo 边，但 GRAPH_SCHEMA 未定义该节点类型
**修复**:
1. 在"计算层"节点部分（Microservice 之后）添加：
   ```
   - K8sService: name(str), namespace(str), svc_type(str), cluster_ip(str), app_label(str)
   ```
2. 在"运行关系"边部分添加：
   ```
   - (:K8sService)-[:Implements]->(:Microservice|:LambdaFunction)
   - (:Pod)-[:BelongsTo]->(:K8sService)
   ```
3. 更新节点计数 27 → 28

**改动量**: ~5 行
**验收**: `GRAPH_SCHEMA` 包含 `K8sService`，节点类型数为 28

---

## 执行顺序

1. Task 1（BUG-S0-01）— 1 行改动
2. Task 2（BUG-S0-02）— 5 行改动
3. 运行 `test_11_schema_consistency.py` 验证 S0-03/S0-04 转绿
4. 运行 `test_23_regression_bugfix.py` 验证 S9-05 转绿
5. 运行全量测试确认无回归
6. git commit + push

**预计总改动**: < 10 行，Level 1 直接修复

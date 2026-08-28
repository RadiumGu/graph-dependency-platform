# 文档 vs 代码 偏差审计报告

> 生成时间:2026-08-27 16:47 UTC
> 对象:`/home/ec2-user/works/graph-dependency-platform`
> 基准文档:`README_CN.md`「关键指标」表 + 架构图 + 各模块 README
> 方法:逐项对照真实代码 / schema / YAML,**只读核对,未改动任何文件**

---

## 1. 逐条核对表

| # | 声明项 | 文档声明值 | 代码实际值 | 是否属实 | 证据文件 |
|---|--------|-----------|-----------|---------|---------|
| 1a | Neptune 节点类型 | **23 种**(关键指标 + 架构图) | 权威 schema 写 **31 种**;ETL 代码引用 ≥28 种 label | ❌ 不属实(文档内部自相矛盾) | `profiles/petsite.yaml`(graph_schema_text「节点类型(31 种)」);`infra/lambda/etl_aws/`;`README_CN.md` L98/L378 |
| 1b | Neptune 边类型 | **19 种** | 权威 schema 写 **28 种** | ❌ 不属实(多处口径不一) | `profiles/petsite.yaml`「边类型(28 种)」;`README_CN.md` L379 |
| 2 | 查询库 Q1–Q18(18 个) | 单一查询库 18 个 | `rca` 只有 **13 个**(Q1–Q11、Q17、Q18);Q12–Q16 在 **dr-plan-generator** 模块 | ⚠️ 半属实/误导 | `rca/neptune/neptune_queries.py`(13 函数);`dr-plan-generator/graph/queries.py`(Q12–Q16) |
| 3 | Chaos Mesh 已验证工具 | **30 个** / 另处 **24 种** | `fault_catalog.yaml` chaosmesh 段 **19 种** | ❌ 不属实(30、24 均无依据且互相矛盾) | `chaos/code/runner/fault_catalog.yaml`;`README_CN.md` L318/L349 |
| 4 | AWS FIS 故障类型 | **15 种** | `FIS_ACTION_MAP` 动态生成 **37 条**(distinct action_id 34) | ❌ 不属实(严重低报,差 22 项) | `chaos/code/runner/fault_registry.py`(`_build_fis_action_map`);`fault_catalog.yaml` fis 段;`fis_backend.py` L26 |
| 5 | Layer2 AWS 服务探针 | **6 个** | `@register_probe` **6 个**(SQS/DynamoDB/Lambda/ALB/StepFunctions/EC2ASG) | ✅ 属实 | `rca/collectors/aws_probers.py` |
| 6 | 跨模块集成测试 | **47+** | `tests/*.py` 共 **277 个** `def test_`,其中集成/E2E 约 53 个 | ✅ 属实(但严重低报) | `tests/`(test_06/07/08/09/20/21/10 等) |
| 7 | DR 计划阶段 | **7 个**(Phase -1 到 4 + 2.5) | 生成器只产出 **5 个**:`phase-0`…`phase-4`;无 phase--1、无 phase-2.5(2.5 仅为 `gate_condition` 门禁) | ❌ 不属实 | `dr-plan-generator/planner/plan_generator.py` L288/317/346/375/442 |
| 8 | Neptune 节点 171+ | **171+** | 运行期数据,源码不产生固定值;`demo/app.py` 写死为展示文案 | ⚠️ 无法佐证(数据依赖) | `chaos/docs/prd.md` L92、`chaos/docs/tdd.md` L1888;`demo/app.py` L68/L129 |

---

## 2. 文档内部矛盾(同一指标多处口径打架)

| 指标 | README 关键指标 | profiles schema(权威) | dr README | demo/app.py | 架构图 |
|------|----------------|----------------------|-----------|-------------|--------|
| 节点类型数 | 23 | **31** | — | 22 | 23 |
| 边类型数 | 19 | **28** | 17 | 19 | 19 |
| 故障总数 | — | 60(19+37+4) | — | — | 「61 种」 |

- 节点类型出现 **23 / 31 / 22** 三种说法;边类型出现 **19 / 28 / 17** 三种说法。
- 架构图「61 种故障 × 双后端」与目录实际 **60 种**(chaosmesh 19 + fis 37 + fis_scenarios 4)差 1。

---

## 3. 偏差严重程度小结:**中—高**

### 🔴 高严重(数字与代码明显背离,易误导评审 / 客户)
- **FIS 故障类型**:声明 15 vs 实际 37(#4)——低报超一半,反而低估了产品能力。
- **Chaos Mesh 工具数**:声明 30 / 24 vs 实际 19(#3)——两个文档值互相矛盾且都无代码依据。
- **节点/边类型数**:声明 23/19 vs 权威 schema 31/28(#1a/1b),且全仓存在 23/31/22 与 19/28/17 多套口径。

### 🟡 中严重
- **DR 阶段**:7 vs 5(#7)——Phase -1、2.5 在生成器中不存在,仅门禁条件。
- **查询库呈现**:「单库 Q1–Q18」误导(#2)——rca 实际 13 个,Q12–Q16 属 dr 模块。

### 🟢 低严重(方向正确,仅数值陈旧/保守)
- **集成测试**:47+ vs 实际 277(#6)——低报,但「47+」下限成立。
- **171+ 节点**(#8)——运行期数据,源码不可验证,非虚假但不可佐证。

### ✅ 完全属实
- Layer2 探针 6 个(#5)。

---

## 4. 修复建议

1. **以代码/配置为单一事实源统一回填**:图 schema 类型数以 `profiles/petsite.yaml` 为准(节点 31 / 边 28),故障数以 `chaos/code/runner/fault_catalog.yaml` 为准(chaosmesh 19 + fis 37 + scenarios 4 = 60)。
2. **查询库表述改为分模块**:明确 rca 提供 Q1–Q11/Q17/Q18(13 个),dr-plan-generator 提供 Q12–Q16,避免"单一 18 查询库"的误导。
3. **DR 阶段口径统一为 5 个 Phase(0–4)**,Phase 2.5 明确标注为"Phase 3 前的硬阻断就绪关卡(gate_condition)"而非独立阶段。
4. **考虑加一个 CI 校验**:用脚本从 `fault_catalog.yaml` / `profiles` 生成 README 指标表,防止再次漂移。

> 核心结论:模块级**功能**声明基本可信,但「关键指标」表中的**计数类数字**大多滞后或与代码不一致,尤以 chaos 双后端故障数(#3/#4)和图 schema 类型数(#1)最严重。

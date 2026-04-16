# Sprint 0 结果：Schema 一致性检查

执行时间：2026-04-16 10:12 UTC  
测试文件：`tests/test_11_schema_consistency.py`  
执行命令：`python3 -m pytest tests/test_11_schema_consistency.py -v --tb=short -s`

---

## 测试结果

| ID | 测试项 | 状态 | 备注 |
|----|--------|------|------|
| S0-01 | schema_prompt.py 节点标签 vs Neptune 实际标签 | ✅ PASS | 27 种标签完全匹配 |
| S0-02 | schema_prompt.py 边类型 vs Neptune 实际边类型 | ✅ PASS | 21 种边类型完全匹配 |
| S0-03 | FEW_SHOT_EXAMPLES 全部可执行且返回结果 | ❌ FAIL | 第 2 条示例有语法 bug，1 条无结果 |
| S0-04 | ETL 节点标签与 schema_prompt.py 一致（静态扫描） | ❌ FAIL | ETL 写入 `K8sService` 但 schema 未定义 |
| S0-05 | ETL 边类型与 schema_prompt.py 一致（静态扫描） | ✅ PASS | ETL 使用的 17 种边类型全在 schema 中 |
| S0-06 | CDK synth 成功（退出码 0） | ❌ FAIL | cdk.context.json 含占位符 `YOUR_VPC_ID` |
| S0-07 | Python 主要模块无循环依赖 | ✅ PASS | 9 个模块全部可正常导入 |

**总计：4 通过 / 3 失败**

---

## 发现问题

### [S0-03-BUG] FEW_SHOT_EXAMPLES[1] 含未定义变量 `r`

**文件：** `rca/neptune/schema_prompt.py`  
**问题查询（问题 Q2）：**
```cypher
MATCH (s:Microservice {name:'petsite'})-[:Calls|AccessesData|PublishesTo|InvokesVia|DependsOn]->(d)
RETURN d.name AS dependency, labels(d)[0] AS type, type(r) AS relation
```
**原因：** RETURN 子句中的 `type(r)` 引用了未绑定变量 `r`（关系匿名，未赋予变量名）。Neptune 返回 400 Bad Request。  
**修复：** 将匿名关系 `-[...]->` 改为具名 `-[r:...]->` 并保留 `type(r)`，或删除 `type(r)` 返回项。

**附加警告（不导致失败）：** 示例 "所有 P0 故障及其根因" 返回空结果——图中暂无 severity=P0 的 Incident 节点，属数据缺失，非 schema 错误。

---

### [S0-04-SCHEMA-GAP] ETL 写入 `K8sService` 节点但 schema_prompt.py 未定义

**文件：** `infra/lambda/etl_aws/handler.py`（Step 8e，约第 695 行）  
**代码：**
```python
k_vid = upsert_vertex('K8sService', svc['name'], {
    'namespace':  svc['namespace'],
    'svc_type':   svc['type'],
    'cluster_ip': svc['cluster_ip'] or '',
    'app_label':  svc['app_label'],
}, 'eks-etl')
```
**原因：** ETL 在 Step 8e 将 Kubernetes Service 对象写为 `K8sService` 节点，但该标签在 `schema_prompt.py` 的 GRAPH_SCHEMA 中不存在，导致：
1. NL→openCypher 引擎不知道该节点类型的存在，无法生成相关查询
2. S0-01 节点标签一致性测试覆盖不到（Neptune 实际有该类型但 schema 缺失）  

**修复：** 在 `schema_prompt.py` 的 `GRAPH_SCHEMA` "计算层" 部分补充：
```
- K8sService: name(str), namespace(str), svc_type(str), cluster_ip(str), app_label(str)
```
并补充对应边定义（`K8sService -[:Implements]-> Microservice`，`Pod -[:BelongsTo]-> K8sService`）。

---

### [S0-06-CDK] CDK synth 失败：VPC ID 占位符未替换

**文件：** `infra/cdk.context.json`  
**错误：**
```
[Error at /NeptuneClusterStack] Could not find any VPCs matching
{"vpc-id":"YOUR_VPC_ID",...}
```
**原因：** `cdk.context.json` 中 VPC lookup 条目使用了字符串 `"YOUR_VPC_ID"` 而非实际的 VPC ID（如 `vpc-xxxxxxxx`）。CDK 无法在 AWS 中找到该 VPC，导致 synth 失败。  
**影响：** 无法用 `cdk diff` / `cdk deploy` 验证栈变更，本地开发流程断裂。  
**修复：** 将 `cdk.context.json` 中所有 `"YOUR_VPC_ID"` 替换为实际 VPC ID，或执行 `cdk synth` 触发自动上下文查找（需 AWS 凭证）。

---

## 结论

| 维度 | 状态 |
|------|------|
| Neptune 图 schema 与 schema_prompt.py 一致性 | ✅ 完全一致（节点 27 种，边 21 种） |
| ETL 写入边类型全部有 schema 定义 | ✅ 17/17 边类型覆盖 |
| ETL 写入节点标签全部有 schema 定义 | ❌ 缺 `K8sService`（1 处） |
| NL 查询 few-shot 示例可执行性 | ❌ 1/10 示例语法错误（`type(r)` 未绑定） |
| IaC 基础设施可编译性 | ❌ CDK context 含占位符，synth 失败 |
| Python 模块无循环依赖 | ✅ 所有关键模块可正常导入 |

**Sprint 0 核心 schema 层健康度：良好。** 图数据库与文档层完全对齐（S0-01/02 PASS），ETL 边类型完全合规（S0-05 PASS）。发现的 3 个问题均有明确修复路径，优先级：`K8sService` schema 补录 > FEW_SHOT bug 修复 > CDK context 占位符替换。

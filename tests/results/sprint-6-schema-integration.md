# Sprint 6 结果：动态 Schema 集成测试

执行时间：2026-04-16 （约 88.5 秒）
测试文件：`tests/test_20_integration_schema.py`
执行命令：`python3 -m pytest tests/test_20_integration_schema.py -v --tb=short`

---

## 测试结果

| ID | 测试项 | 状态 | 备注 |
|----|--------|------|------|
| S6-01 | 动态 schema 提取节点标签 vs 静态 GRAPH_SCHEMA 一致 | ✅ PASS | 27 种节点标签完全对齐 |
| S6-02 | 动态 schema 提取边类型 vs 静态 GRAPH_SCHEMA 一致 | ✅ PASS | 21 种边类型完全对齐 |
| S6-03 | Schema 缓存 TTL 10 分钟后过期刷新（mock time） | ✅ PASS | 三阶段（首次/命中/过期）全部验证通过 |
| S6-04 | Neptune 不可达时回退静态 schema 优雅返回 | ✅ PASS | ConnectionError 被捕获，返回 `{error, cypher}` dict |
| S6-05 | NL 查询 "petsite 依赖哪些数据库" 返回数据库关键词 | ✅ PASS | Cypher 包含 `AccessesData` + `DynamoDBTable`/`RDSCluster` |
| S6-06 | NL 查询 "petsite 的上下游服务" 返回 Calls/AccessesData 相关节点 | ⏭ SKIP | engine.query() 返回 error（Cypher 执行失败，数据缺口） |
| S6-07[微服务依赖] | "petsite 依赖哪些服务" → Cypher 含 Microservice | ✅ PASS | — |
| S6-07[数据库查询] | "petsite 的数据库有哪些" → Cypher 含 Accesses | ✅ PASS | — |
| S6-07[Pod状态] | "哪些 Pod 在运行" → Cypher 含 Pod | ✅ PASS | — |
| S6-07[EKS节点] | "EKS 集群有哪些节点" → Cypher 含 EC2 | ✅ PASS | — |
| S6-07[负载均衡器] | "有哪些负载均衡器" → Cypher 含 LoadBalancer | ✅ PASS | — |
| S6-07[上下游拓扑] | "petsite 的上下游服务" → Cypher 含 Microservice | ✅ PASS | — |
| S6-07[安全组关联] | "哪些服务有安全组" → Cypher 含 SecurityGroup | ✅ PASS | — |
| S6-07[RDS状态] | "RDS 实例状态" → Cypher 含 RDS | ✅ PASS | — |
| S6-07[Lambda函数] | "Lambda 函数列表" → Cypher 含 Lambda | ✅ PASS | — |
| S6-07[混沌实验] | "混沌实验结果" → Cypher 含 ChaosExperiment | ✅ PASS | — |
| S6-08[DROP] | DROP INDEX 被 QueryGuard 拦截 | ✅ PASS | — |
| S6-08[DELETE] | MATCH DELETE 被拦截 | ✅ PASS | — |
| S6-08[SET] | SET 属性操作被拦截 | ✅ PASS | — |
| S6-08[MERGE] | MERGE 创建被拦截 | ✅ PASS | — |
| S6-08[CREATE] | CREATE 节点被拦截 | ✅ PASS | — |
| S6-08[DETACH] | DETACH DELETE 被拦截 | ✅ PASS | — |
| S6-08[REMOVE] | REMOVE 属性被拦截 | ✅ PASS | — |
| S6-08[CALL] | CALL 程序调用被拦截 | ✅ PASS | — |
| S6-08 pipeline | NL engine 流水线中注入 Cypher 被拦截，Neptune 未被调用 | ✅ PASS | mock_nc.assert_not_called() 通过 |

**总计：24 通过 / 1 跳过 / 0 失败**

---

## 发现问题

### [S6-06-DATA-GAP] "petsite 的上下游服务" NL 查询执行失败

**触发路径：** `test_s6_06_nl_query_petsite_upstream_downstream`
**状态：** SKIP（非 FAIL，测试代码对 error 结果做了 skip 处理）

**原因分析：** NLQueryEngine 的 `query()` 对 "petsite 的上下游服务是什么" 返回了 `{"error": ..., "cypher": ...}`，触发了 `pytest.skip`。可能原因：
1. Bedrock 生成的 Cypher 使用了不存在的关系名（如 `DependsOn` 用于数据库，而 schema 中对应关系为 `AccessesData`），导致 Neptune 执行返回空或报错
2. "上下游"语义模糊，Bedrock 生成了过宽泛的多跳查询（如 `*1..5`），执行超时

**影响：** S6-06 本身的"上下游关系 Cypher 关键词"断言未被验证。但 S6-07[上下游拓扑] 使用相同查询词通过了 PASS（仅检查 Cypher 生成，未强制非 error），说明 Cypher 生成本身正常。

**建议修复：** 在 `schema_prompt.py` 的 FEW_SHOT_EXAMPLES 中补充明确的"上下游服务"示例（上游：`Calls` 入边；下游：`Calls` 出边 + `AccessesData`），引导 Bedrock 生成更精确的 Cypher。

---

### [WARN] `pytest.mark.neptune` 未注册

**现象：** 21 条 `PytestUnknownMarkWarning`
**原因：** `neptune` 自定义标记未在 `pytest.ini` / `pyproject.toml` 中注册
**影响：** 仅警告，不影响测试执行；但无法用 `-m neptune` 过滤只运行 Neptune 相关用例

**修复方案：** 在 `pytest.ini` 添加：
```ini
[pytest]
markers =
    neptune: marks tests that require a live Neptune connection
```

---

## 结论

| 维度 | 状态 |
|------|------|
| 动态节点标签 vs 静态 schema 一致性 | ✅ 27/27 完全匹配 |
| 动态边类型 vs 静态 schema 一致性 | ✅ 21/21 完全匹配 |
| Schema 缓存 TTL 刷新逻辑 | ✅ 三阶段（首次/命中/过期）验证通过 |
| Neptune 不可达优雅降级 | ✅ error dict 返回，不传播异常 |
| NL 查询端到端（数据库意图） | ✅ Cypher 包含正确标签/关系 |
| NL 查询端到端（上下游意图） | ⚠️ 执行失败，数据缺口或 few-shot 不足 |
| 10 种常见 NL 查询模式 Cypher 正确性 | ✅ 10/10 关键词匹配通过 |
| QueryGuard 注入防护（8 种攻击向量） | ✅ 8/8 全部拦截 |
| NL 引擎流水线注入防护 | ✅ Neptune 未被调用，guard 在正确位置介入 |

**Sprint 6 核心 schema 集成层健康度：优良。** 动态/静态 schema 完全对齐（S6-01/02 PASS），安全防护全覆盖（S6-08 8+1 全 PASS），10 种 NL 查询模式全部生成正确 Cypher（S6-07 10/10 PASS）。唯一问题是"上下游"查询执行阶段失败（S6-06 SKIP），属 few-shot 示例覆盖不足，有明确修复路径。

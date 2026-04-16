# Sprint 9 回归 / Bug 修复验证报告

**日期**: 2026-04-16  
**测试文件**: `tests/test_23_regression_bugfix.py`  
**执行命令**: `python3 -m pytest tests/test_23_regression_bugfix.py -v --tb=short`  
**总结**: 4 PASSED / 1 FAILED (预期)

---

## 测试结果总览

| 用例 | 优先级 | 关联 Bug | 状态 | 说明 |
|------|--------|----------|------|------|
| S9-01 | P0 | BUG-01 | ✅ PASSED | metadata content 按 bytes 截断，长中文不超 1500 bytes |
| S9-02 | P0 | BUG-01 | ✅ PASSED | 短文本截断逻辑不破坏内容完整性 |
| S9-03 | P0 | BUG-02 | ✅ PASSED | Bedrock 超时返回 `{"error": ...}`，不抛出异常 |
| S9-04 | P0 | BUG-02 | ✅ PASSED | 正常查询路径完整返回四字段结构 |
| S9-05 | P0 | Schema 漂移 | ❌ FAILED (预期) | K8sService 节点漂移待修复 |

---

## S9-01: BUG-01 修复验证 — ✅ PASSED

**验证内容**: `incident_vectordb.index_incident()` 写入 S3 Vectors 时，  
metadata `content` 字段按 UTF-8 bytes 截断，长中文不超 1500 bytes。

**测试方法**: Mock `chunk_text` 返回约 4800 bytes 的中文 chunk，  
mock S3 Vectors client，捕获 `put_vectors` 调用，断言 content 字段 ≤ 1500 bytes。

**Fix 位置**: `rca/search/incident_vectordb.py` ~L86  
```python
'content': chunk.content.encode('utf-8')[:1500].decode('utf-8', errors='ignore'),
```

---

## S9-02: BUG-01 回归防护 — ✅ PASSED

**验证内容**: 短文本（< 200 bytes）经 bytes 截断逻辑后内容无损，  
`DynamoDB`、`petsite` 等关键词仍保留在 metadata content 中。

**测试方法**: 同 S9-01，使用短文本，验证 content 非空且关键词完整。

---

## S9-03: BUG-02 修复验证 — ✅ PASSED

**验证内容**: `NLQueryEngine.query()` 捕获 `_generate_cypher()` 的异常，  
Bedrock 超时时返回 `{"error": "ReadTimeoutError: ...", "cypher": ""}` 而非抛出异常。

**测试方法**: mock `bedrock.invoke_model` 抛出 `Exception("ReadTimeoutError: ...")`，  
调用 `engine.query()`，断言返回值是包含 `"error"` key 的 dict。

**Fix 位置**: `rca/neptune/nl_query.py` `query()` 方法
```python
try:
    cypher = self._generate_cypher(question)
except Exception as e:
    logger.warning(f"NLQuery cypher generation failed: {e}")
    return {"error": str(e), "cypher": ""}
```

**关联旧测试**: `test_04_unit_nlquery.py::test_ub2_03_bedrock_timeout_raises`  
该测试验证旧行为（异常向上传播），在 BUG-02 修复后应标记为 XFAIL 或删除。

---

## S9-04: BUG-02 回归防护 — ✅ PASSED

**验证内容**: 正常 Bedrock 响应时，`query()` 的 try/except 不影响正常路径，  
结果包含完整的 `question / cypher / results / summary` 四字段。

**测试方法**: mock Bedrock 返回有效 cypher，mock `neptune_client.results` 返回 mock 数据，  
断言结果不含 `"error"`，且四字段均存在。

---

## S9-05: Schema 漂移回归 — ❌ FAILED (预期)

**失败原因（预期，已知漂移）**:

```
【节点标签漂移】ETL 使用但 schema_prompt.py 未定义：
  ❌ K8sService
```

**漂移详情**:
- `infra/lambda/etl_aws/handler.py` L695 新增了 `K8sService` 节点写入：
  ```python
  k_vid = upsert_vertex('K8sService', svc['name'], {...})
  ```
- `rca/neptune/schema_prompt.py` GRAPH_SCHEMA 中无 `K8sService` 节点定义

**静态扫描覆盖**:
- 节点标签: ETL 使用 27 个标签，schema 定义 27 个标签，K8sService 为净差 1 个
- 边类型: ETL 使用 19 种边类型，schema 均覆盖（含 `[:ForwardsTo|:RoutesTo]` 多类型写法）

**修复方法**: 在 `rca/neptune/schema_prompt.py` GRAPH_SCHEMA 中添加：
```
### 容器网络层
- K8sService: name(str), cluster_ip(str), port(int), selector(str)
```
并在边类型部分添加：
```
- (:K8sService)-[:Implements]->(:Microservice)
- (:Pod)-[:BelongsTo]->(:K8sService)
```

**预计修复 Sprint**: Sprint 10（K8s 服务发现功能完善时一并更新）

---

## 注意事项

1. S9-03 验证的是 **修复后** 的行为（返回 error dict）。  
   与之对应的 `test_04_unit_nlquery.py::test_ub2_03_bedrock_timeout_raises` 验证旧行为（期望抛出异常）——  
   该旧测试在当前代码状态下会 FAIL，建议 Sprint 10 中标记为 `@pytest.mark.skip` 并添加说明。

2. S9-05 使用纯静态 AST 正则扫描，仅提取字符串字面量，不执行代码。  
   边类型扫描已正确处理 `[:Type1|:Type2]` 多类型写法（v2 修复了初版 regex 漏洞）。

---

*记录人: 测试自动化系统 | 2026-04-16*

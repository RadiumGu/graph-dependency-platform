# FAILURES.md — 已知问题记录

**日期**: 2026-04-01
**版本**: test suite v1.0

---

## BUG-01: incident_vectordb.py — S3 Vectors metadata 超出 2048 bytes 限制

**文件**: `rca/search/incident_vectordb.py`
**行号**: ~L82
**发现于**: `test_05_unit_vectors.py::test_ub5_03`（改为测试 chunker 逻辑，绕过此限制）

### 问题描述

`index_incident()` 将 `chunk.content[:2000]` 存入 S3 Vectors metadata，但 S3 Vectors 的 filterable metadata 总大小上限为 **2048 bytes**（所有字段合计）。当 report_text 为中文时，每个汉字占 3 bytes（UTF-8 编码），2000 个汉字 ≈ 6000 bytes，远超上限。

### 错误信息

```
botocore.errorfactory.ValidationException: An error occurred (ValidationException) when calling the
PutVectors operation: Invalid record for key 'xxx.chunk-0000': Filterable metadata must have at most
2048 bytes
```

### 影响范围

- 单条 incident 使用短文本（< ~500 bytes content + 其他字段）时正常（test_ub5_02 通过）
- 长中文 RCA 报告写入 S3 Vectors 时会失败

### 建议修复

在 `incident_vectordb.py` 的 metadata 构建中缩短 content 字段限制，并为 JSON 序列化后的 bytes 大小做总量控制：

```python
# 修复建议（不修改测试，修改 src）
MAX_METADATA_BYTES = 1500  # 留余量给其他字段

content_bytes = chunk.content.encode('utf-8')
if len(content_bytes) > MAX_METADATA_BYTES:
    # 按 bytes 截断，再解码（避免截断多字节字符）
    content_bytes = content_bytes[:MAX_METADATA_BYTES]
    content_truncated = content_bytes.decode('utf-8', errors='ignore')
else:
    content_truncated = chunk.content

metadata = {
    ...
    'content': content_truncated,
    ...
}
```

---

## BUG-02: nl_query.py — Bedrock 超时异常未被捕获

**文件**: `rca/neptune/nl_query.py`
**行号**: `query()` 方法
**发现于**: `test_04_unit_nlquery.py::test_ub2_03_bedrock_timeout_raises`

### 问题描述

`NLQueryEngine.query()` 没有捕获来自 `_generate_cypher()` 的异常。当 Bedrock 超时或不可用时，异常会向上传播到调用者，而非返回 `{"error": "..."}` 的友好格式。

根据 PRD 规范（U-B2-03），调用者期望收到 `{"error": ...}` dict 而非原始异常。

### 当前行为

```python
def query(self, question: str) -> dict:
    cypher = self._generate_cypher(question)  # 异常直接传播
    ...
```

### 建议修复

```python
def query(self, question: str) -> dict:
    try:
        cypher = self._generate_cypher(question)
    except Exception as e:
        logger.warning(f"Bedrock cypher generation failed: {e}")
        return {"error": str(e), "cypher": ""}
    ...
```

---

*记录人: 测试自动化系统 | 2026-04-01*

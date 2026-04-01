# CLAUDE-TEST.md — Graph Dependency Platform 测试执行指引

> **给 Claude Code 的测试任务指引。执行前必读。**

## 你的任务

执行 `/home/ubuntu/tech/blog/gp-improve/测试计划-Phase-AB.md` 中定义的测试用例。
测试代码写在 `/home/ubuntu/tech/graph-dependency-platform/tests/` 目录下。

---

## ⚠️ 关键约束

1. **环境变量必须设置**（已在 shell 中 export，但 pytest 需要确认）：
   ```
   NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com
   REGION=ap-northeast-1
   ```

2. **不要修改任何 src 代码**，只写测试代码和 conftest.py

3. **测试文件结构**：
   ```
   tests/
   ├── conftest.py                    # 共享 fixtures（Neptune client、环境变量、测试数据清理）
   ├── test_00_regression.py          # P0: 回归测试（现有功能不破坏）
   ├── test_01_security.py            # P0: NL 查询安全测试
   ├── test_02_unit_rca.py            # P2: rca 模块单元测试
   ├── test_03_unit_chaos.py          # P2: chaos 模块单元测试
   ├── test_04_unit_nlquery.py        # P2: NL 查询引擎单元测试
   ├── test_05_unit_vectors.py        # P2: S3 Vectors 单元测试
   ├── test_06_integration_chaos_rca.py    # P1: chaos ↔ rca 联动
   ├── test_07_integration_incident.py     # P1: incident 全链路联动
   ├── test_08_integration_cross_module.py # P1: 跨模块 Neptune 一致性
   ├── test_09_integration_nlquery.py      # P1: NL 查询跨模块验证
   └── test_10_e2e.py                     # P3: 端到端场景
   ```

4. **sys.path 设置**（在 conftest.py 中统一处理）：
   ```python
   import sys, os
   PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'
   sys.path.insert(0, os.path.join(PROJECT_ROOT, 'rca'))
   sys.path.insert(0, os.path.join(PROJECT_ROOT, 'chaos', 'code'))
   sys.path.insert(0, os.path.join(PROJECT_ROOT, 'dr-plan-generator'))
   sys.path.insert(0, os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_aws'))
   ```

5. **测试数据命名约定**：所有测试创建的 Neptune 节点必须以 `test-auto-` 为前缀，便于清理

6. **每个测试文件开头的 docstring** 必须写清楚测试了哪些用例编号（如 `# Tests: U-A1-01 ~ U-A1-07`）

---

## 执行顺序

**严格按文件编号顺序执行**（前面的测试失败不影响后面的独立测试）：

```bash
cd /home/ubuntu/tech/graph-dependency-platform

# Phase 1: P0 回归 + 安全（必须全过）
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
REGION=ap-northeast-1 \
python -m pytest tests/test_00_regression.py tests/test_01_security.py -v --tb=short

# Phase 2: P2 单元测试
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
REGION=ap-northeast-1 \
python -m pytest tests/test_02_unit_rca.py tests/test_03_unit_chaos.py tests/test_04_unit_nlquery.py tests/test_05_unit_vectors.py -v --tb=short

# Phase 3: P1 联动测试（最重要）
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
REGION=ap-northeast-1 \
python -m pytest tests/test_06_integration_chaos_rca.py tests/test_07_integration_incident.py tests/test_08_integration_cross_module.py tests/test_09_integration_nlquery.py -v --tb=short

# Phase 4: P3 端到端
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
REGION=ap-northeast-1 \
python -m pytest tests/test_10_e2e.py -v --tb=long
```

---

## conftest.py 必须包含的 Fixtures

```python
import pytest
import sys
import os
import logging

logger = logging.getLogger(__name__)

# === Path setup ===
PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'rca'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'chaos', 'code'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'dr-plan-generator'))

# === Environment ===
os.environ.setdefault('NEPTUNE_ENDPOINT', 'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
os.environ.setdefault('REGION', 'ap-northeast-1')
os.environ.setdefault('NEPTUNE_PORT', '8182')

TEST_PREFIX = 'test-auto-'


@pytest.fixture(scope='session')
def neptune_rca():
    """rca 模块的 Neptune client"""
    from neptune import neptune_client as nc
    # 验证连接
    result = nc.results("MATCH (n) RETURN count(n) AS cnt LIMIT 1")
    assert isinstance(result, list), "Neptune connection failed"
    return nc


@pytest.fixture(scope='session')
def neptune_dr():
    """dr-plan-generator 模块的 Neptune client"""
    from graph import neptune_client
    return neptune_client


@pytest.fixture(scope='session')
def test_incident_id():
    return f'{TEST_PREFIX}incident-001'


@pytest.fixture(scope='session')
def test_experiment_id():
    return f'{TEST_PREFIX}chaos-001'


@pytest.fixture(scope='session', autouse=True)
def cleanup_test_data(neptune_rca):
    """测试结束后清理所有 test-auto- 前缀的测试数据"""
    yield
    # Cleanup Neptune
    try:
        neptune_rca.results(
            f"MATCH (n) WHERE n.id STARTS WITH '{TEST_PREFIX}' "
            f"OR n.experiment_id STARTS WITH '{TEST_PREFIX}' "
            f"DETACH DELETE n"
        )
        logger.info("Neptune test data cleaned")
    except Exception as e:
        logger.warning(f"Neptune cleanup failed: {e}")

    # Cleanup S3 Vectors
    try:
        import boto3
        client = boto3.client('s3vectors', region_name='ap-northeast-1')
        # Best-effort cleanup
        resp = client.list_vectors(
            vectorBucketName='gp-incident-kb',
            indexName='incidents-v1'
        )
        test_keys = [
            v['key'] for v in resp.get('vectors', [])
            if v['key'].startswith(TEST_PREFIX)
        ]
        if test_keys:
            client.delete_vectors(
                vectorBucketName='gp-incident-kb',
                indexName='incidents-v1',
                keys=test_keys
            )
            logger.info(f"S3 Vectors: cleaned {len(test_keys)} test vectors")
    except Exception as e:
        logger.warning(f"S3 Vectors cleanup failed: {e}")
```

---

## 各测试文件的实现指引

### test_00_regression.py — 回归测试（R-01 ~ R-07）

```python
"""Tests: R-01 ~ R-07 — 回归测试，确保 Phase A/B 改动不破坏现有功能"""

def test_r01_rca_existing_tests():
    """R-01: rca 现有 pytest 通过"""
    # 直接 import rca 核心模块，验证无 ImportError
    from core import rca_engine, fault_classifier
    from actions import playbook_engine

def test_r02_dr_plan_existing_tests():
    """R-02: dr-plan-generator 测试通过"""
    from graph.graph_analyzer import GraphAnalyzer
    from validation.plan_validator import PlanValidator
    analyzer = GraphAnalyzer()
    assert analyzer is not None

def test_r03_q1_to_q16(neptune_rca):
    """R-03: Q1-Q16 现有查询不受影响"""
    from neptune import neptune_queries as nq
    # 逐一调用 Q1-Q16，验证不抛异常
    result = nq.q1_service_overview()
    assert isinstance(result, list)
    # ... 补充其他 Q2-Q16

def test_r05_rca_handler_import():
    """R-05: rca handler.py 正常导入"""
    import handler  # rca/handler.py

def test_r06_chaos_main_import():
    """R-06: chaos main.py 正常导入"""
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'chaos', 'code'))
    import main  # chaos/code/main.py

def test_r07_dr_main_import():
    """R-07: dr-plan main.py 正常导入"""
    sys.path.insert(0, PROJECT_ROOT + '/dr-plan-generator')
    import main  # dr-plan-generator/main.py
```

### test_01_security.py — 安全测试（S-01 ~ S-05）

```python
"""Tests: S-01 ~ S-05 — NL 查询引擎安全校验"""
from neptune.query_guard import is_safe, ensure_limit

def test_s01_reject_delete():
    safe, reason = is_safe("MATCH (n) DELETE n")
    assert not safe

def test_s02_reject_set():
    safe, reason = is_safe("MATCH (n) SET n.pwned = true")
    assert not safe

def test_s03_reject_create():
    safe, reason = is_safe("CREATE (n:Test {name: 'hack'})")
    assert not safe

def test_s04_reject_merge():
    safe, reason = is_safe("MATCH (n) MERGE (n)-[:BAD]->(m)")
    assert not safe

def test_s05_auto_limit():
    result = ensure_limit("MATCH (n) RETURN n")
    assert "LIMIT" in result.upper()
```

### test_06_integration_chaos_rca.py — chaos ↔ rca 联动（I-01 ~ I-03）

```python
"""Tests: I-01 ~ I-03 — 混沌实验写入 → Neptune → RCA 历史查询"""

def test_i01_chaos_write_then_rca_query(neptune_rca, test_experiment_id):
    """I-01: chaos write_experiment → rca Q18 可查到"""
    # Step 1: 用 chaos 的 neptune_sync 写入
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'chaos', 'code'))
    from neptune_sync import write_experiment
    write_experiment({
        'experiment_id': test_experiment_id,
        'target_service': 'petsite',
        'fault_type': 'pod-kill',
        'result': 'passed',
        'recovery_time_sec': 45,
        'degradation_rate': 0.12,
        'timestamp': '2026-04-01T00:00:00Z',
    })

    # Step 2: 用 rca 的 Q18 查询
    from neptune import neptune_queries as nq
    results = nq.q18_chaos_history('petsite')

    # Step 3: 验证
    exp_ids = [r.get('id') for r in results]
    assert test_experiment_id in exp_ids, f"Q18 未查到 {test_experiment_id}"
```

### test_07_integration_incident.py — Incident 全链路（I-04 ~ I-07）

```python
"""Tests: I-04 ~ I-07 — Incident 写入 → 实体提取 → Neptune 边 → Q17 → 向量搜索"""
import time

def test_i04_incident_full_chain(neptune_rca, test_incident_id):
    """I-04: write_incident 全链路"""
    from actions.incident_writer import write_incident
    write_incident(
        incident_id=test_incident_id,
        severity='P1',
        affected_service='petsite',
        root_cause='DynamoDB ReadThrottling',
        resolution='增加 RCU',
        report_text='petsite 服务因 DynamoDB PetAdoptions 表 ReadThrottling 导致 5xx，影响 petsearch 下游',
    )
    # 验证 Neptune 节点存在
    result = neptune_rca.results(
        "MATCH (inc:Incident {id: $id}) RETURN inc",
        {'id': test_incident_id}
    )
    assert len(result) > 0, "Incident 节点未创建"

def test_i05_q17_finds_incident(neptune_rca, test_incident_id):
    """I-05: Q17 查到刚写入的 Incident"""
    from neptune import neptune_queries as nq
    results = nq.q17_incidents_by_resource('petsite')
    inc_ids = [r.get('id') for r in results]
    assert test_incident_id in inc_ids

def test_i06_vector_search_finds_incident(test_incident_id):
    """I-06: 向量搜索查到刚写入的 Incident"""
    from search.incident_vectordb import search_similar
    time.sleep(2)  # S3 Vectors 写入可能有短延迟
    results = search_similar('DynamoDB 限流导致服务超时', top_k=5, threshold=0.5)
    found_ids = [r.get('incident_id') for r in results]
    assert test_incident_id in found_ids, f"向量搜索未找到 {test_incident_id}"
```

---

## 完成标准

1. **所有测试文件写完后**，先执行一遍 `python -m pytest tests/ -v --tb=short`，记录结果
2. **如果有失败**，分析原因：
   - 是测试代码写错了 → 修复测试
   - 是 src 代码有 bug → 记录到 `tests/FAILURES.md`，不要修改 src
3. **最终输出**：
   - `tests/` 目录下所有测试文件
   - `tests/FAILURES.md`（如果有失败，记录失败原因和建议修复）
   - `tests/RESULTS.md`（最终测试结果摘要）

---

## 参考文件速查

| 需要了解 | 读这个文件 |
|---------|-----------|
| 测试计划完整定义 | `/home/ubuntu/tech/blog/gp-improve/测试计划-Phase-AB.md` |
| 项目改动说明 | `/home/ubuntu/tech/graph-dependency-platform/CLAUDE.md` |
| PRD | `/home/ubuntu/tech/blog/gp-improve/PRD-改进计划.md` |
| rca Neptune 查询 | `rca/neptune/neptune_queries.py` |
| rca Incident 写入 | `rca/actions/incident_writer.py` |
| chaos Neptune 同步 | `chaos/code/neptune_sync.py` |
| NL 查询引擎 | `rca/neptune/nl_query.py` |
| 查询安全校验 | `rca/neptune/query_guard.py` |
| S3 Vectors 搜索 | `rca/search/incident_vectordb.py` |
| DR 查询 | `dr-plan-generator/graph/queries.py` |
| Schema Prompt | `rca/neptune/schema_prompt.py` |

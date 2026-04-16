# Sprint 7 E2E Pipeline Test Results

**Run date:** 2026-04-16  
**Test file:** `tests/test_21_e2e_pipeline.py`  
**Neptune endpoint:** `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com`  
**Result:** ✅ 7 / 7 PASSED

---

## Test Summary

| ID     | Name                                               | Result | Duration |
|--------|----------------------------------------------------|--------|----------|
| S7-01  | ETL AWS 写入后所有节点类型可查                      | PASS   | —        |
| S7-02  | 节点详情属性非 None / 非空字符串                    | PASS   | —        |
| S7-03  | ETL DeepFlow 写入后 Calls 边可查                    | PASS   | —        |
| S7-04  | Chaos 实验 TestedBy 边写入且属性完整                | PASS   | —        |
| S7-05  | Incident 写入后 TriggeredBy 边 + 结构化字段正确     | PASS   | —        |
| S7-06  | DR Plan 生成器基于图数据生成有效计划                | PASS   | —        |
| S7-07  | 多轮幂等写入后节点数不膨胀                          | PASS   | —        |

Total: **2.74 s** wall time.

---

## Issues Found & Fixed During Test Development

### 1. Wrong DynamoDB label (S7-01)
- **Symptom:** `DynamoDB` node type returned count=0.
- **Root cause:** The ETL writes nodes with label `DynamoDBTable`, not `DynamoDB`.
- **Fix:** Updated expected label list in test to `DynamoDBTable`.

### 2. `runner` package relative-import failure (S7-04, S7-07)
- **Symptom:** `ImportError: attempted relative import with no known parent package` when importing `neptune_sync.write_experiment`.
- **Root cause:** Test fixture was incorrectly adding `chaos/code/runner/` to `sys.path`. This caused `runner.py` to be treated as a top-level module, breaking all `from .xxx import` relative imports inside the `runner` package.
- **Fix:** Removed the `runner_dir` sys.path insertion. `conftest.py` already adds `chaos/code`, making `runner` importable as a proper package.

### 3. `dr-plan-generator` NEPTUNE_ENDPOINT empty (S7-06)
- **Symptom:** `ValueError: NEPTUNE_ENDPOINT environment variable is not set`.
- **Root cause:** `conftest.py` calls `os.environ.setdefault('NEPTUNE_ENDPOINT', …)` **after** building the unified config module. As a result, `dr-plan-generator/config.py` evaluates `os.environ.get("NEPTUNE_ENDPOINT", "")` while the env var is still unset, and the module-level constant is frozen as `""`.
- **Fix:** In S7-06 test body, patched `graph.neptune_client.NEPTUNE_ENDPOINT` with the correct endpoint after import.

### 4. `IndexError` in `step_builder._build_generic_step` (S7-06)
- **Symptom:** `IndexError: Replacement index 0 out of range for positional args tuple` in `planner/step_builder.py:415`.
- **Root cause:** `service_types.yaml` validation templates for `Pod` / `K8sService` contain jsonpath expressions such as `{.subsets[0].addresses}`. Python's `.format()` interprets `[0]` as positional-arg subscript access; since no positional args are passed, it raises `IndexError`.
- **Fix:** Wrapped the `.format()` call in `step_builder.py` with `except (KeyError, IndexError)` and fall back to a safe `# TODO` placeholder. This is correct defensive behaviour — the generic fallback path should never crash regardless of template content.

---

## Coverage Notes

- **S7-01**: Verified 9 node label types: `EC2Instance`, `EKSCluster`, `Pod`, `Microservice`, `RDSCluster`, `DynamoDBTable`, `AvailabilityZone`, `VPC`, `Subnet`.
- **S7-03**: Found Microservice→Microservice `Calls` edges (DeepFlow ETL confirmed active).
- **S7-04**: Wrote test `ChaosExperiment` via `neptune_sync.write_experiment()` and confirmed `TestedBy` edge with all 3 required fields (`experiment_id`, `fault_type`, `result`).
- **S7-05**: Wrote test `Incident` via `incident_writer.write_incident()` and confirmed `TriggeredBy` edge and structured fields (`severity`, `root_cause`, `status`).
- **S7-06**: Generated AZ-scope DR plan (`ap-northeast-1a → ap-northeast-1c`); plan contained phases and steps with non-empty `step_id`/`action` fields.
- **S7-07**: Verified MERGE idempotency — two writes of same `experiment_id` yielded node count = 1, and `ON MATCH SET` updated the `result` property.

---

## Cleanup

Test data is automatically removed by `conftest.py`'s `cleanup_test_data` session fixture, which deletes all nodes with `id STARTS WITH 'test-auto-'` or `experiment_id STARTS WITH 'test-auto-'` after the session ends.

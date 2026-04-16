# Sprint 2 ETL DeepFlow + CFN 测试结果

**执行时间**: 2026-04-16  
**耗时**: 0.43s  
**结果**: 11 passed / 0 failed

---

## 测试文件

| 文件 | 用例数 | 状态 |
|------|--------|------|
| `tests/test_13_unit_etl_deepflow.py` | 4 | PASS |
| `tests/test_14_unit_etl_cfn.py` | 7 | PASS |

---

## 用例明细

### test_13 — DeepFlow ETL

| ID | 测试名 | 验证内容 | 结果 |
|----|--------|----------|------|
| S2-01 | `test_s2_01_calls_edge_from_flow_data` | `batch_upsert_edges` 生成正确的 Gremlin Calls 边（src/dst/protocol/error_rate/coalesce 模式） | PASS |
| S2-02 | `test_s2_02_accessesdata_edge_from_dns_inference` | `run_drift_detection` 在 DNS 观测到 DynamoDB 访问但无声明边时，创建 `addE('AccessesData')` 且标注 `observed_not_declared` | PASS |
| S2-03 | `test_s2_03_empty_clickhouse_data_returns_early` | ClickHouse 返回空行时 `run_etl` 提前返回 `{nodes:0, edges:0}`，不调用 `batch_upsert_*` 和 `run_drift_detection` | PASS |
| S2-03 | `test_s2_03_malformed_rows_are_skipped` | 列数不足的行被静默跳过，合法行正常处理，不崩溃 | PASS |

### test_14 — CFN ETL + ETL Trigger

| ID | 测试名 | 验证内容 | 结果 |
|----|--------|----------|------|
| S2-04 | `test_s2_04_cfn_template_lambda_ddb_dependency` | Lambda 环境变量 `Ref` 提取 DynamoDB + SQS `AccessesData` 依赖，evidence 含 `env:` 前缀 | PASS |
| S2-04 | `test_s2_04_cfn_stepfunction_invokes_lambda` | StepFunction `DefinitionString` 中的 Lambda `Fn::GetAtt` 提取 `Invokes` 边 | PASS |
| S2-05 | `test_s2_05_nested_stack_resource_skipped` | `AWS::CloudFormation::Stack` 不在 `SEMANTIC_TYPES` 中，不生成依赖边；同模板中其他资源正常处理 | PASS |
| S2-05 | `test_s2_05_handler_cfn_event_routing` | `handler` 从 EventBridge `stack-id` ARN 提取 stack name，路由到配置列表中的正确 stack | PASS |
| S2-06 | `test_s2_06_trigger_sqs_event_invokes_etl` | SQS 消息触发 `neptune-etl-from-aws`，`InvocationType=Event`（异步），sleep 30s，payload 含 event_sources | PASS |
| S2-06 | `test_s2_06_trigger_multiple_records_batched` | 多条 SQS 记录合并为一次 Lambda 调用，payload 含全部 event_sources | PASS |
| S2-06 | `test_s2_06_trigger_empty_records_skips_etl` | 空 Records 不触发 ETL 调用，不 sleep | PASS |

---

## Mock 策略

- **neptune_client_base**: `types.ModuleType` stub（Lambda Layer 在测试环境不存在）
- **DeepFlow / ClickHouse**: `patch.object(etl_df, 'ch_query')` 返回 TSV 行列表
- **boto3 CloudFormation**: `extract_declared_deps` 是纯函数，不需要 mock；`handler` 通过 `patch.object(etl_cfn, 'run_etl')` 隔离
- **boto3 Lambda (Trigger)**: `patch.object(etl_trigger, 'lambda_client')` + `patch('time.sleep')` 跳过 30s 等待
- **Neptune 写操作**: `patch.object(module, 'neptune_query')` 捕获 Gremlin 字符串做断言

---

## 关键验证点

1. **Calls 边格式**: Gremlin 包含 `coalesce(__.inE('Calls')...__.addE('Calls'))` + `error_rate` 计算正确
2. **AccessesData 推断**: DNS 关键字命中 → `addE('AccessesData')` + `source=deepflow-dns` + `drift_status=observed_not_declared`
3. **空数据早返**: `run_etl` 在 rows=[] 时不调用任何写操作（保护 Neptune 免受无效调用）
4. **CFN 语义过滤**: `AWS::CloudFormation::Stack` 被正确排除，不引入拓扑噪音
5. **Trigger 异步模式**: `InvocationType='Event'` 确保触发器不阻塞等待 ETL 完成

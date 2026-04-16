# Sprint 4 Chaos Runner — 单元测试结果

**执行时间**: 2026-04-16  
**测试文件**: `tests/test_17_unit_chaos_runner.py`  
**结果**: ✅ 31 passed, 0 failed (1.78s)

---

## 测试用例汇总

| ID | 模块 | 测试描述 | 结果 |
|----|------|----------|------|
| S4-01a | fault_injector | InjectionResult 字段完整性 (experiment_ref/backend/start_time/expected_duration) | ✅ PASS |
| S4-01b | fault_injector | FaultInjector 是抽象基类，含 inject/remove/status/abort/preflight_check | ✅ PASS |
| S4-01c | fault_injector | ChaosMeshBackend.inject 调用 ChaosMCPClient 返回 InjectionResult | ✅ PASS |
| S4-02a | fis_backend | FISClient.inject 调用 create_experiment_template + start_experiment | ✅ PASS |
| S4-02b | fis_backend | FISClient.stop 调用 fis.stop_experiment | ✅ PASS |
| S4-03a | fis_backend | FISClient.status 返回标准状态字符串 (running/completed/...) | ✅ PASS |
| S4-03b | fis_backend | FISClient.wait_for_completion 超时后返回当前状态（不阻塞） | ✅ PASS |
| S4-04a | experiment | parse_duration 正确解析 2m/30s/1h | ✅ PASS |
| S4-04b | experiment | parse_duration 无效格式抛出 ValueError | ✅ PASS |
| S4-04c | experiment | MetricsSnapshot 模拟 PENDING→RUNNING→COMPLETED 状态转换 | ✅ PASS |
| S4-04d | experiment | StopCondition 在成功率低于阈值时触发 | ✅ PASS |
| S4-05a | metrics | DeepFlowMetrics.collect() mock ClickHouse 返回正确 MetricsSnapshot | ✅ PASS |
| S4-05b | metrics | ClickHouse 不可达时 collect() 返回 fallback（success_rate=100） | ✅ PASS |
| S4-06 | observability | ChaosMetrics.publish_experiment_metrics 调用 put_metric_data，含必要维度 | ✅ PASS |
| S4-07a | log_collector | LogCollectionResult.summary() 包含服务名和行数 | ✅ PASS |
| S4-07b | log_collector | PodLogCollector start_background/stop_and_collect 无异常（kubectl mock） | ✅ PASS |
| S4-08a | report | Reporter.generate_markdown 包含 name/status/fault_type 等必要字段 | ✅ PASS |
| S4-08b | report | Reporter.save_to_dynamodb 调用 DynamoDB put_item | ✅ PASS |
| S4-09a | neptune_sync | write_experiment 调用 query_opencypher 建立 TestedBy 边（≥2次调用） | ✅ PASS |
| S4-09b | neptune_sync | experiment_id/target_service 缺失时静默跳过 | ✅ PASS |
| S4-10 | neptune_sync | ChaosExperiment 节点含 experiment_id/fault_type/result/recovery_time_sec/degradation_rate | ✅ PASS |
| S4-11a | graph_feedback | write_back Neptune 可达时调用 query_gremlin | ✅ PASS |
| S4-11b | graph_feedback | Neptune 不可达时 write_back 抛出 RuntimeError("Neptune 不可达") | ✅ PASS |
| S4-12a | composite_runner | CompositeRunner 初始化含 metrics/scheduler/cm_client/fis_client | ✅ PASS |
| S4-12b | composite_runner | CompositeExperimentResult 含 action_states 字段 | ✅ PASS |
| S4-13a | target_resolver | TargetResolver 初始化含缓存字段 | ✅ PASS |
| S4-13b | target_resolver | 缓存命中时 resolve() 不调用 Neptune/AWS API | ✅ PASS |
| S4-13c | target_resolver | 缓存未命中时 _find_lambda_arn 通过 paginator 查找 ARN | ✅ PASS |
| S4-14a | fault_registry | FaultDef 含 type/backend/fis_action_id/default_params | ✅ PASS |
| S4-14b | fault_registry | fault_catalog.yaml 加载后 CATALOG 非空，含 pod_kill/network_delay/pod_failure | ✅ PASS |
| S4-14c | fault_registry | FIS_ACTION_MAP 中每个故障类型在 CATALOG 中有对应条目 | ✅ PASS |

---

## 技术说明

- **全部 mock**：无真实 AWS/Neptune/ClickHouse 连接
- **关键修复点**：
  - `graph_feedback` 导入时已绑定 `check_connectivity`/`query_gremlin`，需在 `runner.graph_feedback` 命名空间 patch
  - `FISClient` 状态查询方法为 `status()`，非 `get_status()`
  - `PodLogCollector` 停止方法为 `stop_and_collect()`
  - `Reporter` 公开方法为 `generate_markdown()` 和 `save_to_dynamodb()`
  - `TargetResolver` 缓存 key 格式：`{resource_type}:{service_name}`；Lambda ARN 查找用 `_find_lambda_arn()` + paginator
- **覆盖范围**：fault_injector / fis_backend / experiment / metrics / observability / log_collector / report / neptune_sync / graph_feedback / composite_runner / target_resolver / fault_registry

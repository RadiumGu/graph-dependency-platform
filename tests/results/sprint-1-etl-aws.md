# Sprint 1 ETL AWS 单元测试结果

**执行时间**: 2026-04-16  
**测试文件**: `tests/test_12_unit_etl_aws.py`  
**结果**: ✅ 15 passed / 0 failed — 6.70s

---

## 测试清单

| ID | 测试名称 | 覆盖模块 | 结果 |
|----|---------|---------|------|
| S1-01 | `test_s1_01_ec2_instance_collector_properties` | `collectors/ec2.py` — `collect_ec2_instances` | PASS |
| S1-02 | `test_s1_02_ec2_located_in_az_edge` | `collectors/ec2.py` + `neptune_client.upsert_edge` | PASS |
| S1-03 | `test_s1_03_ec2_security_group_collector` | `collectors/ec2.py` — `collect_security_groups` | PASS |
| S1-04 | `test_s1_04_eks_cluster_collector_properties` | `collectors/eks.py` — `collect_eks_cluster` | PASS |
| S1-05 | `test_s1_05_microservice_pod_runson_edge` | `collectors/eks.py` — `collect_eks_pods` | PASS |
| S1-06 | `test_s1_06_pod_ec2_runson_edge` | `collectors/eks.py` — pod→EC2 AZ lookup | PASS |
| S1-07 | `test_s1_07_rds_cluster_and_instance_collector` | `collectors/rds.py` | PASS |
| S1-08 | `test_s1_08_rds_instance_belongs_to_cluster_edge` | `collectors/rds.py` + `upsert_edge BelongsTo` | PASS |
| S1-09 | `test_s1_09_alb_and_target_group_collector` | `collectors/alb.py` | PASS |
| S1-10 | `test_s1_10_alb_routing_chain_lb_to_tg` | `collectors/alb.py` — LB→ListenerRule→TG 链 | PASS |
| S1-11 | `test_s1_11_data_stores_collectors` | `collectors/data_stores.py` — DDB/S3/SQS/SNS | PASS |
| S1-12 | `test_s1_12_lambda_and_stepfunction_collectors` | `collectors/lambda_sfn.py` | PASS |
| S1-13 | `test_s1_13_handler_run_etl_upserts_ec2_vertices` | `handler.run_etl` — 幂等写入验证 | PASS |
| S1-14 | `test_s1_14_handler_partial_failure_non_fatal` | `handler.run_etl` — ListenerRule 步骤失败不阻断 | PASS |
| S1-15 | `test_s1_15_graph_gc_drops_stale_nodes` | `graph_gc._gc_vertices` | PASS |

---

## 技术说明

### Mock 策略
- **AWS 服务**: `moto @mock_aws` 装饰器拦截所有 boto3 调用（EC2/EKS/RDS/ALB/DDB/S3/SQS/SNS/Lambda/SFN）
- **Neptune**: `neptune_client_base` Lambda Layer 以 `types.ModuleType` 注入 `sys.modules`，`neptune_query`/`upsert_vertex`/`upsert_edge` 均为 MagicMock
- **K8s API**: `collectors.eks._get_eks_token` + `urllib.request.urlopen` 打桩，注入 fake Pod JSON
- **conftest.py 隔离**: 在测试文件中定义 session-scoped `neptune_rca` 覆盖 fixture，阻止 autouse `cleanup_test_data` 连接真实 Neptune

### 已知限制 / Workaround
- **moto SQS `RedriveAllowPolicy`**: moto 5.x 不支持该属性名，测试中对 `get_queue_attributes` 做轻量包装，去掉该属性后再调用 moto — 不修改生产代码
- **`collectors` 包名冲突**: `rca/collectors/` 和 `etl_aws/collectors/` 同名，通过在导入前将 `ETL_PATH` 插入 `sys.path[0]` 并清除缓存解决

---

## 执行命令

```bash
python3 -m pytest tests/test_12_unit_etl_aws.py -v --tb=short
```

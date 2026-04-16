# Sprint 3 — RCA Actions + Collectors 单元测试结果

**执行日期:** 2026-04-16  
**执行环境:** Python 3.12.3 / pytest 9.0.2  
**测试文件:** `test_15_unit_rca_actions.py`, `test_16_unit_rca_collectors.py`  
**策略:** 全部 unittest.mock，零真实 AWS/Neptune/Slack 连接

---

## 汇总

| 指标 | 值 |
|------|-----|
| 总用例数 | 31 |
| 通过 | **31** |
| 失败 | 0 |
| 跳过 | 0 |
| 执行时长 | 0.54s |

---

## test_15_unit_rca_actions.py — RCA Actions

### S3-01: slack_notifier — 告警消息格式化

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_01_medium_risk_blocks_format` | ✅ PASS | MEDIUM/P1 → Block Kit 格式正确，section block 含服务名+等级 |
| `test_s3_02_http_error_returns_false_no_crash` | ✅ PASS | HTTP 500 → 返回 False，不抛异常 |

**Mock 策略:** `urllib3.PoolManager.request` 替换为 side_effect 捕获 payload；`_get_interact_url` mock 返回空串；`SLACK_WEBHOOK_URL` env patch。

---

### S3-02: incident_writer — Neptune 写入与幂等

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_03_write_incident_neptune_merge` | ✅ PASS | `nc.results` 第一调用含 `MERGE (inc:Incident ...)` 及正确 params |
| `test_s3_04_idempotent_uses_merge_not_bare_create` | ✅ PASS | 两次调用生成不同 incident_id；所有 cypher 无裸 `CREATE (` 语句 |

**Mock 策略:** `patch.object(neptune_client_module, 'results', return_value=[...])` — 直接 patch 已加载模块的函数属性，避免真实 Neptune HTTP 请求。

---

### S3-05: playbook_engine — 匹配与执行逻辑

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_05a_crashloop_matched` | ✅ PASS | `pod_status=CrashLoopBackOff` → `crashloop`, risk=MEDIUM |
| `test_s3_05b_db_connection_low_risk_auto` | ✅ PASS | `rds_connections=0.95` → `db_connection_exhausted`, risk=LOW, can_auto_exec=True |
| `test_s3_05c_no_match_returns_dynamic` | ✅ PASS | 未知 metric → 动态生成，matched_playbook=None, risk=UNKNOWN |
| `test_s3_05d_p0_never_auto_exec` | ✅ PASS | P0 故障 → can_auto_exec=False，无论 playbook risk 级别 |

**Mock 策略:** 无需 mock（纯逻辑函数）。

---

### S3-06: action_executor — 通知→写入→Playbook 编排

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_06_rollout_restart_dry_run_success` | ✅ PASS | dry_run=True → success=True, 跳过 K8s API 调用 |
| `test_s3_06b_rate_limit_exceeded_blocks_exec` | ✅ PASS | `_check_rate_limit` 返回 False → success=False, reason=rate_limit_exceeded |
| `test_s3_06c_scale_dry_run` | ✅ PASS | scale_deployment dry_run=True → 正确返回目标 replicas |

**Mock 策略:** `patch('actions.action_executor._check_rate_limit', return_value=True/False)` + `patch('actions.action_executor._audit')` 隔离 SSM/CloudWatch 调用。

---

### S3-07: feedback_collector — 反馈收集和存储

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_07_confirm_feedback_written` | ✅ PASS | feedback_type=confirm → Neptune 写入，返回 success=True |
| `test_s3_07b_deny_feedback` | ✅ PASS | feedback_type=deny → Neptune 写入成功 |
| `test_s3_07c_invalid_feedback_type_error` | ✅ PASS | 非法类型 → success=False |
| `test_s3_07d_missing_fields_returns_error` | ✅ PASS | 缺 incident_id → success=False (invalid payload) |

**Mock 策略:** `patch.object(nc_mod, 'results', return_value=[...])` 隔离 Neptune。

---

### S3-08: semi_auto — 半自动修复建议生成

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_08_p0_no_auto_exec` | ✅ PASS | P0 → mode=suggest, reason=P0_no_auto_exec，Slack 通知已触发 |
| `test_s3_08b_low_risk_semi_auto_executes` | ✅ PASS | LOW risk db_connection_exhausted → mode=semi-auto，rollout_restart 已调用 |
| `test_s3_08c_medium_risk_suggest_only` | ✅ PASS | MEDIUM risk → mode=suggest，不执行自动操作 |

**Mock 策略:** `patch('actions.slack_notifier.notify_fault')` (semi_auto 在函数内部 `from actions import slack_notifier`，故 patch 模块级函数而非模块属性)；`patch('actions.action_executor.rollout_restart')` 隔离 K8s。

---

## test_16_unit_rca_collectors.py — RCA Collectors

### S3-09: infra_collector — AWS 实时状态采集

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_09_collect_returns_pods_and_databases` | ✅ PASS | collect() 结构完整：pods + databases，字段正确 |
| `test_s3_09b_get_db_metrics_cloudwatch_rds_mocked` | ✅ PASS | mock boto3 RDS+CW → status=available, connections=87 |
| `test_s3_09c_pod_collection_k8s_fails_gracefully` | ✅ PASS | EKS token 获取失败 → 返回 []，不 crash |
| `test_s3_09d_format_for_prompt_structure` | ✅ PASS | format_for_prompt 文本含 Pod 名 + DB 引擎 |

**Mock 策略:** `patch('collectors.infra_collector.get_pods_for_service', ...)` 等子函数 mock；`patch('boto3.client', side_effect=fake_client)` 按服务名分发。

---

### S3-10: aws_probers — RDS/EKS/ALB 健康探针

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_10_alb_probe_unhealthy_target` | ✅ PASS | ALBProbe → unhealthy target → healthy=False, score_delta>0, evidence 含 Unhealthy |
| `test_s3_10b_alb_probe_no_alb_returns_none` | ✅ PASS | 找不到 ALB → probe 返回 None |
| `test_s3_10c_sqs_probe_dlq_has_messages` | ✅ PASS | DLQ 有 5 条消息 → healthy=False, score_delta>0 |
| `test_s3_10d_probe_result_prompt_block` | ✅ PASS | to_prompt_block() 含服务名、ANOMALY、evidence |
| `test_s3_10e_total_score_delta_capped_at_40` | ✅ PASS | 3 个异常 probe 累计 60 → 上限 40 |

**Mock 策略:** 直接实例化 `ALBProbe()`/`SQSProbe()` 并 `patch('boto3.client', side_effect=...)` 分发不同 mock client。

---

### S3-11: eks_auth — EKS 认证 Token 获取

| 用例 | 状态 | 说明 |
|------|------|------|
| `test_s3_11_get_k8s_endpoint_returns_endpoint_and_ca` | ✅ PASS | mock EKS describe_cluster → 返回 https endpoint + ca_data |
| `test_s3_11_write_ca_decodes_and_writes_file` | ✅ PASS | base64 CA → 临时 .crt 文件，内容一致 |
| `test_s3_11_get_eks_token_format` | ✅ PASS | SigV4 presign → token 以 `k8s-aws-v1.` 开头 |
| `test_s3_11_get_k8s_endpoint_eks_error_propagates` | ✅ PASS | EKS API 报错 → 异常正确传播，不静默吞掉 |

**Mock 策略:** `patch('boto3.client', ...)` + `patch('collectors.eks_auth.AWSRequest', ...)` + `patch('collectors.eks_auth.SigV4QueryAuth')` 完全隔离 AWS 调用。

---

## Warnings（非阻塞）

1. **InsecureRequestWarning** — neptune_client 模块加载时尝试连接 Neptune（conftest 中 NEPTUNE_ENDPOINT 已设置）。单元测试中 `nc.results` 已被 mock，实际未发起请求，警告来自 urllib3 连接池初始化。不影响测试正确性。
2. **DeprecationWarning: datetime.utcnow()** — 来自 `infra_collector.py:152`（源码使用 `datetime.utcnow()`），不影响功能。

---

## 覆盖验证

| Sprint 3 用例 ID | 覆盖状态 |
|-----------------|---------|
| S3-01 slack_notifier blocks 格式 | ✅ 已覆盖 |
| S3-02 Slack API 失败不 crash | ✅ 已覆盖 |
| S3-03 incident_writer Neptune MERGE | ✅ 已覆盖 |
| S3-04 incident_writer 幂等 | ✅ 已覆盖 |
| S3-05 playbook_engine 匹配逻辑 | ✅ 已覆盖（4 个子用例） |
| S3-06 action_executor 编排 | ✅ 已覆盖（3 个子用例） |
| S3-07 feedback_collector 存储 | ✅ 已覆盖（4 个子用例） |
| S3-08 semi_auto 半自动建议 | ✅ 已覆盖（3 个子用例） |
| S3-09 infra_collector AWS 采集 | ✅ 已覆盖（4 个子用例） |
| S3-10 aws_probers 健康探针 | ✅ 已覆盖（5 个子用例） |
| S3-11 eks_auth Token 获取 | ✅ 已覆盖（4 个子用例） |

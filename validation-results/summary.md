# Chaos Mesh MCP 工具验证汇总

验证时间：2026-02-28  
集群：PetSite (ap-northeast-1)  
测试目标：search-service (default namespace)

| # | 工具 | 状态 | 备注 |
|---|------|------|------|
| 1 | health_check | ✅ success | chaos_mesh_controllers=7 |
| 2 | list_namespaces | ✅ success | - |
| 3 | list_services_in_namespace | ✅ success | - |
| 4 | get_load_test_results | ✅ success | - |
| 5 | get_logs | ✅ success | - |
| 6 | load_generate | ✅ success | - |
| 7 | pod_kill | ✅ success | 有409冲突重试，最终成功 |
| 8 | pod_failure | ✅ success | 有409冲突重试，最终成功 |
| 9 | container_kill | ✅ success | 有409冲突重试，最终成功 |
| 10 | network_partition | ✅ success | 有409冲突重试，最终成功 |
| 11 | network_bandwidth | ✅ success | 有409冲突重试，最终成功 |
| 12 | network_delay | ✅ success | 新增，正常 |
| 13 | network_loss | ✅ success | 新增，正常 |
| 14 | network_corrupt | ✅ success | 新增，正常 |
| 15 | network_duplicate | ✅ success | 新增，正常 |
| 16 | inject_delay_fault | ⏭️ skip | 需要 Istio，当前集群未安装 |
| 17 | remove_delay_fault | ⏭️ skip | 需要 Istio，当前集群未安装 |
| 18 | pod_cpu_stress | ✅ success | 新增，正常 |
| 19 | pod_memory_stress | ✅ success | 新增，正常 |
| 20 | host_cpu_stress | ✅ success | 有409冲突重试，最终成功 |
| 21 | host_memory_stress | ✅ success | 有409冲突重试，最终成功 |
| 22 | host_disk_fill | ❌ failed | Python client 不支持 HOST_FILL，spec.mode 为空 422 |
| 23 | host_read_payload | ❌ failed | spec.mode 为空 422 |
| 24 | host_write_payload | ❌ failed | spec.mode 为空 422 |
| 25 | dns_chaos | ❌ failed | spec.scope 字段 CRD 不支持，unknown field 报错 |
| 26 | http_chaos | ✅ success | 新增，正常 |
| 27 | io_chaos | ✅ success | 新增，正常 |
| 28 | time_chaos | ✅ success | 新增，正常 |
| 29 | kernel_chaos | ✅ success | 新增，正常 |
| 30 | delete_experiment | ✅ verified | 各步骤清理均成功 |

## 统计
- ✅ 成功：24 个
- ❌ 失败：4 个
- ⏭️ Skip：2 个（需要 Istio）

## 待修复

### host_disk_fill / host_read_payload / host_write_payload
原因：Python client 传入 spec.mode 为空导致 422
修复：改用 kubectl apply YAML 方案（与 StressChaos workaround 相同思路）

### dns_chaos
原因：YAML 里的 spec.scope 字段当前 Chaos Mesh CRD 版本不支持
修复：移除 scope 字段，改用 spec.patterns 直接控制域名范围

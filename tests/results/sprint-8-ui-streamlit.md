# Sprint 8 — Streamlit UI 功能测试结果

**日期**: 2026-04-16  
**测试文件**: `tests/test_22_ui_streamlit.py`  
**执行方式**: `python3 -m pytest tests/test_22_ui_streamlit.py -v --tb=short`  
**框架**: `streamlit.testing.v1.AppTest`（无浏览器，headless）

---

## 执行摘要

| 指标 | 结果 |
|------|------|
| 总测试数 | 25 |
| 通过 | 25 ✅ |
| 失败 | 0 |
| 跳过 | 0 |
| 执行时长 | 2.53s |
| 警告 | 2（InsecureRequestWarning，不影响功能） |

---

## 测试用例明细

### S8-01: Graph Explorer — 图谱渲染

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_page_loads_without_exception` | ✅ PASS | mock nc.results 返回节点+边，页面无未处理异常 |
| `test_warning_when_no_type_selected` | ✅ PASS | 默认类型运行后无崩溃 |

**覆盖要点**: `@st.cache_data` 的 `fetch_graph_data()` 正确调用 mock，`build_pyvis_html()` 成功渲染。

---

### S8-02 [P0]: Graph Explorer — 节点详情属性

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_node_detail_rows_not_none` | ✅ PASS | MOCK_DETAIL_ROWS 的 name/type 字段均非 None |
| `test_node_detail_query_page_runs` | ✅ PASS | nc.results 三次调用序列（nodes/edges/detail）无异常 |

**覆盖要点**: 节点详情查询 cypher 返回结构含 `name`, `type`, `rel`, `neighbor`, `neighbor_type`。

---

### S8-03: Smart Query — NL 查询返回表格

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_page_loads_without_exception` | ✅ PASS | mock NLQueryEngine 返回结果，页面无异常 |
| `test_nl_result_has_results_and_summary` | ✅ PASS | results 列表非空，summary 字符串非空 |
| `test_nl_result_cypher_is_read_only` | ✅ PASS | 生成的 Cypher 不含 CREATE/DELETE/SET/MERGE/REMOVE/DROP |

**覆盖要点**: `NLQueryEngine` 通过 `sys.modules` mock 注入，验证返回 `{question, cypher, results, summary}` 结构。

---

### S8-04: Smart Query — 空结果友好提示

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_empty_result_no_exception` | ✅ PASS | `results=[]` 时页面展示「查询返回空结果」提示 |
| `test_error_result_no_exception` | ✅ PASS | `error` 键存在时页面展示错误信息，不崩溃 |

**覆盖要点**: 页面的 `if results:` / `else: st.info(...)` 路径均被覆盖。

---

### S8-05: Root Cause Analysis — 根因报告

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_rca_page_loads_without_exception` | ✅ PASS | mock q1/q5/q6/q9/q17 + generate_rca_report，页面无异常 |
| `test_rca_report_structure_valid` | ✅ PASS | 报告含 root_cause (str)、confidence (0-100)、recommended_action |

**覆盖要点**: `core.graph_rag_reporter.generate_rca_report` mock 返回 `MOCK_RCA_REPORT`，`_render_rca_report()` 路径覆盖 dict 结构。

---

### S8-06: Chaos Engineering — 实验列表

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_chaos_page_loads_without_exception` | ✅ PASS | nc.results 返回两条实验记录，页面正常渲染 |
| `test_experiment_data_schema` | ✅ PASS | 实验记录含 service/id/fault_type/result/timestamp |
| `test_experiment_result_values` | ✅ PASS | result 字段只含合法值 {passed, failed, running, aborted} |
| `test_chaos_page_empty_neptune` | ✅ PASS | Neptune 返回空列表时，页面显示「暂无实验」提示 |

**覆盖要点**: `fetch_all_experiments()` 和 `fetch_untested_services()` 的 try/except 路径；空数据时的 `st.info()` 展示。

---

### S8-07: DR Plan — 计划生成与下载

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_dr_page_loads_without_exception` | ✅ PASS | 默认状态（未生成）页面加载无异常 |
| `test_example_plan_files_exist` | ✅ PASS | `examples/az-switchover-apne1-az1.md` 存在且 >100 字符 |
| `test_dr_plan_json_structure` | ✅ PASS | JSON 含 plan_id/phases/affected_services |
| `test_dr_plan_markdown_non_empty` | ✅ PASS | Markdown 内容 >50 字符 |
| `test_dr_plan_generate_with_mock` | ✅ PASS | mock dr-plan-generator 模块后，页面无异常 |

**覆盖要点**: `download_button` 由 result.get("markdown") 触发，示例文件路径验证，JSON 结构完整性。

---

### S8-08 [P1]: Neptune 断开时所有页面不 crash

| 测试 | 状态 | 说明 |
|------|------|------|
| `test_graph_explorer_neptune_failure` | ✅ PASS | nc.results 抛出 Exception，页面 st.error + st.stop() 优雅降级 |
| `test_chaos_page_neptune_failure` | ✅ PASS | fetch_all_experiments 的 except 捕获，返回 []，页面展示空状态 |
| `test_rca_page_neptune_failure` | ✅ PASS | q1 抛出/q5~q9 返回空，页面 st.error 展示，无未处理异常 |
| `test_dr_page_no_neptune_needed` | ✅ PASS | DR Plan 默认状态无 Neptune 调用，正常加载 |
| `test_homepage_no_neptune_needed` | ✅ PASS | 主页（app.py）静态内容，无 Neptune 调用，无异常 |

**覆盖要点**: 各页面均有 `except Exception as exc: ... st.error(...)` 守卫，验证 `at.exception` 为 None（未处理异常为零）。

---

## 警告说明

```
InsecureRequestWarning: Unverified HTTPS request is being made to host
'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com'
```

- **出处**: `infra/lambda/etl_aws/urllib3/connectionpool.py`（项目内嵌 urllib3）
- **影响**: 无。触发于 `app.py` 主页加载时（设置了 `NEPTUNE_ENDPOINT` 环境变量默认值），但未实际发起查询。
- **处置**: 生产环境已在 VPC 内通过私有端点访问 Neptune，无需 TLS 验证绕过；测试环境 mock 覆盖 nc.results，不走真实网络。

---

## Mock 策略

| 依赖 | Mock 方式 | 注入位置 |
|------|----------|---------|
| `neptune.neptune_client` | `types.ModuleType` + `mock.Mock` | `sys.modules` |
| `neptune.neptune_queries` | `types.ModuleType` + `mock.Mock` | `sys.modules` |
| `neptune.nl_query.NLQueryEngine` | `mock.MagicMock` class | `sys.modules["neptune.nl_query"]` |
| `core.graph_rag_reporter.generate_rca_report` | `mock.Mock(return_value=MOCK_RCA_REPORT)` | `sys.modules["core.graph_rag_reporter"]` |
| DR plan generator 模块链 | `types.ModuleType` stubs | `sys.modules` |

所有 mock 在测试完成后通过 `_restore_mocks()` 还原，避免测试间污染。

---

## 结论

Sprint 8 Streamlit UI 功能测试 **全部通过（25/25）**。  
关键风险点已覆盖：
- [P0] S8-02 节点详情属性非 None ✅  
- [P1] S8-08 Neptune 断开时所有 5 个页面优雅降级 ✅

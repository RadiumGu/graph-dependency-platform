# chaos-automation 真实环境测试报告

**测试日期：** 2026-03-20  
**测试类型：** 真实环境集成测试（非 dry-run）  
**测试环境：** ip-10-1-2-198 → EKS PetSite / AWS ap-northeast-1  
**测试执行人：** 小乖乖（自动化）

---

## 一、测试范围与方法

本次测试在真实 AWS + EKS 环境中运行，涵盖以下模块：

| 模块 | 测试方法 | 是否真实调用 |
|------|----------|-------------|
| Target Resolver (FIS) | `resolve_targets.py --backend fis` | ✅ AWS API |
| Target Resolver (Chaos Mesh) | `resolve_targets.py --backend chaosmesh` | ✅ kubectl |
| DeepFlow 指标采集 | `DeepFlowMetrics.collect()` × 5 服务 | ✅ DeepFlow API |
| 5 Phase 引擎（Chaos Mesh） | `petsite-pod-kill.yaml`（真实注入） | ✅ kubectl apply CRD |
| 5 Phase 引擎（FIS Lambda） | `fis-lambda-delay-petstatusupdater.yaml` | ⚠️ Phase 2 失败（见 Bug-3） |
| RCA Trigger | `RCATrigger.trigger()` | ✅ Lambda 调用 |
| Graph Feedback | `GraphFeedback.write_back()` | ✅ neptune-proxy |
| DynamoDB History | `main.py history --service petsite` | ✅ DynamoDB scan |
| DynamoDB Setup | `main.py setup` | ✅ 表已存在验证 |
| FMEA | `fmea.py` | ✅ Neptune 14 节点 |

---

## 二、测试结果汇总

| 模块 | 状态 | 说明 |
|------|------|------|
| Target Resolver (FIS) | ✅ 通过 | 6/6 ARN 解析成功 |
| Target Resolver (Chaos Mesh) | ✅ 通过 | 5/5 服务 Pod 解析成功 |
| DeepFlow 指标采集 | ✅ 通过 | 5 服务均可采集，数据正常 |
| 5 Phase 引擎（petsite pod-kill） | ✅ **PASSED** | 全流程完成，恢复耗时 0.8s |
| CloudWatch Metrics 发布 | ✅ 通过 | 实验完成后指标自动发布 |
| DynamoDB 写入 | ✅ 通过 | 实验结果写入成功 |
| RCA Trigger (Lambda 调用) | ⚠️ 部分 | Lambda 调用成功，但结果解析失败（见 Bug-4） |
| Graph Feedback | ✅ 通过 | neptune-proxy 启动后写回正常 |
| FMEA | ✅ 通过 | Neptune 14 节点，RPN 计算正确 |
| FIS Lambda delay 实验 | ❌ 失败 | CloudWatch ARN 占位符未替换（见 Bug-3） |
| HypothesisAgent.to_experiment_yamls | ✅ 通过 | YAML 导出正常 |
| LearningAgent (DynamoDB 读取) | ✅ 通过 | 6 条历史记录读取正常 |

---

## 三、真实实验结果详情

### petsite-pod-kill（真实注入，完整 5 Phase）

```
实验 ID: exp-petsite-pod-kill-20260320-143752
故障:    pod_kill fixed-percent=50，持续 2m

Phase 0 ✅ Pre-flight:           2 pods ready
Phase 1 ✅ Steady State Before:  success_rate=100.0%, p99=3041ms
Phase 2 ✅ Fault Injection:      pod-kill-33ea8b48 已注入 (kubectl apply)
Phase 3 ✅ Observation:          
          T+0s:   100.0% / 3039ms
          T+10s:  100.0% / 3042ms
          T+20s:   95.3% / 3036ms  ← Pod 被杀，流量波动
          T+30s:   94.5% / 3051ms  ← RCA 触发
          T+74s:  100.0% / 3058ms  ← 服务自动恢复
          T+84s:  100.0% / 3056ms
Phase 4 ✅ Recovery:             2/2 pods running，耗时 0.8s
Phase 5 ✅ Steady State After:   success_rate=98.5% ≥ 95% ✅

最终状态:  PASSED
总耗时:    186s
最低成功率: 94.5%（未触发 Stop Condition 阈值 < 50%，符合预期）
恢复耗时:  0.8s（优秀）
```

### fis-lambda-delay-petstatusupdater（FIS 后端）

```
Phase 0 ✅ FIS Pre-flight:       IAM Role 可用
Phase 1 ✅ Steady State Before:  success_rate=100.0%
Phase 2 ❌ Fault Injection 异常:  ValidationException
         原因: CloudWatch stop condition ARN 包含未替换的占位符 ${REGION}/${ACCOUNT_ID}
```

---

## 四、各模块指标数据（真实采集）

### DeepFlow 指标（采集时间 22:36 CST）

| 服务 | success_rate | p99 | total_req (60s 窗口) |
|------|-------------|-----|---------------------|
| petsite | 100.0% | 3036ms | 486 |
| petsearch | 100.0% | 0ms | 0（无流量） |
| payforadoption | 100.0% | 0ms | 0（无流量） |
| pethistory | 33.3% | 10ms | 54 |
| petlistadoptions | 100.0% | 0ms | 0（无流量） |

> 注：pethistory 成功率 33.3% 属异常，后续测试中该服务稳态检查正确触发 ABORTED。

### DynamoDB 历史记录（petsite，最近 5 次）

| 时间 | 故障类型 | 状态 | min_sr | 恢复时间 |
|------|----------|------|--------|----------|
| 2026-03-06 15:15 | pod_kill | ✅ PASSED | 94.4% | 0.8s |
| 2026-03-06 15:09 | pod_kill | ❌ FAILED | 92.4% | 0.8s |
| 2026-03-06 14:58 | pod_kill | ❌ FAILED | 100% | 0.8s |
| 2026-03-06 04:25 | pod_kill | 💥 ERROR | 100% | — |
| 2026-03-06 03:58 | pod_kill | 💥 ERROR | 100% | — |

---

## 五、发现的问题（共 5 个，见 bugs.md）

详见 `/home/ubuntu/tech/chaos/docs/testdoc/bugs.md`

| 编号 | 严重程度 | 简述 |
|------|---------|------|
| Bug-1 | 🔴 高 | runner.py `check_pods()` 缺 K8s label 映射 |
| Bug-2 | 🟡 中 | config.py Bedrock 模型 ID 格式错误 |
| Bug-3 | 🔴 高 | FIS YAML CloudWatch ARN `${REGION}/${ACCOUNT_ID}` 占位符未替换 |
| Bug-4 | 🟡 中 | rca.py `_parse()` 未处理 `root_cause_candidates` 格式 |
| Bug-5 | 🟢 低 | graph_feedback 依赖 neptune-proxy 进程，无自动启动机制 |

---

## 六、结论

**系统整体可用，核心 Chaos Mesh 路径验证通过。**

- Chaos Mesh 后端（petsite pod-kill）端到端完整运行，实验结果写入 DynamoDB，报告生成正常
- Target Resolver 在真实环境可正确解析所有 FIS ARN 和 Chaos Mesh Pod 目标
- DeepFlow 指标采集正常，5 Phase 框架数据流无问题
- FIS 路径因 YAML 占位符问题（Bug-3）无法运行，需修复所有 FIS 实验文件

---

*报告由小乖乖生成 at 2026-03-20T14:44:53Z*

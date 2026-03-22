# chaos-automation 测试报告

**测试日期：** 2026-03-20  
**测试环境：** ip-10-1-2-198 (控制机，ap-northeast-1)  
**测试执行人：** 小乖乖（自动化）  
**测试目标：** 验证 chaos-automation 平台最新更新后的功能完整性

---

## 一、测试范围

| 模块 | 测试内容 |
|------|----------|
| 核心框架 | 模块导入、数据模型、YAML 解析 |
| 5 Phase 引擎 | dry-run 全流程（Phase 0-5） |
| Target Resolver | FIS ARN 解析、Chaos Mesh Pod 解析 |
| Orchestrator | 排序策略（by_tier / by_domain） |
| Agents 模块 | HypothesisAgent / LearningAgent 结构 |
| FMEA 模块 | Neptune 图谱读取 + RPN 计算 |
| 配置 | Bedrock 模型 ID 有效性 |

---

## 二、测试结果汇总

| 类别 | 通过 | 失败 | 备注 |
|------|------|------|------|
| 模块导入 | 13/13 | 0 | 全部成功 |
| YAML 实验文件加载 | 16/16 | 0 | tier0/tier1/fis/network 全部可解析 |
| parse_duration / parse_threshold | 8/8 | 0 | 边界值测试全通过 |
| SteadyStateCheck 逻辑 | 6/6 | 0 | 正常/异常快照均正确判断 |
| StopCondition 逻辑 | 4/4 | 0 | 触发/不触发逻辑正确 |
| ExperimentResult 数据模型 | 通过 | — | ID 生成、字段赋值正常 |
| dry-run: petsite-pod-kill | **PASSED** | — | 全 5 Phase 走完，报告生成 |
| dry-run: pethistory-network-delay | ABORTED | — | Phase 1 稳态基线不达标（正常熔断，行为符合预期）|
| dry-run: suite by_tier | 1 ABORTED | — | payforadoption Pre-flight 触发 Bug（见下） |
| Target Resolver: Chaos Mesh | 5/5 服务 | — | 全部服务 Pod 已解析并缓存 |
| Target Resolver: FIS | 6/6 ARN | — | 全部 ARN 已解析（aws-api 兜底） |
| Orchestrator 排序策略 | 通过 | — | by_tier / by_domain 排序结果正确 |
| FMEA 模块 | 通过 | — | Neptune 14 个节点读取，RPN 计算正确 |
| Agents 模块导入 | 通过 | — | HypothesisAgent / LearningAgent 可导入 |
| ChaosMetrics / Observability | 通过 | — | 接口签名正常 |

---

## 三、发现的问题

### 🔴 Bug-1：runner.py `ChaosMeshClient.check_pods` 缺少 K8s label 映射

**严重程度：** 高  
**文件：** `code/runner/runner.py` → `ChaosMeshClient.check_pods()`  
**现象：**  
Phase 0 对 `payforadoption` / `petsearch` / `petlistadoptions` 等服务执行 Pod 健康检查时，
直接用逻辑名（如 `payforadoption`）作为 K8s `app=` label 查询，
而这些服务实际 label 是 `app=pay-for-adoption` / `app=search-service` / `app=list-adoptions`。
导致 Phase 0 误报"无 Running Pods"而触发 ABORTED。

**根因：**  
`chaos_mcp.py` 的 `_selector_spec()` 已有正确的 `SERVICE_TO_K8S_LABEL` 映射，
但 `runner.py` 的 `ChaosMeshClient.check_pods()` 独立实现未复用此映射。

**复现：**
```bash
cd code && python3 main.py suite --dir experiments/tier0/ --strategy by_tier --dry-run
# payforadoption-http-chaos Pre-flight 失败: 服务 payforadoption 无 Running Pods
```

**修复建议：**  
在 `runner.py` 的 `ChaosMeshClient.check_pods()` 中添加 label 映射：
```python
SERVICE_TO_K8S_LABEL = {
    "petsearch":        "search-service",
    "payforadoption":   "pay-for-adoption",
    "petlistadoptions": "list-adoptions",
}

def check_pods(self, service: str, namespace: str = "default") -> dict:
    k8s_label = SERVICE_TO_K8S_LABEL.get(service, service)
    # ... 用 k8s_label 查询 Pod
```

---

### 🟡 Bug-2：`BEDROCK_MODEL` 配置 ID 格式无效

**严重程度：** 中（仅影响 LLM 分析报告，不影响实验执行）  
**文件：** `code/runner/config.py`  
**现状：**
```python
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-20250514"  # ❌ 格式错误
```
**报错：** `An error occurred (ValidationException) when calling the InvokeModel operation: The provided model identifier is invalid.`  
**说明：** 当前 us-east-1 可用的 Claude Sonnet 4 ID 为：
- `us.anthropic.claude-sonnet-4-20250514-v1:0`（含版本后缀，标准格式）
- 或 `anthropic.claude-sonnet-4-20250514-v1:0`（直接 inference）

**修复建议：**
```python
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)
```

---

### 🟢 信息：pethistory 稳态基线异常（非 Bug）

**文件：** `experiments/tier1/pethistory-network-delay.yaml`  
**现象：** Phase 1 稳态检查失败，`success_rate=51.5% < 90%`，实验正确 ABORTED  
**分析：** pethistory 服务当前成功率仅 51.5%，低于实验要求的 90% 基线阈值。
可能原因：服务本身有流量问题，或 DeepFlow 采集窗口内无足够流量（低流量 = 随机性大）。
5 Phase 框架的熔断机制**正常运作**，符合设计预期——稳态未达标则拒绝注入。  
**建议：** 若服务本身流量稀少，可调低 `steady_state.before.threshold` 至 `>= 50%`，
或增加采样窗口 `window: 5m` 以获得更稳定的基线读数。

---

## 四、各功能模块详细测试

### 4.1 模块导入测试

所有 13 个核心模块均可正常导入，无循环依赖、缺失依赖等问题：

```
✅ config / experiment / result / observability / target_resolver
✅ metrics / rca / report / graph_feedback / chaos_mcp
✅ fis_backend / runner / orchestrator
```

### 4.2 YAML 实验文件合规性（16/16）

| 目录 | 数量 | 状态 |
|------|------|------|
| `experiments/fis/` | 8 | ✅ 全部可解析（含 FIS ARN 运行时解析） |
| `experiments/tier0/` | 4 | ✅ 全部可解析 |
| `experiments/tier1/` | 3 | ✅ 全部可解析 |
| `experiments/network/` | 1 | ✅ 可解析 |

### 4.3 5 Phase 引擎 dry-run（petsite-pod-kill）

```
Phase 0 ✅ Pre-flight:         2 pods ready (petsite)
Phase 1 ✅ Steady State Before: success_rate=100.0%, p99=3056ms
Phase 2 ✅ Fault Injection:     [dry-run 跳过]
Phase 3 ✅ Observation:         [dry-run 跳过]
Phase 4 ✅ Recovery:            [dry-run 跳过]
Phase 5 ✅ Steady State After:  [dry-run 跳过]
最终状态: PASSED，报告已生成
```

### 4.4 Target Resolver

**Chaos Mesh（5 服务）：**

| 服务 | K8s Label | Pods |
|------|-----------|------|
| payforadoption | app=pay-for-adoption | 2/2 running |
| pethistory | app=pethistory | 2/2 running |
| petlistadoptions | app=list-adoptions | 2/2 running |
| petsearch | app=search-service | 2/2 running |
| petsite | app=petsite | 2/2 running |

**FIS（6 ARN）：**

| 资源类型 | 服务 | 解析来源 |
|----------|------|----------|
| ec2:subnet | ap-northeast-1a | aws-api |
| ec2:volume | ap-northeast-1a | aws-api |
| eks:nodegroup | PetSite | aws-api |
| lambda:function | statusupdater | aws-api |
| rds:cluster | payforadoption | aws-api |
| rds:cluster | petlistadoptions | aws-api |

> ⚠️ 注：所有 FIS ARN 均通过 aws-api 兜底解析，Neptune 图谱中对应数据可能不完整。

### 4.5 Orchestrator 排序策略

**by_tier（6 Tier1 → 10 Tier0）：** 低风险优先，排序正确  
**by_domain（compute/network/resources/dependencies 分组）：** 分组正确

### 4.6 FMEA 模块

- Neptune 读取 14 个服务节点（成功）
- RPN 计算：`petsite` RPN=15（🟡中风险），其余均为低风险
- 报告生成正常

---

## 五、主要改进亮点（本次更新观察）

1. **FIS 双后端架构完整**：`fis_backend.py` 和 `chaos_mcp.py` 作为统一接口层，`fault_injector.py` 提供抽象
2. **目标审计文件分离**：`targets-fis.json` 与 `targets-chaosmesh.json` 独立管理，清晰
3. **观测阶段结构化日志**：`structlog` JSON 格式，CloudWatch 友好
4. **CloudWatch Metrics 接口完整**：Phase 耗时 + 实验指标均有独立发布方法
5. **RCA 条件报告更细化**：区分"未启用"/"Phase 前终止"/"Phase 3 终止"等多种情况

---

## 六、建议修复优先级

| 优先级 | 问题 | 预估工时 |
|--------|------|----------|
| P0（立即）| Bug-1: check_pods label 映射 | 5 分钟（加 4 行映射代码） |
| P1（本周）| Bug-2: BEDROCK_MODEL ID 格式 | 1 行修改 |
| P2（优化）| Neptune FIS ARN 数据补全 | 视图谱数据情况而定 |
| P2（优化）| pethistory 稳态基线阈值 | YAML 配置调整 |

---

*报告由小乖乖自动生成 at 2026-03-20T14:35:39 CST*  
*测试工具：pytest-style Python unit tests + dry-run integration tests*

# chaos-automation Bug 报告

**日期：** 2026-03-20  
**环境：** 真实环境测试（ap-northeast-1）  
**发现方式：** 全模块集成测试

---

## Bug-1 🔴 高优先级

**标题：** `runner.py` `ChaosMeshClient.check_pods()` 缺少 K8s app label 映射

**文件：** `code/runner/runner.py` → `ChaosMeshClient.check_pods()`

**现象：**  
Phase 0 Pre-flight 对 `payforadoption` / `petsearch` / `petlistadoptions` 做 Pod 健康检查时，
直接用逻辑服务名作为 `app=` label 查询，导致 0 个 Pod 被返回，实验误判为 ABORTED。

**复现：**
```bash
cd code && python3 main.py suite --dir experiments/tier0/ --strategy by_tier --dry-run
# ❌ Pre-flight 失败: 服务 payforadoption 无 Running Pods（检查 label app=payforadoption）
```

**根因：**  
`chaos_mcp.py` 的 `_selector_spec()` 已有正确映射，但 `runner.py` 的同名方法独立实现未复用：
```
payforadoption  → app=pay-for-adoption
petsearch       → app=search-service
petlistadoptions → app=list-adoptions
```

**修复建议：**
```python
# 在 runner.py ChaosMeshClient.check_pods() 顶部添加：
_LABEL_MAP = {
    "petsearch":        "search-service",
    "payforadoption":   "pay-for-adoption",
    "petlistadoptions": "list-adoptions",
}

def check_pods(self, service: str, namespace: str = "default") -> dict:
    k8s_label = _LABEL_MAP.get(service, service)
    # 后续用 k8s_label 替换 service 做 kubectl 查询
```

**影响范围：** 使用 suite 批量执行时，所有涉及上述 3 个服务的 Chaos Mesh 实验都会 ABORTED

---

## Bug-2 ✅ 已修复

**标题：** `config.py` Bedrock 模型 ID 格式无效

**文件：** `code/runner/config.py`

**现象：** 每次实验完成后报告 LLM 分析部分报错，AI 分析结论为空：
```
WARNING runner.report: LLM 分析生成失败（非致命）: An error occurred (ValidationException) 
when calling the InvokeModel operation: The provided model identifier is invalid.
```

**根因：**
```python
# 当前（❌ 格式错误，缺少 -v1:0 版本后缀）
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-20250514")
```

us-east-1 实际可用 ID：
- `us.anthropic.claude-sonnet-4-20250514-v1:0`
- `anthropic.claude-sonnet-4-20250514-v1:0`

**修复：**
```python
# 已修复（2026-03-20）
BEDROCK_MODEL = os.environ.get(
    "BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"
)
```

**影响范围：** 仅影响报告中 LLM 分析部分，不影响实验执行和结果记录

---

## Bug-3 🔴 高优先级

**标题：** FIS 实验 YAML CloudWatch alarm ARN 含未展开占位符，导致所有 FIS 实验失败

**文件：** `experiments/fis/` 下所有 YAML（共 9 个文件）

**现象：**
```
ValidationException: Value for CloudWatch alarm StopCondition is not a valid ARN.
```

**根因：**  
所有 FIS 实验文件中 `cloudwatch_alarm_arn` 使用了 Shell 风格占位符，但 YAML 加载时未进行变量替换：
```yaml
# ❌ 原样传给 FIS API，不是合法 ARN
cloudwatch_alarm_arn: "arn:aws:cloudwatch:${REGION}:${ACCOUNT_ID}:alarm:chaos-xxx"
```

**受影响文件（9个）：**
```
experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml
experiments/fis/lambda/fis-lambda-error-petadoptionshistory.yaml
experiments/fis/rds/fis-aurora-failover.yaml
experiments/fis/rds/fis-aurora-reboot.yaml
experiments/fis/network-infra/fis-ebs-io-latency.yaml
experiments/fis/network-infra/fis-network-disrupt-az-1a.yaml
experiments/fis/eks-node/fis-eks-terminate-node-1a.yaml
experiments/fis/eks-node/fis-eks-terminate-node-1c.yaml
```

**修复方案（二选一）：**

**方案 A（推荐）—— YAML 直接写真实值：**
```yaml
cloudwatch_alarm_arn: "arn:aws:cloudwatch:ap-northeast-1:926093770964:alarm:chaos-xxx"
```

**方案 B —— experiment.py 加载时做变量替换：**
```python
# experiment.py load_experiment() 中，parse_stops 时替换 ARN 模板变量
def _expand_arn(arn: str) -> str:
    from .config import REGION, ACCOUNT_ID
    return arn.replace("${REGION}", REGION).replace("${ACCOUNT_ID}", ACCOUNT_ID)

def parse_stops(items):
    return [StopCondition(
        ...
        cloudwatch_alarm_arn=_expand_arn(c.get("cloudwatch_alarm_arn", "") or ""),
    ) for c in (items or [])]
```

> 或在 `fis_backend.py` 的 inject() 方法中，传给 FIS API 之前展开 ARN 变量。

**影响范围：** 所有 FIS 实验在 Phase 2 均会报错退出，FIS 后端完全不可用

---

## Bug-4 🟡 中优先级

**标题：** `rca.py` `_parse()` 未处理 Lambda 实际返回的 `root_cause_candidates` 格式

**文件：** `code/runner/rca.py` → `RCATrigger._parse()`

**现象：** RCA 触发成功（Lambda 200），但 `root_cause` 始终为空：
```
WARNING runner.runner: ⚠️ RCA 触发但失败: Lambda 返回成功但 root_cause 为空
```

**根因：**  
`rca.py` 文档注释描述了 `top_candidate` 字段，但 Lambda 实际返回的是 `root_cause_candidates` 数组，没有 `top_candidate`：
```json
// 实际返回（Lambda 真实响应）
{
  "rca": {
    "root_cause_candidates": [
      {"service": "petsite", "confidence": 1.0, "score": 100, "evidence": [...]}
    ]
  }
}
```
```python
# 当前代码（❌ 找不到 top_candidate）
top = rca.get("top_candidate") or {}    # → {}
root_cause = (top.get("service") or top.get("root_cause", "")).strip()  # → ""
```

**修复建议：**
```python
def _parse(self, body: dict) -> RCAResult:
    rca = body.get("rca") or body
    
    # 兼容 top_candidate（旧格式）和 root_cause_candidates（新格式）
    top = rca.get("top_candidate")
    if not top:
        candidates = rca.get("root_cause_candidates", [])
        top = candidates[0] if candidates else {}
    
    root_cause = (top.get("service") or top.get("root_cause", "")).strip()
    confidence = float(top.get("confidence", 0.0))
    evidence   = top.get("evidence", [])
    
    return RCAResult(
        root_cause=root_cause,
        confidence=confidence,
        evidence=evidence,
        raw=rca,
    )
```

**影响范围：** 所有启用 RCA 的实验，RCA 结果始终报告为"失败"，实验报告中 RCA 命中率无法正确统计

---

## Bug-5 🟢 低优先级

**标题：** `graph_feedback.py` 依赖 `neptune-proxy` 进程，无自动启动/检测机制

**文件：** `code/runner/graph_feedback.py`

**现象：**
```
ERROR runner.graph_feedback: Neptune Calls 边更新失败: HTTPConnectionPool(host='localhost', port=9876): 
Max retries exceeded with url: /gremlin (Caused by NewConnectionError(...Connection refused))
```

**根因：**  
`graph_feedback.py` 通过 `http://localhost:9876/gremlin`（neptune-proxy）写回图谱，
但没有检测代理是否运行，也没有 fallback 直连 Neptune 的逻辑。  
`neptune-proxy.py` 需要手动提前启动，无 systemd/supervisord 管理。

**修复建议（按优先级）：**

1. **最快（临时）**：在实验前脚本中 `pgrep neptune-proxy || python3 /home/ubuntu/tech/neptune-ui/neptune-proxy.py &`
2. **推荐**：`graph_feedback.py` 增加连接检测，代理不可用时直连 Neptune SigV4（参考 `target_resolver.py` 的 `_neptune_gremlin_query()`）
3. **长期**：将 neptune-proxy 注册为 systemd service，开机自启

**影响范围：** 所有实验的 Neptune 图谱反馈功能失效（non-fatal，不影响实验主流程）

---

*Bug 报告由小乖乖生成 at 2026-03-20T14:44:53Z*

"""hypothesis_common.py — HypothesisAgent 两个引擎共用的 prompt 模板与打分规则。

## 为什么单独一个模块

`hypothesis_strands.py` 原先在 `prioritize_with_meta()` 里**整个委托给
DirectBedrockHypothesis**，然后把结果打上 `engine="strands"` 标签：

    direct = DirectBedrockHypothesis(profile=self.profile)
    meta = direct.prioritize_with_meta(hypotheses)
    meta["engine"] = self.ENGINE_NAME      # ← direct 的结果，strands 的标签

也就是说 prioritize 这个功能**从来没被 Strands 化**，而任何按 engine
标签做的统计或基线都会以为它是 strands 跑的。这比静默降级更进一步 ——
它主动伪造了引擎标签。（原 docstring 自己承认「这里退化为调
Direct.prioritize()」，但标签照打。）

2026-09-20 真正实现 Strands 版 prioritize 时，把两件**与 LLM 调用方式
无关**的东西搬到这里，让两个引擎共用一份：

  · `build_prioritize_system_prompt()` —— 评分规则 prompt（约 130 行）
  · `apply_priority_scores()` —— 四维加权与排序

尤其是加权规则：business_impact×3 + blast_radius×1 + feasibility×2 +
learning_value×2。这个权重如果两个引擎各写一份，某天改了一处就会让
两边排序不一致，而这种不一致**不会报错**，只会让优先级悄悄不同。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from runner.config import BEDROCK_MODEL  # type: ignore
from runner.fault_registry import FAULT_DEFAULTS  # type: ignore

from .models import Hypothesis  # noqa: F401  （load_hypotheses 用）

logger = logging.getLogger(__name__)

# VALID_FAULT_TYPES 从 `runner/fault_registry.py` 的权威表派生。
#
# 删除前 `hypothesis_direct.py` 里**另有一份硬编码的 FAULT_DEFAULTS 字面量**，
# 只有 9 个类型，而 fault_registry 的权威表有 19 个 —— 那份副本是过期的，
# 后来新增的 10 种故障（io_chaos / time_chaos / http_delay / container_kill …）
# 从没同步过去。`extract_fault_type()` 按这个列表做关键词匹配，所以过期的
# 副本会让那 10 种故障**永远匹配不到**、静默落到 fallback 的 pod_kill。
#
# 按长度降序排列：避免短名先命中长名的前缀（如 http_chaos 抢在 http_delay 前）。
VALID_FAULT_TYPES = sorted(FAULT_DEFAULTS.keys(), key=len, reverse=True)


def build_prioritize_system_prompt() -> str:
    """Prompt Caching 的稳定前缀（评分规则 + 详解 + 示例）。

    长度要求 > 4000 chars 以满足 Sonnet/Opus ~1024 tokens 最低缓存门槛。
    """
    return """你是混沌工程优先级评估专家。用户会给你一组已经生成的混沌假设（每个包含 id/title/failure_domain/target_services/backend），你的任务是对每个假设按四个维度打 1-10 分，输出 JSON 数组。

## 评分维度详解

### 1. business_impact (1-10)
业务影响：从服务 tier 和在整体业务中的重要性考量。
- 10 分：核心 Tier0 用户前台服务（petsite / gateway），挂了立即影响所有用户
- 8-9 分：Tier0 关键依赖（payforadoption、auth-service、order-service）
- 6-7 分：Tier1 主要服务（petsearch、petlistadoptions）
- 4-5 分：Tier2 入门服务（pethistory、points-service）
- 1-3 分：工具类非业务服务（artillery、trafficgenerator）

### 2. blast_radius (1-10)
爆炸半径：实验引发的潜在影响范围。**注意这个维度越高越好，即小半径=高分**。
- 10 分：单节点/单 Pod 摩擦，业务 SR 降 <1%
- 8-9 分：fixed-percent <= 30%，duration < 3min
- 6-7 分：半数副本 fault，单 AZ
- 4-5 分：跨服务影响（A 挂导致 B 撤退）
- 1-3 分：跨 AZ、跨 region、业务全不可用

### 3. feasibility (1-10)
技术可行性：在现有 chaos-mesh + FIS 环境的依赖度。
- 10 分：hypothesis 指定的 fault_type 在标准 VALID_FAULT_TYPES，有现成 CRD/FIS action，target_services 都在拓扑内
- 8-9 分：需参数微调但没新工具
- 6-7 分：需自定义 probe 或外部工具
- 4-5 分：需操作器手工干预（比如让 RDS 进 failover）
- 1-3 分：当前环境无法执行（例如要求 us-east-1 但环境在 ap-northeast-1）

### 4. learning_value (1-10)
学习价值：假设提供的新信息的规模。
- 10 分：历史未做过 + 能验证核心 resilience 目标（多 AZ 故障等）
- 8-9 分：历史未做过但规模受限（单项组件）
- 6-7 分：组件测过但同参数下又测一次，重复影响但仍有新 runtime 变化
- 4-5 分：测过 + 参数接近，部分重复
- 1-3 分：完全重复历史实验，无新信息

## 总分计算公式
**最终优先级 = business_impact * 3 + blast_radius * 1 + feasibility * 2 + learning_value * 2**
权重给业务影响最大（因为混沌实验的目的就是验证业务 resilience），其次是可行性和学习价值。爆炸半径权重最低，因为它瞬时的鸿暂影响，不是学习目标本身。

## 输出格式必须严格
```json
[
  {"id": "H001", "business_impact": 8, "blast_radius": 7, "feasibility": 9, "learning_value": 8},
  {"id": "H002", "business_impact": 6, "blast_radius": 9, "feasibility": 7, "learning_value": 6}
]
```

## 输出示例详解
对 H001 "petsite pod_kill fixed-percent:50"：
- business_impact=8：Tier0 用户前台，影响面广但不是完全不可用
- blast_radius=7：fixed-percent 50% 注入算中等魔张，duration 限时可恢复
- feasibility=9：pod_kill 是标准 chaos-mesh 花色，直接用
- learning_value=8：历史未对 50% 规模做过，能验证 HPA 边界行为

对 H002 "trafficgenerator network_delay"：
- business_impact=3：压测工具服务对业务无直接影响
- blast_radius=9：单个压测容器，半径极小
- feasibility=7：network_delay 是标准 chaos-mesh，但压测容器 selector 稍特殊
- learning_value=4：压测容器 + 网络延迟 ≈ 自己给自己加压，信息量有限

## 重要约束
1. 输出必须是裸的 JSON 数组，每个元素包含四个维度的整数分数
2. 不要加任何解释文本，不要输出 total 字段（调用方会用上面的公式计算）
3. 不要跳过任何假设，每个都要打分
4. 用 ```json ... ``` 代码块包裹数组以便解析
5. 打分要体现各维度的差异，避免把所有假设都打 7-8 分
6. 打分要对不同假设呈现出区分度，不要扁平化

## 常见陷阱 / 不要做的事
- 不要给 Tier2 工具服务（artillery、trafficgenerator）business_impact >= 6 —— 它们不影响最终用户
- 不要给 dns_chaos 或 network_partition 这类大范围故障 blast_radius >= 8 —— 半径很大，分数应低
- 不要对于重复过多次的相同 fault_type 给 learning_value >= 7 —— 已无增量
- 不要对于 backend=fis 的 Lambda hypothesis 给 feasibility <= 5 —— FIS 本身对 Lambda 是成熟的
- 不要对于 target_services 只包含压测类工具的 hypothesis 给 learning_value >= 6 —— 自己压自己学习不到太多

## 调用场景
这个评分函数会在 chaos orchestrator 的 run_auto 模式下被调用一次：
1. HypothesisAgent.generate() 产出 N 个假设
2. HypothesisAgent.prioritize() 对这 N 个打分排序（调用你这里）
3. 取 top_n（通常 5-10）转成实验 YAML
4. ChaosRunner 按优先级顺序执行

所以评分的准确性直接影响「先测哪些假设」。一个坏的评分会让无效假设挤占宝贵的实验窗口；一个好的评分能让业务影响大、可行性高、学习价值大、半径小的假设优先被验证，这正是 chaos engineering 的核心收益。

## 输出二次检查
在你输出最终 JSON 前，请在心里做一次检查：
- 每个 id 都出现了吗？（不能漏任何假设）
- 四个维度都是 1-10 的整数吗？（不要小数、不要 0、不要 >10）
- 分数分布合理吗？（别都 7-8 或都 10）
- fault_scenario 中如果含 "dns_chaos" 或 "network_partition"，blast_radius 应该 <= 6
- fault_scenario 中如果含 "fixed-percent:50" 并且 duration <= 3min，blast_radius 应该 7-8

确认后再输出 JSON。

## 特殊场景打分指南

### 多服务级联假设
如果 hypothesis 的 target_services 包含多个服务（比如 [petsite, payforadoption]），说明是在测跨服务 resilience。这种：
- business_impact 取最高 tier 服务的打分
- blast_radius 酌情降 1-2 分，因为影响面比单服务大
- feasibility 如果是标准故障，保持 9-10；如果需要协调多 service，降到 6-7
- learning_value 通常 8-9，因为级联测试价值高

### Lambda + DynamoDB 假设
backend="fis" 且 target_resources 含 "lambda" 或 "dynamodb"：
- feasibility 基准 8-9（FIS 支持成熟）
- blast_radius 看配置——throttle concurrency=1 是 10 分高精度，cold_start 类的是 6-7
- learning_value 看是否有过 Lambda 故障的历史

### RDS 相关假设
- RDS failover 是 FIS 里的标准 action —— feasibility 9-10
- 但 RDS 挂 = 业务大面积受影响 —— blast_radius 必须 <= 4
- business_impact 看服务重要性（高）

### 单 Pod 微小故障
target_services 单个 + fixed:1 / fixed-percent:10 / duration < 2min：
- blast_radius 9-10 无疑
- business_impact 按 tier
- learning_value 看是否新场景

## 最终注意事项
按 id 顺序输出，便于 diff 比对。不要重新排序。不要为某个假设解释你的分数——解释留给后续的 Review 页面，此处只输出纯 JSON 数组。
"""


# 四维加权规则 —— **唯一一份**。
#
# 两个引擎都必须用它算总分。如果各写一份，某天改了一处就会让两边排序
# 悄悄不同：这种不一致不报错、不抛异常，只是同一批假设在两个引擎下
# 给出不同的执行顺序，而混沌实验是按这个顺序真的去打生产的。
PRIORITY_WEIGHTS = {
    "business_impact": 3,
    "blast_radius": 1,
    "feasibility": 2,
    "learning_value": 2,
}

_DEFAULT_SCORE = 5  # LLM 没给某一维时的中位分


def extract_json(text: str) -> Any:
    """从 LLM 响应里取 JSON（处理 markdown code fence）。"""
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    return json.loads(raw)


def apply_priority_scores(hypotheses: list, scores_list: list) -> list:
    """把 LLM 给的四维分数写回假设、加权求和、排序，并把 priority 改写成排名。

    行为与搬家前的 `DirectBedrockHypothesis.prioritize_with_meta` 逐字一致：

      1. 按 id 匹配分数；LLM 漏给的维度取中位分 5（不是 0 —— 漏评不等于最差）
      2. 加权求和（PRIORITY_WEIGHTS）
      3. 按总分降序排序
      4. **把 h.priority 从总分改写成排名 1..N**

    第 4 步容易看漏：排序时 `h.priority` 存的是加权总分，排完又被覆盖成
    名次。所以调用方拿到的 `priority` 是**名次**（1 最高），不是分数；
    分数留在 `h.priority_scores` 里。

    Args:
        hypotheses: Hypothesis 列表，**原地修改**并返回同一个列表。
        scores_list: LLM 输出的 [{id, business_impact, ...}, ...]。

    Returns:
        按优先级排好序的同一个列表。
    """
    scores_map = {s["id"]: s for s in (scores_list or []) if isinstance(s, dict) and "id" in s}
    missing = [h.id for h in hypotheses if h.id not in scores_map]
    if missing:
        # 不静默：LLM 漏评哪些假设必须留下痕迹，否则它们会以中位分混进排序
        logger.warning(
            "prioritize: LLM 未给 %d/%d 个假设评分，这些取中位分 %d：%s",
            len(missing), len(hypotheses), _DEFAULT_SCORE, missing,
        )

    for h in hypotheses:
        s = scores_map.get(h.id, {})
        h.priority_scores = {k: s.get(k, _DEFAULT_SCORE) for k in PRIORITY_WEIGHTS}
        h.priority = sum(h.priority_scores[k] * w for k, w in PRIORITY_WEIGHTS.items())

    hypotheses.sort(key=lambda h: h.priority, reverse=True)
    # ⚠️ priority 在此从「加权总分」被改写成「排名」，分数留在 priority_scores
    for rank, h in enumerate(hypotheses, 1):
        h.priority = rank
    return hypotheses


# ══════════════════════════════════════════════════════════════════════
# 假设的持久化与实验导出
#
# 这三件事（导出 YAML / 存盘 / 读盘）与「用哪个引擎调 LLM」完全无关，
# 却只住在 `hypothesis_direct.py` 里，而 `HypothesisBase` 并没有它们 ——
# 于是 `chaos/code/main.py` 和 `orchestrator.py` 拿 `HypothesisAgent()`
# 只为了调 `.load()` / `.save()` / `.to_experiment_yamls()`（注释里写着
# 「需 .load()/.to_experiment_yamls() 等附属方法」），把 direct 钉住了。
#
# 2026-09-20 搬到这里作为模块级函数。`self._topology` 改成显式参数 ——
# 它原本是实例里缓存的图拓扑，只被 `infer_tier` 用来查服务 Tier。
# ══════════════════════════════════════════════════════════════════════

HYPOTHESES_PATH = os.path.join(os.path.dirname(__file__), "..", "hypotheses.json")

TIER_CONFIG = {
    "Tier0": {"before_sr": 95, "after_sr": 95, "after_p99": 5000, "stop_sr": 50, "stop_p99": 8000, "rca": True},
    "Tier1": {"before_sr": 90, "after_sr": 90, "after_p99": 8000, "stop_sr": 30, "stop_p99": 15000, "rca": True},
    "Tier2": {"before_sr": 80, "after_sr": 80, "after_p99": 15000, "stop_sr": 20, "stop_p99": 30000, "rca": False},
}


def extract_fault_type(fault_scenario: str) -> str:
    """从 fault_scenario 描述中提取故障类型关键字。"""
    for ft in VALID_FAULT_TYPES:
        if ft in fault_scenario:
            return ft
    # fallback: 按关键词推断
    lower = fault_scenario.lower()
    if "kill" in lower or "崩溃" in lower:
        return "pod_kill"
    if "delay" in lower or "延迟" in lower:
        return "network_delay"
    if "loss" in lower or "丢包" in lower:
        return "network_loss"
    if "cpu" in lower:
        return "pod_cpu_stress"
    if "memory" in lower or "内存" in lower:
        return "pod_memory_stress"
    if "partition" in lower or "隔离" in lower:
        return "network_partition"
    if "dns" in lower:
        return "dns_chaos"
    return "pod_kill"

def infer_tier(service: str, topology: list | None = None) -> str:
    """从已缓存的拓扑中推断服务 Tier。"""
    if topology:
        for svc in topology:
            if svc.get("name") == service:
                return svc.get("tier", "Tier1")
    return "Tier1"

# ── 持久化 ───────────────────────────────────────────────────────


def to_experiment_yamls(
    hypotheses: list,
    output_dir: str = "experiments/generated",
    *,
    topology: list | None = None,
) -> list[str]:
    """将假设转化为兼容 runner load_experiment() 的 YAML 文件。"""
    os.makedirs(output_dir, exist_ok=True)
    paths = []

    for h in hypotheses:
        service = h.target_services[0] if h.target_services else "unknown"
        fault_type = extract_fault_type(h.fault_scenario)
        tier = infer_tier(service, topology)
        tc = TIER_CONFIG.get(tier, TIER_CONFIG["Tier1"])

        exp_name = f"{service}-{fault_type.replace('_', '-')}-{h.id.lower()}"
        ts = datetime.now().strftime("%Y%m%d")
        defaults = FAULT_DEFAULTS.get(fault_type, FAULT_DEFAULTS["pod_kill"])

        # 构建 fault block
        fault_lines = [
            f"  type: {fault_type}",
            f"  mode: {defaults['mode']}",
            f'  value: "{defaults["value"]}"',
            f'  duration: "{defaults["duration"]}"',
        ]
        for k in ("latency", "loss", "direction", "action", "size"):
            if k in defaults:
                v = defaults[k]
                fault_lines.append(f'  {k}: "{v}"' if isinstance(v, str) else f"  {k}: {v}")
        for k in ("workers", "load", "port"):
            if k in defaults:
                fault_lines.append(f"  {k}: {defaults[k]}")
        if "external_targets" in defaults and defaults["external_targets"]:
            fault_lines.append("  external_targets:")
            for t in defaults["external_targets"]:
                fault_lines.append(f'    - "{t}"')

        yaml_content = f"""\
# 由 HypothesisAgent 生成 — {datetime.now().strftime('%Y-%m-%d %H:%M')}
# 假设 {h.id}: {h.title}
# 故障域: {h.failure_domain} | 后端: {h.backend}

name: {exp_name}-{ts}
description: "{h.title}"

target:
  service: {service}
  namespace: default
  tier: {tier}

fault:
{chr(10).join(fault_lines)}

steady_state:
  before:
- metric: success_rate
  threshold: ">= {tc['before_sr']}%"
  window: "1m"
  after:
- metric: success_rate
  threshold: ">= {tc['after_sr']}%"
  window: "5m"
- metric: latency_p99
  threshold: "< {tc['after_p99']}ms"
  window: "5m"

stop_conditions:
  - metric: success_rate
threshold: "< {tc['stop_sr']}%"
window: "30s"
action: abort
  - metric: latency_p99
threshold: "> {tc['stop_p99']}ms"
window: "30s"
action: abort

rca:
  enabled: {str(tc['rca']).lower()}
  trigger_after: "30s"

graph_feedback:
  enabled: true
  edges:
- Calls

backend: {h.backend}

options:
  max_duration: "10m"
  save_to_bedrock_kb: false
"""
        filename = f"{exp_name}.yaml"
        path = os.path.join(output_dir, filename)
        with open(path, "w") as f:
            f.write(yaml_content)
        paths.append(path)
        logger.info(f"已生成: {path}")

    return paths


def save_hypotheses(hypotheses: list, path: str = HYPOTHESES_PATH,
                    *, topology: list | None = None):
    """保存假设库到 hypotheses.json。"""
    topology = topology or []
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "hypothesis-agent-v1",
        "model": BEDROCK_MODEL,
        "graph_snapshot": {
            "services": len(topology),
        },
        "hypotheses": [h.to_dict() for h in hypotheses],
    }
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"假设库已保存: {path} ({len(hypotheses)} 个假设)")

def load_hypotheses(path: str = HYPOTHESES_PATH) -> list[Hypothesis]:
    """从 hypotheses.json 加载假设库。"""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return [Hypothesis.from_dict(h) for h in data.get("hypotheses", [])]

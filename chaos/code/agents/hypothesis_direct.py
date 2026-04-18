"""
hypothesis_direct.py — Direct Bedrock Hypothesis 引擎（Phase 3 Module 1）。

从 hypothesis_agent.py rename 而来，类名 HypothesisAgent → DirectBedrockHypothesis。
继承 engines.base.HypothesisBase，新增 generate_with_meta() / prioritize_with_meta()
返回带迁移元数据的 dict；原 .generate() / .prioritize() / .save() / .load() API 保留，
保证 orchestrator / learning_agent / main 等调用方零改动。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

from engines.base import HypothesisBase

from .models import Hypothesis

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from runner.config import BEDROCK_REGION, BEDROCK_MODEL
from runner.neptune_client import query_gremlin_parsed, parse_graphson

logger = logging.getLogger(__name__)

HYPOTHESES_PATH = os.path.join(os.path.dirname(__file__), "..", "hypotheses.json")

# ─── 故障类型 → YAML 参数映射 ────────────────────────────────────────────────

FAULT_DEFAULTS = {
    "pod_kill":          {"mode": "fixed-percent", "value": "50", "duration": "2m"},
    "pod_failure":       {"mode": "fixed-percent", "value": "50", "duration": "3m"},
    "network_delay":     {"mode": "all", "value": "", "duration": "3m", "latency": "200ms"},
    "network_loss":      {"mode": "all", "value": "", "duration": "3m", "loss": "30"},
    "network_partition": {"mode": "all", "value": "", "duration": "2m", "direction": "both", "external_targets": []},
    "pod_cpu_stress":    {"mode": "all", "value": "", "duration": "5m", "workers": 2, "load": 80},
    "pod_memory_stress": {"mode": "all", "value": "", "duration": "5m", "size": "256MB"},
    "dns_chaos":         {"mode": "all", "value": "", "duration": "2m", "action": "error"},
    "http_chaos":        {"mode": "all", "value": "", "duration": "3m", "action": "delay", "port": 80, "delay": "1s"},
}

TIER_CONFIG = {
    "Tier0": {"before_sr": 95, "after_sr": 95, "after_p99": 5000, "stop_sr": 50, "stop_p99": 8000, "rca": True},
    "Tier1": {"before_sr": 90, "after_sr": 90, "after_p99": 8000, "stop_sr": 30, "stop_p99": 15000, "rca": True},
    "Tier2": {"before_sr": 80, "after_sr": 80, "after_p99": 15000, "stop_sr": 20, "stop_p99": 30000, "rca": False},
}

VALID_FAULT_TYPES = list(FAULT_DEFAULTS.keys())


# ─── Neptune Gremlin 客户端（委托给 neptune_client.py）──────────────────────

def _gremlin_query(gremlin: str) -> list:
    """执行 Neptune Gremlin 查询，返回解析后的 Python 对象列表。"""
    return query_gremlin_parsed(gremlin)


# ─── Bedrock LLM 调用 ────────────────────────────────────────────────────────

def _invoke_llm(prompt: str, max_tokens: int = 8192) -> str:
    """调用 Bedrock Claude 模型，返回文本响应。
    使用 boto3 内置 adaptive retry 模式（exponential backoff），
    自动处理 ThrottlingException / ServiceUnavailableException。
    """
    from botocore.config import Config as _BotocoreConfig
    client = boto3.client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=_BotocoreConfig(retries={"mode": "adaptive", "max_attempts": 5}),
    )
    resp = client.invoke_model(
        modelId=BEDROCK_MODEL,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"]


def _invoke_llm_tracked(
    prompt: str,
    max_tokens: int = 8192,
    system_prompt: str | None = None,
) -> tuple[str, dict]:
    """带 token_usage 采集的版本。保持与 _invoke_llm 行为一致，只是额外抽 usage。

    当 system_prompt 提供且 ≥ 3000 chars 时，自动对 system 加 cache_control=ephemeral
    → Prompt Caching 生效。

    Returns: (text, {"input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int})
    """
    from botocore.config import Config as _BotocoreConfig
    client = boto3.client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=_BotocoreConfig(retries={"mode": "adaptive", "max_attempts": 5}),
    )
    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt and len(system_prompt) > 3000:
        body["system"] = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    elif system_prompt:
        # 太短不能缓存，就晦黑直接当普通 system
        body["system"] = system_prompt
    resp = client.invoke_model(modelId=BEDROCK_MODEL, body=json.dumps(body))
    result = json.loads(resp["body"].read())
    text = result["content"][0]["text"]
    u = result.get("usage") or {}
    it = int(u.get("input_tokens", 0) or 0)
    ot = int(u.get("output_tokens", 0) or 0)
    cr = int(u.get("cache_read_input_tokens", 0) or 0)
    cw = int(u.get("cache_creation_input_tokens", 0) or 0)
    usage = {
        "input": it, "output": ot, "cache_read": cr, "cache_write": cw,
        "total": it + ot + cr + cw,
    }
    return text, usage


def _extract_json(text: str) -> list | dict:
    """从 LLM 响应中提取 JSON（处理 markdown code fence）。"""
    # 尝试找 ```json ... ``` 块
    import re
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    return json.loads(raw)


# ─── HypothesisAgent ─────────────────────────────────────────────────────────

class DirectBedrockHypothesis(HypothesisBase):
    """
    Direct Bedrock Hypothesis 引擎：直接调 Bedrock 生成混沌假设。

    输入源：Neptune 图谱拓扑 + DynamoDB 历史实验
    输出：hypotheses.json（假设库，含优先级排序）
    """

    ENGINE_NAME = "direct"

    def __init__(self, profile: Any = None, region: str = None, model: str = None):
        super().__init__(profile=profile)
        self.region = region or BEDROCK_REGION
        self.model = model or BEDROCK_MODEL
        self._last_model: str | None = None
        self._last_tokens: dict | None = None
        self._topology = None
        self._incidents = None
        self._infra_snapshot = None

    def _invoke_llm_tracked(self, prompt: str, max_tokens: int = 8192,
                            system_prompt: str | None = None) -> tuple[str, dict]:
        text, usage = _invoke_llm_tracked(
            prompt, max_tokens=max_tokens, system_prompt=system_prompt,
        )
        prev = self._last_tokens or {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_write": 0}
        self._last_tokens = {
            k: prev.get(k, 0) + usage.get(k, 0)
            for k in ("input", "output", "cache_read", "cache_write", "total")
        }
        return text, self._last_tokens

    def _pack(self, *, hypotheses: list, prioritized: list, t0: float,
              token_usage: dict | None = None, error: str | None = None) -> dict:
        out = {
            "hypotheses": list(hypotheses or []),
            "prioritized": list(prioritized or []),
            "engine": self.ENGINE_NAME,
            "model_used": self._last_model,
            "latency_ms": int((time.time() - t0) * 1000),
            "token_usage": token_usage or self._last_tokens,
            "trace": [],
        }
        if error is not None:
            out["error"] = error
        return out

    # ── Neptune 图谱查询 ─────────────────────────────────────────────

    def _query_topology(self) -> list[dict]:
        """获取完整服务依赖图（improvement-plan.md Gremlin 模板 #1）。"""
        if self._topology is not None:
            return self._topology
        gremlin = """
        g.V().hasLabel('Microservice')
          .project('name','tier','deps','callers','resources')
          .by('name')
          .by('recovery_priority')
          .by(out('DependsOn').values('name').fold())
          .by(in('DependsOn').values('name').fold())
          .by(out('RunsOn','DependsOn').hasLabel('LambdaFunction','RDSCluster','DynamoDBTable','SQSQueue').values('name').fold())
        """
        self._topology = _gremlin_query(gremlin)
        logger.info(f"Neptune 拓扑: {len(self._topology)} 个服务")
        return self._topology

    def _query_incidents(self) -> list[dict]:
        """获取历史故障模式（improvement-plan.md Gremlin 模板 #2）。"""
        if self._incidents is not None:
            return self._incidents
        gremlin = """
        g.V().hasLabel('Incident')
          .project('service','type','duration','impact')
          .by(out('AffectedService').values('name'))
          .by('incident_type')
          .by('duration_minutes')
          .by('impact_level')
        """
        try:
            self._incidents = _gremlin_query(gremlin)
        except Exception as e:
            logger.warning(f"历史故障查询失败（可能无 Incident 节点）: {e}")
            self._incidents = []
        return self._incidents

    def _query_history(self, service: Optional[str] = None) -> list[dict]:
        """从 DynamoDB 查询历史实验结果。"""
        from runner.query import ExperimentQueryClient
        client = ExperimentQueryClient()
        if service:
            return client.list_by_service(service, days=180, limit=50)
        # 查所有已知服务
        results = []
        for svc in self._query_topology():
            results.extend(client.list_by_service(svc["name"], days=180, limit=10))
        return results

    def _query_infra_snapshot(self, services: list[str]) -> dict:
        """通过 TargetResolver 获取实时基础设施快照（Pod 数量、节点、AWS 资源 ARN）。"""
        if self._infra_snapshot is not None:
            return self._infra_snapshot
        try:
            from runner.target_resolver import TargetResolver
            resolver = TargetResolver()
            self._infra_snapshot = resolver.get_infra_snapshot(services)
            logger.info(f"基础设施快照: {len(self._infra_snapshot)} 个服务")
        except Exception as e:
            logger.warning(f"基础设施快照获取失败（非致命，将用拓扑信息代替）: {e}")
            self._infra_snapshot = {}
        return self._infra_snapshot

    # ── 生成假设 ─────────────────────────────────────────────────────

    def generate_with_meta(
        self,
        max_hypotheses: int = 50,
        service_filter: str | None = None,
    ) -> dict:
        """生成假设主流程（带迁移元数据的 dict 返回）。表现等价于旧 generate()。"""
        t0 = time.time()
        self._last_model = self.model
        self._last_tokens = None
        try:
            topology = self._query_topology()
            if service_filter:
                topology = [s for s in topology if s["name"] == service_filter]
                if not topology:
                    raise ValueError(f"服务 {service_filter} 不在 Neptune 图谱中")

            incidents = self._query_incidents()
            history = self._query_history(service_filter)
            service_names = [s["name"] for s in topology]
            infra_snapshot = self._query_infra_snapshot(service_names)

            prompt_stable = self._build_generate_system_stable()
            prompt_user = self._build_generate_user_dynamic(
                topology, incidents, history, max_hypotheses, infra_snapshot,
            )
            logger.info("调用 Bedrock LLM 生成假设... (system_cache=%d chars)", len(prompt_stable))
            llm_response, usage = self._invoke_llm_tracked(
                prompt_user, system_prompt=prompt_stable,
            )

            raw_list = _extract_json(llm_response)
            hypotheses = []
            for i, item in enumerate(raw_list[:max_hypotheses]):
                h = Hypothesis(
                    id=item.get("id", f"H{i+1:03d}"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    steady_state=item.get("steady_state", ""),
                    fault_scenario=item.get("fault_scenario", ""),
                    expected_impact=item.get("expected_impact", ""),
                    failure_domain=item.get("failure_domain", "compute"),
                    target_services=item.get("target_services", []),
                    target_resources=item.get("target_resources", []),
                    backend=item.get("backend", "chaosmesh"),
                    source_context={"topology_services": len(topology), "incidents": len(incidents)},
                )
                hypotheses.append(h)
            logger.info(f"LLM 生成 {len(hypotheses)} 个假设")
        except Exception as e:
            logger.warning(f"generate_with_meta failed: {e}")
            return self._pack(
                hypotheses=[], prioritized=[], t0=t0, error=str(e),
            )
        return self._pack(
            hypotheses=hypotheses, prioritized=[], t0=t0,
            token_usage=usage,
        )

    # 保留原有同名方法仅作向后兼容；基类 .generate() 已提供直接返 list 的包装

    def _build_generate_prompt(
        self, topology: list, incidents: list, history: list, max_hypotheses: int,
        infra_snapshot: dict = None,
    ) -> str:
        """兼容旧 API（单 prompt 版，用于非缓存路径），等价于将 system_stable + user_dynamic 拼接。"""
        return (
            self._build_generate_system_stable()
            + "\n\n"
            + self._build_generate_user_dynamic(
                topology, incidents, history, max_hypotheses, infra_snapshot,
            )
        )

    @staticmethod
    def _build_generate_system_stable() -> str:
        """Prompt Caching 的稳定前缀（规则 + 示例 + fault type 详解）。不依赖本次输入。

        长度要求 > 3000 chars 以满足 Sonnet/Opus 最低缓存门槛。
        """
        valid_faults_str = ", ".join(VALID_FAULT_TYPES)
        return f"""你是混沌工程假设专家。根据用户提供的服务拓扑 / 历史实验 / 历史故障 / 实时基础设施快照生成混沌工程假设。

## 核心规则
1. 覆盖 5 大故障域：compute, data, network, dependencies, resources
2. 每个假设必须包含：稳态假设、故障场景、预期影响、验证标准
3. 优先关注 Tier0/Tier1 服务
4. 考虑级联故障（A 依赖 B，B 故障时 A 如何表现）
5. 避免生成已经验证过的重复假设
6. fault_scenario 中的故障类型必须是以下之一: {valid_faults_str}
7. backend 字段: 对 Lambda/RDS 等 AWS 资源用 "fis"，对 K8s Pod 用 "chaosmesh"
8. 按 JSON 数组格式输出，每个元素包含以下字段:
   id, title, description, steady_state, fault_scenario, expected_impact,
   failure_domain, target_services (数组), target_resources (数组), backend

## 故障类型详解
- pod_kill：删除一部分 Pod，验证副本自愈 / HPA 扩容 / PDB 保护。适用于有多副本的 StatefulSet/Deployment。
- pod_failure：让 Pod 单独停掉不被重新调度，验证跨 AZ failover。比 pod_kill 持久，影响更大。
- network_delay：网络延迟注入，验证超时重试 / 降级 策略。适用于上下游依赖 / 数据库查询场景。
- network_loss：网络丢包，验证重传 / 熔断。比 delay 更恶劣，适合测试 TCP 层鲁棒性。
- network_partition：设置网络隔离，完全切断 AZ 或服务间通信。验证 split-brain / quorum 行为。
- pod_cpu_stress / pod_memory_stress：资源压力注入，验证 HPA 扩容 / OOM kill / limit 保护。
- dns_chaos：DNS 响应异常或超时，验证服务依赖 DNS 解析的鲁棒性。
- http_chaos：HTTP 层注入延迟/错误/篑改，验证 SDK retry / timeout / circuit-breaker。

## Tier 分级实验标准
- Tier0：只在非工作时间做；假设 blast_radius <= 10%；before/after steady state 均 SR>=95%；自动 RCA
- Tier1：工作时间可做；blast_radius <= 30%；SR>=90%；自动 RCA
- Tier2：时间不限；blast_radius <= 50%；SR>=80%；RCA 不强制

## 输出示例
```json
[
  {{
    "id": "H001",
    "title": "petsite pod_kill 验证 HPA 快速扩容",
    "description": "删除 50% petsite Pod，观察 HPA 是否在 60s 内将副本恢复到 desired，且期间 ALB 健康检查没有全部失败。",
    "steady_state": "SR>=99%, p99<500ms",
    "fault_scenario": "pod_kill 50% petsite Pod 持续 2min",
    "expected_impact": "SR 暂降到 97%，HPA 在 60s 内恢复副本",
    "failure_domain": "compute",
    "target_services": ["petsite"],
    "target_resources": ["pod"],
    "backend": "chaosmesh"
  }}
]
```

## 实时基础设施提示（通用字句）
- 利用实际 Pod 数量决定 fault mode/value（例如 2 副本服务用 fixed:1 而非 fixed-percent:50）
- 单节点部署的服务，pod_kill 会导致完全不可用，需要更保守的实验参数
- 有 AWS 资源（Lambda/RDS）的服务可以同时设计 FIS 实验
- 没有 running Pod 的服务跳过 Chaos Mesh 类假设
- 不要给没有历史流量的服务做后期验证实验，优先选择有非空 Pod + 有 ALB 流量的服务

## 命名规范
- id 预留为 "H{{NNN}}" 格式但无须人工指定，调用方会重新编号
- target_services 是服务名字符串数组、target_resources 是 pod/lambda/rds/dynamodb 之类的资源类型
- description 和中文摘要都用简体中文

## 假设质量评价维度
后续 prioritize 阶段会对假设按以下 4 个维度评分，这里先告诉你方便你产出的假设本身就很有评分相关的质量：
- business_impact (1-10)：业务影响。Tier0 级别的初始红线服务得 8-10，Tier1 准关键服务 6-7，Tier2 得 4-5。假设描述清楚地关联业务指标（订单成功率 / 登录延迟）能拿高分。
- blast_radius (1-10)：爆炸半径。越小越好，因此 10 分 = 单节点或单 Pod 摩挣，1 分 = 全 region 中断。假设中指定 fixed-percent 比例、duration 短、有明确的 targeted pod selector 的容易拿高分。
- feasibility (1-10)：技术可行性。有现成 FIS action / Chaos Mesh CRD 的假设得 8-10；需要写自定义 probe / 外部工具的得 4-6；不可实施的 ≤ 3。
- learning_value (1-10)：学习价值。历史未验证过的故障场景得 8-10；验证过但基础设施有变化的得 5-6；完全重复历史实验的得 1-3。

你要出的是假设本身，不要直接写分数。但你心里要有这个标尺，避免产出不可实施 / 重复 / 没有业务指标的假设。

## 下游消费者的假定
生成的 Hypothesis 列表会被下游 runner 模块消费；它会按 target_services/target_resources 解析成 K8s namespace/label 或 AWS ARN，再用 backend 指定的手段（chaosmesh/fis）注入故障。所以必须：
- target_services 里的名字要完全匹配 Neptune 拓扑中的 Microservice 节点 name，不要自习似的别名
- target_resources 用小写英文关键字：pod / lambda / rds / dynamodb / sqs / ec2 / node 等
- fault_scenario 中的故障类型要是前面列出的 VALID_FAULT_TYPES之一，其他信息（比例、时长）用自然语描述

## 不要做的事（Do NOT）
1. 不要对 Tier2 服务生成 Tier0 级别的破坏性实验（优先级和爆炸半径不匹配）
2. 不要在一个假设里同时注入多种 fault（一个 Hypothesis = 一个故障变量）
3. 不要构造“理论上可能但拓扑中不存在的服务”假设（都在 Neptune 数据里有满足的节点）
4. 不要将 RDS 集群或 DynamoDB 表写成 target_resources=["pod"]（才不匹配 backend）
5. 不要生成 duration 超过 15 分钟的实验，blast radius 控制
6. 不要输出解释或中英文夹杂的描述，只用简体中文 + JSON
7. 不要引用拓扑里没出现的服务名，寄托 topology.services 里出现过的才写

## Few-shot 示例（输入 → 理想输出风格）
### 示例 1：单服务 pod_kill
输入：topology 中 petsite Tier0 服务，3 副本分布在 2 AZ，无历史实验
理想输出：
```json
{{
  "id": "H001", "title": "petsite 混部署验证 HPA",
  "description": "删除 1 个 petsite Pod，观察 HPA 是否在 60s 内补上副本",
  "steady_state": "SR>=99%",
  "fault_scenario": "pod_kill mode=fixed value=1 duration=2m",
  "expected_impact": "SR 暂降 1-2%，60s 内恢复",
  "failure_domain": "compute",
  "target_services": ["petsite"],
  "target_resources": ["pod"],
  "backend": "chaosmesh"
}}
```

### 示例 2：依赖性故障
topology：petsite -> payforadoption (Tier1) 的调用关系，历史无此组合实验
理想输出：
```json
{{
  "id": "H002", "title": "payforadoption 网络延迟验证 petsite 降级",
  "description": "在 payforadoption Pod 的 egress 注入 500ms 延迟，观察 petsite 调用 timeout 后是否降级",
  "steady_state": "petsite SR>=95%",
  "fault_scenario": "network_delay latency=500ms duration=3m",
  "expected_impact": "临时 SR 降到 93%，降级生效后回升",
  "failure_domain": "network",
  "target_services": ["payforadoption"],
  "target_resources": ["pod"],
  "backend": "chaosmesh"
}}
```

### 示例 3：Lambda 服务
topology：statusupdater 是 Lambda，依赖 DynamoDB pets 表
理想输出：
```json
{{
  "id": "H003", "title": "statusupdater Lambda throttle 验证降级",
  "description": "对 statusupdater Lambda 设置 reserved concurrency=1，观察上游扩量的究极而进行操作",
  "steady_state": "Lambda invocation SR>=99%",
  "fault_scenario": "lambda_throttle concurrency=1 duration=5m",
  "expected_impact": "SR 降到 50%，触发 SQS 中的 DLQ 积压告警",
  "failure_domain": "compute",
  "target_services": ["statusupdater"],
  "target_resources": ["lambda"],
  "backend": "fis"
}}
```

## Tool 名词表
- pod_kill / pod_failure: K8s Pod 层故障，需要 chaosmesh backend
- network_delay / network_loss / network_partition: Pod 网络注入，chaosmesh
- pod_cpu_stress / pod_memory_stress: 资源压力，chaosmesh
- dns_chaos: DNS 响应异常，chaosmesh
- http_chaos: HTTP 层注入，chaosmesh
所有 AWS 原生服务（Lambda/RDS/DynamoDB/SQS）用 fis backend，存于 FIS experiment templates 中有定义的故障。
"""

    def _build_generate_user_dynamic(
        self, topology: list, incidents: list, history: list,
        max_hypotheses: int, infra_snapshot: dict = None,
    ) -> str:
        history_summary = []
        for item in history[:30]:
            history_summary.append({
                "service": item.get("target_service", {}).get("S", ""),
                "fault": item.get("fault_type", {}).get("S", ""),
                "status": item.get("status", {}).get("S", ""),
            })
        infra_block = ""
        if infra_snapshot:
            infra_block = f"\n## 实时基础设施状态\n{json.dumps(infra_snapshot, ensure_ascii=False, indent=2)}\n"
        return (
            f"## 服务拓扑\n{json.dumps(topology, ensure_ascii=False, indent=2)}\n"
            f"{infra_block}"
            f"## 历史实验结果\n{json.dumps(history_summary, ensure_ascii=False, indent=2)}\n\n"
            f"## 历史故障事件\n{json.dumps(incidents, ensure_ascii=False, indent=2)}\n\n"
            f"## 本次要求\n最多生成 {max_hypotheses} 个假设。直接输出 JSON 数组。"
        )

    # ── 优先级排序 ───────────────────────────────────────────────────

    def prioritize_with_meta(self, hypotheses: list[Hypothesis]) -> dict:
        """用 LLM 对假设进行多维度评分（dict 版本。旧 prioritize() 自动从本方法返回值中抽 list）。

        business_impact, blast_radius, feasibility, learning_value
        每项 1-10 分，加权求和后排序。
        """
        t0 = time.time()
        self._last_model = self.model
        self._last_tokens = None
        if not hypotheses:
            return self._pack(hypotheses=[], prioritized=hypotheses, t0=t0)

        summaries = [
            {"id": h.id, "title": h.title, "failure_domain": h.failure_domain,
             "target_services": h.target_services, "backend": h.backend}
            for h in hypotheses
        ]

        prompt = f"""你是混沌工程优先级评估专家。对以下假设进行多维度评分。

## 假设列表
{json.dumps(summaries, ensure_ascii=False, indent=2)}

## 评分维度（每项 1-10 分）
- business_impact: 业务影响（Tier0 服务 > Tier1 > Tier2）
- blast_radius: 爆炸半径（越小越安全，分数越高越好，即小半径=高分）
- feasibility: 技术可行性（有现成 FIS action / Chaos Mesh CRD 的优先）
- learning_value: 学习价值（未测试过的故障模式优先）

## 输出格式
JSON 数组，每个元素: {{"id": "H001", "business_impact": 8, "blast_radius": 7, "feasibility": 9, "learning_value": 8}}

```json
[...]
```"""

        logger.info("调用 Bedrock LLM 评估优先级...")
        try:
            llm_response, usage = self._invoke_llm_tracked(prompt, max_tokens=4096)
            scores_list = _extract_json(llm_response)
        except Exception as e:
            logger.warning(f"prioritize_with_meta failed: {e}")
            return self._pack(hypotheses=[], prioritized=hypotheses, t0=t0, error=str(e))

        scores_map = {s["id"]: s for s in scores_list if "id" in s}
        for h in hypotheses:
            s = scores_map.get(h.id, {})
            h.priority_scores = {
                "business_impact": s.get("business_impact", 5),
                "blast_radius": s.get("blast_radius", 5),
                "feasibility": s.get("feasibility", 5),
                "learning_value": s.get("learning_value", 5),
            }
            ps = h.priority_scores
            total = (ps["business_impact"] * 3 + ps["blast_radius"] * 1
                     + ps["feasibility"] * 2 + ps["learning_value"] * 2)
            h.priority = total

        hypotheses.sort(key=lambda h: h.priority, reverse=True)
        for rank, h in enumerate(hypotheses, 1):
            h.priority = rank

        return self._pack(hypotheses=[], prioritized=hypotheses, t0=t0, token_usage=usage)

    # ── 导出实验 YAML ────────────────────────────────────────────────

    def to_experiment_yamls(
        self,
        hypotheses: list[Hypothesis],
        output_dir: str = "experiments/generated",
    ) -> list[str]:
        """将假设转化为兼容 runner load_experiment() 的 YAML 文件。"""
        os.makedirs(output_dir, exist_ok=True)
        paths = []

        for h in hypotheses:
            service = h.target_services[0] if h.target_services else "unknown"
            fault_type = self._extract_fault_type(h.fault_scenario)
            tier = self._infer_tier(service)
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

    def _extract_fault_type(self, fault_scenario: str) -> str:
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

    def _infer_tier(self, service: str) -> str:
        """从已缓存的拓扑中推断服务 Tier。"""
        if self._topology:
            for svc in self._topology:
                if svc.get("name") == service:
                    return svc.get("tier", "Tier1")
        return "Tier1"

    # ── 持久化 ───────────────────────────────────────────────────────

    def save(self, hypotheses: list[Hypothesis], path: str = HYPOTHESES_PATH):
        """保存假设库到 hypotheses.json。"""
        topology = self._topology or []
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

    @staticmethod
    def load(path: str = HYPOTHESES_PATH) -> list[Hypothesis]:
        """从 hypotheses.json 加载假设库。"""
        if not os.path.exists(path):
            return []
        with open(path) as f:
            data = json.load(f)
        return [Hypothesis.from_dict(h) for h in data.get("hypotheses", [])]

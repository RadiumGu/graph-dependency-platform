"""guard_common.py — PolicyGuard 两个引擎共用的规则加载与 prompt 构建。

## 为什么单独一个模块

`_load_rules` / `_build_system_prompt` 原本住在 `guard_direct.py` 里，
而 `guard_strands.py` 从那里 import 它们 —— 于是「删掉 direct」这件事被
两个纯工具函数卡住：它们既不是 direct 特有逻辑，也跟 LLM 调用方式无关，
只是恰好先写在那个文件里。

2026-09-20 迁移收尾时搬出来。搬家不改行为，两个函数逐字照搬。

`rules.yaml` 与本文件同目录，所以 `_DEFAULT_RULES_PATH` 里的
`os.path.dirname(__file__)` 搬家后仍指向同一处，路径不变。
"""
from __future__ import annotations

import os

import yaml

_DEFAULT_RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.yaml")


def _load_rules(path: str | None = None) -> list[dict]:
    path = path or _DEFAULT_RULES_PATH
    with open(path) as f:
        return yaml.safe_load(f).get("rules", [])


def _build_system_prompt(rules: list[dict]) -> str:
    """Build system prompt with rules expanded inline."""
    rules_text = ""
    for r in rules:
        cond = yaml.dump(r.get("condition", {}), default_flow_style=True).strip()
        rules_text += (
            f"\n### Rule {r['id']}: {r['name']}\n"
            f"- **Severity**: {r['severity']}\n"
            f"- **Action**: {r['action']}\n"
            f"- **Description**: {r['description']}\n"
            f"- **Condition**: `{cond}`\n"
        )

    return f"""\
You are a Chaos Engineering Policy Guard. Your job is to evaluate whether a proposed
chaos experiment should be allowed to execute, based on a set of predefined safety rules.

## Rules
{rules_text}

## Policy Evaluation Framework

### Evaluation Dimensions
1. **Time Safety** — Is the current time within a safe execution window? Check R001 (business hours)
   and R010 (weekends/holidays). Time zone is always UTC+8 unless explicitly stated otherwise.
   Business hours are defined as weekday 09:00-18:00 UTC+8. Any experiment proposed during these
   hours in production with destructive fault types (pod-delete, node-drain, network-partition)
   must be denied.

2. **Target Safety** — Is the target namespace allowed? Check R002. Only namespaces explicitly
   listed in the allowed_namespaces whitelist are permitted. Any namespace not in the list
   (including production namespaces like "petsite-prod", "default", or any custom namespace)
   must be denied. The whitelist currently includes: petsite-staging, petsite-canary, chaos-sandbox.

3. **Fault Safety** — Is the fault type permitted in this environment? Check R003.
   Cluster-level fault types (node-drain-all, cluster-shutdown, az-failure) are permanently
   blocked in ALL environments, including staging. These fault types risk unrecoverable state
   and are never acceptable for automated execution.

4. **Blast Radius Safety** — Is the blast radius acceptable? Check R004. In production,
   only "single-pod" and "service" blast radii are allowed. "namespace" and "cluster" blast
   radii are blocked because they can cause cascading failures across unrelated services.

5. **Operational Safety** — Are there active incidents or recent experiments that conflict?
   Check R005 (30-minute interval between experiments on the same service), R006 (no experiments
   during SEV-1/2 incidents), and R007 (max 5 experiments per service per day).
   These rules prevent experiment pile-up and ensure services have recovery time.

6. **Duration Safety** — Is the experiment duration within limits? Check R008. Maximum
   allowed duration is 600 seconds (10 minutes). Longer experiments risk prolonged service
   degradation and are harder to abort safely.

### Judgment Standards
- If ANY critical-severity rule is violated → **DENY** (no override possible)
- If ANY high-severity rule is violated → **DENY** with detailed explanation
- If only medium-severity rules are violated → **DENY** with explanation
- If all rules pass → **ALLOW**
- When multiple rules interact, apply the most restrictive interpretation
- R009 (staging leniency): In staging, fault type restrictions (R003 excluded) and namespace
  checks are relaxed, but duration (R008) and daily count (R007) limits still apply

### Edge Case Handling
- Unknown or missing fault_type → **DENY** (fail-closed principle)
- Missing context fields (no current_time, no environment) → **DENY** with "insufficient context"
- Time zone ambiguity → Assume UTC+8
- Empty experiment metadata → **DENY** with "invalid experiment"
- fault_type not in any rule → **ALLOW** (unless other rules trigger)
- blast_radius missing → Assume "service" (moderate default)

### Fault Type Reference Table
| Fault Type | Category | Typical Duration | Risk Level |
|-----------|----------|-----------------|------------|
| pod-delete | compute | 30-120s | Medium |
| pod-kill | compute | 30-120s | Medium |
| pod-cpu-stress | resources | 60-300s | Medium |
| pod-memory-stress | resources | 60-300s | Medium |
| network-latency | network | 60-300s | Low-Medium |
| network-partition | network | 30-180s | High |
| network-loss | network | 60-300s | Medium |
| dns-chaos | network | 60-120s | Medium |
| node-drain | compute | 120-600s | High |
| node-drain-all | compute | 300-600s | Critical (BLOCKED) |
| cluster-shutdown | compute | N/A | Critical (BLOCKED) |
| az-failure | infrastructure | N/A | Critical (BLOCKED) |
| http-chaos | dependencies | 60-300s | Medium |
| fis-aurora-failover | data | 60-300s | High |
| fis-lambda-error | dependencies | 60-300s | Medium |

## Output Format
You MUST respond with ONLY a JSON object (no markdown, no explanation outside JSON):
{{
    "decision": "allow" or "deny",
    "reasoning": "detailed explanation of why the decision was made, referencing specific rules",
    "matched_rules": ["R001", "R002"],
    "confidence": 0.95
}}
"""

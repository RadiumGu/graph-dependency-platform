"""
registry/rule_parser.py — Natural language rule parser using LLM (Bedrock Claude)

Parses the ``## 自定义规则`` section of a Markdown policy file into
structured CustomRule objects via LLM.

PRD §10.3: Customer writes NL business rules, LLM parses them into
structured rules with trigger, scope, condition, action, and check commands.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def parse_custom_rules(markdown_text: str) -> list:
    """Parse natural language rules from Markdown policy text.

    Extracts the ``## 自定义规则`` (or ``## Custom Rules``) section,
    splits by ``###`` sub-headings (categories), and sends to LLM
    for structured parsing.

    Args:
        markdown_text: Full Markdown policy text.

    Returns:
        List of CustomRule-like dicts (or CustomRule objects if model available).
    """
    # Extract the custom rules section
    rules_text = _extract_rules_section(markdown_text)
    if not rules_text:
        return []

    # Parse categories and individual rules
    categories = _split_categories(rules_text)

    # Try LLM parsing first
    try:
        parsed = _llm_parse_rules(categories)
        if parsed:
            return _to_custom_rules(parsed)
    except Exception as exc:
        logger.warning("LLM rule parsing failed, falling back to regex: %s", exc)

    # Fallback: regex-based parsing
    return _regex_parse_rules(categories)


def _extract_rules_section(text: str) -> str:
    """Extract content under ## 自定义规则 or ## Custom Rules heading."""
    patterns = [
        r"## 自定义规则\s*\n(.*?)(?=\n## |\Z)",
        r"## Custom Rules\s*\n(.*?)(?=\n## |\Z)",
        r"## custom_rules\s*\n(.*?)(?=\n## |\Z)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _split_categories(text: str) -> Dict[str, List[str]]:
    """Split rules text into categories (### headings) → list of rule strings."""
    categories: Dict[str, List[str]] = {}
    current_category = "general"
    current_rules: List[str] = []

    for line in text.split("\n"):
        line = line.rstrip()
        if line.startswith("### "):
            if current_rules:
                categories[current_category] = current_rules
            current_category = line[4:].strip()
            current_rules = []
        elif line.startswith("- "):
            current_rules.append(line[2:].strip())
        elif line.startswith("  ") and current_rules:
            # Continuation of previous rule
            current_rules[-1] += " " + line.strip()

    if current_rules:
        categories[current_category] = current_rules

    return categories


def _llm_parse_rules(categories: Dict[str, List[str]]) -> Optional[List[Dict]]:
    """Use Bedrock Claude to parse NL rules into structured format.

    Returns None if LLM is unavailable.
    """
    try:
        import boto3
    except ImportError:
        return None

    rules_text = ""
    for category, rules in categories.items():
        rules_text += f"\n### {category}\n"
        for rule in rules:
            rules_text += f"- {rule}\n"

    prompt = f"""将以下业务规则解析为结构化的 DR Plan Policy 规则。每条规则输出 JSON 格式：

{{
  "id": "rule-NNN",
  "source_text": "原始规则文本",
  "category": "分类名",
  "trigger": "before_step | after_step | before_phase | after_phase | before_plan | schedule",
  "scope": {{
    "phase_ids": ["phase-1"] 或 null,
    "resource_types": ["RDSCluster"] 或 null,
    "resource_names": null,
    "scope_types": null
  }},
  "condition": {{
    "type": "metric | state | time | approval | custom",
    "check": "检查表达式",
    "threshold": null 或具体值,
    "command": "AWS CLI / kubectl 检查命令" 或 null
  }},
  "action": "block | warn | require_approval | skip",
  "action_message": "不满足时的提示信息",
  "confidence": 0.0-1.0
}}

业务规则：
{rules_text}

请返回 JSON 数组，每条规则一个对象。只返回 JSON，不要其他文字。"""

    try:
        client = boto3.client("bedrock-runtime", region_name="us-west-2")
        response = client.invoke_model(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        result = json.loads(response["body"].read())
        content = result.get("content", [{}])[0].get("text", "")

        # Extract JSON from response
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as exc:
        logger.warning("Bedrock LLM call failed: %s", exc)

    return None


def _regex_parse_rules(categories: Dict[str, List[str]]) -> list:
    """Fallback: simple regex-based rule extraction.

    Creates basic CustomRule-like dicts from NL text patterns.
    """
    from validation.verification_models import CustomRule, RuleCondition, RuleScope

    rules: list = []
    rule_idx = 1

    for category, rule_texts in categories.items():
        for text in rule_texts:
            rule = CustomRule(
                id=f"rule-{rule_idx:03d}",
                source_text=text,
                category=category,
                trigger=_infer_trigger(text),
                scope=RuleScope(
                    resource_types=_infer_resource_types(text),
                    phase_ids=_infer_phases(text),
                ),
                condition=RuleCondition(
                    type="custom",
                    check=text,
                ),
                action=_infer_action(text),
                action_message=text,
                confidence=0.5,  # Low confidence for regex parsing
                human_verified=False,
            )
            rules.append(rule)
            rule_idx += 1

    return rules


def _to_custom_rules(parsed: List[Dict]) -> list:
    """Convert LLM-parsed dicts to CustomRule objects."""
    from validation.verification_models import CustomRule, RuleCondition, RuleScope

    rules = []
    for d in parsed:
        scope_data = d.get("scope", {}) or {}
        cond_data = d.get("condition", {}) or {}

        rule = CustomRule(
            id=d.get("id", "rule-unknown"),
            source_text=d.get("source_text", ""),
            category=d.get("category", "general"),
            trigger=d.get("trigger", "before_step"),
            scope=RuleScope(
                phase_ids=scope_data.get("phase_ids"),
                resource_types=scope_data.get("resource_types"),
                resource_names=scope_data.get("resource_names"),
                scope_types=scope_data.get("scope_types"),
            ),
            condition=RuleCondition(
                type=cond_data.get("type", "custom"),
                check=cond_data.get("check", ""),
                threshold=cond_data.get("threshold"),
                command=cond_data.get("command"),
            ),
            action=d.get("action", "warn"),
            action_message=d.get("action_message", ""),
            check_command=cond_data.get("command"),
            confidence=d.get("confidence", 0.0),
            human_verified=False,
        )
        rules.append(rule)

    return rules


# ------------------------------------------------------------------
# Heuristic inference helpers (for regex fallback)
# ------------------------------------------------------------------

def _infer_trigger(text: str) -> str:
    """Infer rule trigger from NL text."""
    lower = text.lower()
    if "之前" in lower or "before" in lower or "前" in lower:
        if "phase" in lower or "阶段" in lower:
            return "before_phase"
        return "before_step"
    if "之后" in lower or "after" in lower or "后" in lower:
        if "phase" in lower or "阶段" in lower:
            return "after_phase"
        return "after_step"
    if "时段" in lower or "时间" in lower or "window" in lower:
        return "before_step"
    return "before_step"


def _infer_resource_types(text: str) -> Optional[List[str]]:
    """Infer resource types from NL text."""
    types = []
    if "rds" in text.lower() or "数据库" in text:
        types.append("RDSCluster")
    if "dynamodb" in text.lower():
        types.append("DynamoDBTable")
    if "route53" in text.lower() or "dns" in text.lower():
        types.append("Route53Record")
    if "alb" in text.lower() or "负载均衡" in text:
        types.append("ALB")
    if "eks" in text.lower() or "pod" in text.lower() or "k8s" in text.lower():
        types.append("EKSDeployment")
    return types if types else None


def _infer_phases(text: str) -> Optional[List[str]]:
    """Infer phase IDs from NL text."""
    phases = []
    lower = text.lower()
    if "数据" in lower or "数据库" in lower or "data" in lower:
        phases.append("phase-1")
    if "计算" in lower or "compute" in lower or "pod" in lower:
        phases.append("phase-2")
    if "流量" in lower or "dns" in lower or "traffic" in lower:
        phases.append("phase-3")
    if "回滚" in lower or "rollback" in lower:
        phases.append("phase-rollback")
    return phases if phases else None


def _infer_action(text: str) -> str:
    """Infer rule action from NL text."""
    lower = text.lower()
    if "禁止" in lower or "必须" in lower or "不允许" in lower or "block" in lower:
        return "block"
    if "审批" in lower or "approval" in lower:
        return "require_approval"
    if "跳过" in lower or "skip" in lower:
        return "skip"
    return "warn"

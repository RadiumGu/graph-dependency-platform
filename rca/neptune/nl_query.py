"""
nl_query.py - 自然语言 → openCypher 查询引擎。

流程：
  1. 用 Bedrock Claude 将自然语言问题转成 openCypher
  2. 安全校验（拦截写操作、超深遍历）
  3. 执行查询（复用 neptune_client）
  4. 用 Bedrock Claude 生成中文摘要
"""
import json
import logging
import os

import boto3

from neptune import neptune_client as nc
from neptune import query_guard
from neptune.schema_prompt import build_system_prompt

logger = logging.getLogger(__name__)

from shared import get_region
REGION = get_region()
MODEL = os.environ.get('BEDROCK_MODEL', 'global.anthropic.claude-sonnet-4-6')
# Wave 5: 复杂问题升级用的重模型（准确率更高，成本 5-10x）
MODEL_HEAVY = os.environ.get('BEDROCK_MODEL_HEAVY', 'global.anthropic.claude-opus-4-7')


# 空结果 "合理空" 的语义信号词（Wave 4）——命中这些词的问题不触发重试，
# 因为用户原本就在查找不存在/未发生的事情，空结果是正确答案。
_NEGATION_HINTS = (
    "从未", "没有", "未做", "未被", "未出现", "未触发", "未曾", "不曾",
    "不存在", "无", "零次", "不在",
    "never", "without", "not exist", "no ", "none", "zero",
)


class NLQueryEngine:
    """自然语言图查询引擎：NL → openCypher → Neptune → 摘要。"""

    def __init__(self, profile=None) -> None:
        self.bedrock = boto3.client('bedrock-runtime', region_name=REGION)
        if profile is None:
            from profiles.profile_loader import EnvironmentProfile
            profile = EnvironmentProfile()
        self.profile = profile
        self.system_prompt = build_system_prompt(self.profile)

    def query(self, question: str) -> dict:
        """执行自然语言查询，返回包含 cypher、结果和摘要的字典。

        Args:
            question: 自然语言问题（中文或英文）

        Returns:
            {
              "question": str,
              "cypher": str,
              "results": list,
              "summary": str,
              "retried": bool,  # Wave 4: 是否空结果重试过
            }
            或出错时：{"error": str, "cypher": str}
        """
        try:
            cypher = self._generate_cypher(question)
        except Exception as e:
            logger.warning(f"NLQuery cypher generation failed: {e}")
            return {"error": str(e), "cypher": ""}

        safe, reason = query_guard.is_safe(cypher)
        if not safe:
            logger.warning(f"NLQuery blocked unsafe cypher: {reason} | cypher={cypher[:200]}")
            return {"error": reason, "cypher": cypher}

        cypher = query_guard.ensure_limit(cypher)

        try:
            results = nc.results(cypher)
        except Exception as e:
            logger.warning(f"NLQuery execution failed: {e} | cypher={cypher[:200]}")
            return {"error": str(e), "cypher": cypher}

        # Wave 4: 空结果自动重试（跳过“合理空”的否定查询）
        retried = False
        if not results and self._should_retry_on_empty(question):
            retry_cypher = self._retry_with_hint(question, cypher)
            if retry_cypher:
                safe2, reason2 = query_guard.is_safe(retry_cypher)
                if safe2:
                    retry_cypher = query_guard.ensure_limit(retry_cypher)
                    if retry_cypher == cypher:
                        logger.debug("NLQuery retry produced identical cypher; skip exec")
                    else:
                        try:
                            retry_results = nc.results(retry_cypher)
                        except Exception as e:
                            logger.warning(f"NLQuery retry failed: {e} | cypher={retry_cypher[:200]}")
                            retry_results = []
                        if retry_results:
                            logger.info(
                                "NLQuery empty-result retry succeeded: %d rows (was 0)",
                                len(retry_results),
                            )
                            cypher = retry_cypher
                            results = retry_results
                            retried = True
                else:
                    logger.debug(f"NLQuery retry blocked by guard: {reason2}")

        summary = self._summarize(question, results)

        return {
            "question": question,
            "cypher": cypher,
            "results": results,
            "summary": summary,
            "retried": retried,
        }

    def _should_retry_on_empty(self, question: str) -> bool:
        """判断空结果是否值得重试。否定查询（“从未做过”等）不重试。"""
        ql = question.lower()
        return not any(hint in ql for hint in _NEGATION_HINTS)

    def _retry_with_hint(self, question: str, prev_cypher: str) -> str:
        """带关系提示和上次结果重新生成 cypher。

        Returns:
            重试后的 cypher；生成失败返回空字符串。
        """
        relations = getattr(self, "profile", None)
        relations = relations.neptune_common_relations if relations else []
        if not relations:
            return ""
        hint = (
            f"\n\n注意：上次生成的查询返回空结果，请检查关系名是否正确。"
            f"\n常用关系：{', '.join(relations)}"
            f"\n上次生成的查询：{prev_cypher}"
            f"\n请重新生成一个可能返回结果的查询（注意：微服务访问数据库用 AccessesData，不是 DependsOn）。"
        )
        try:
            return self._generate_cypher(question + hint)
        except Exception as e:
            logger.warning(f"NLQuery retry generation failed: {e}")
            return ""

    def _select_model(self, question: str) -> str:
        """根据问题复杂度选模型（Wave 5）。

        命中任何 profile.complex_keywords.zh / .en 关键词 → 用重模型（Opus）。
        否则使用默认模型（Sonnet）。
        """
        profile = getattr(self, "profile", None)
        if profile is None:
            return MODEL
        ck = profile.neptune_complex_keywords or {}
        needles = list(ck.get("zh") or []) + list(ck.get("en") or [])
        if not needles:
            return MODEL
        ql = question.lower()
        for kw in needles:
            kw_l = kw.lower()
            if kw_l and kw_l in ql:
                logger.info(
                    "NLQuery upgrading model to %s (matched complex keyword: %s)",
                    MODEL_HEAVY, kw,
                )
                return MODEL_HEAVY
        return MODEL

    def _generate_cypher(self, question: str) -> str:
        """调用 Bedrock Claude 将自然语言问题转成 openCypher 查询。

        Args:
            question: 自然语言问题

        Returns:
            openCypher 查询字符串（已去除 markdown 代码块包裹）
        """
        model_id = self._select_model(question)
        resp = self.bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "system": self.system_prompt,
                "messages": [{"role": "user", "content": question}],
            }),
        )
        body = json.loads(resp['body'].read())
        text = body['content'][0]['text'].strip()

        # 去除 LLM 可能包裹的 markdown 代码块
        if text.startswith('```'):
            lines = text.split('\n')
            # 去掉首行 ```cypher / ```openCypher 等
            text = '\n'.join(lines[1:])
            if text.rstrip().endswith('```'):
                text = text.rstrip()[:-3].rstrip()

        return text.strip()

    def _summarize(self, question: str, results: list) -> str:
        """调用 Bedrock Claude 对查询结果生成中文摘要。

        Args:
            question: 原始问题
            results: Neptune 查询结果列表

        Returns:
            简洁的中文摘要字符串
        """
        if not results:
            return "查询无结果。"

        results_text = json.dumps(results, ensure_ascii=False, default=str)[:3000]
        resp = self.bedrock.invoke_model(
            modelId=MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 512,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"用户问题: {question}\n"
                        f"查询结果: {results_text}\n\n"
                        "请用简洁中文（2-4句）总结查询结果，直接给出结论："
                    ),
                }],
            }),
        )
        body = json.loads(resp['body'].read())
        return body['content'][0]['text'].strip()

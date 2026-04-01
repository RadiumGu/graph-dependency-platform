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

REGION = os.environ.get('REGION', 'ap-northeast-1')
MODEL = os.environ.get('BEDROCK_MODEL', 'global.anthropic.claude-sonnet-4-6')


class NLQueryEngine:
    """自然语言图查询引擎：NL → openCypher → Neptune → 摘要。"""

    def __init__(self) -> None:
        self.bedrock = boto3.client('bedrock-runtime', region_name=REGION)
        self.system_prompt = build_system_prompt()

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

        summary = self._summarize(question, results)

        return {
            "question": question,
            "cypher": cypher,
            "results": results,
            "summary": summary,
        }

    def _generate_cypher(self, question: str) -> str:
        """调用 Bedrock Claude 将自然语言问题转成 openCypher 查询。

        Args:
            question: 自然语言问题

        Returns:
            openCypher 查询字符串（已去除 markdown 代码块包裹）
        """
        resp = self.bedrock.invoke_model(
            modelId=MODEL,
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

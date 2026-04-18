"""
schema_prompt.py - Neptune 图 schema + few-shot 示例 + system prompt 构建。

Wave 1 (2026-04-18): 内容从 `profiles/<env>.yaml` 的 `neptune` 段读取，
而不是硬编码。保留模块级常量 GRAPH_SCHEMA 和 FEW_SHOT_EXAMPLES 作为
向后兼容（import 时从默认 profile 加载）。

切换环境：设置 PROFILE_NAME 后重启 Streamlit，或直接传 profile 给
`build_system_prompt(profile=...)`。
"""
from __future__ import annotations

from typing import Any


def _load_default_profile() -> Any:
    from profiles.profile_loader import EnvironmentProfile
    return EnvironmentProfile()


# Import-time load: 向后兼容 `from schema_prompt import GRAPH_SCHEMA / FEW_SHOT_EXAMPLES`。
# profile 切换需重启进程（与改造前一致）。
_DEFAULT_PROFILE = _load_default_profile()
GRAPH_SCHEMA: str = _DEFAULT_PROFILE.neptune_graph_schema_text
FEW_SHOT_EXAMPLES: list[dict] = list(_DEFAULT_PROFILE.neptune_few_shot_examples)


def build_system_prompt(profile: Any = None) -> str:
    """构建 NL→openCypher 的 system prompt。

    Args:
        profile: EnvironmentProfile 实例；None 时用默认 profile（petsite）

    Returns:
        包含 schema、规则和 few-shot 示例的完整 system prompt 字符串
    """
    if profile is None:
        profile = _DEFAULT_PROFILE

    schema_text = profile.neptune_graph_schema_text
    examples = "\n".join(
        f"Q: {ex['q']}\nCypher: {ex['cypher']}"
        for ex in profile.neptune_few_shot_examples
    )

    return f"""你是一个 Amazon Neptune openCypher 查询生成器。根据用户的自然语言问题，生成正确的 openCypher 查询。

{schema_text}

## 规则
1. 只生成 READ 查询（MATCH/OPTIONAL MATCH/RETURN/WHERE/ORDER BY/LIMIT），绝对禁止 CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL
2. 参数化查询用 $param 语法
3. RETURN 必须带有意义的别名（AS ...）
4. 多跳遍历限制 *1..5，避免路径爆炸
5. 不确定的查询加 LIMIT 50 兜底
6. 只输出 openCypher 查询语句，不要解释，不要加 markdown 代码块
7. ⚠️ 微服务访问数据库（RDS、DynamoDB、S3）用 AccessesData 关系，不是 DependsOn
8. ⚠️ DependsOn 仅用于 ECR 镜像依赖和 SQS 队列依赖

## 示例
{examples}

现在根据用户问题生成查询："""

"""
query_guard.py - openCypher 查询安全校验。

职责：
  - 阻断所有写操作关键字（CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL）
  - 限制多跳遍历深度（最大 6 跳）
  - 确保查询有 LIMIT 子句防止结果集过大
"""
import re

_WRITE_KEYWORDS = re.compile(
    r'\b(CREATE|DELETE|DETACH|SET|MERGE|REMOVE|DROP|CALL)\b',
    re.IGNORECASE,
)

_HOP_PATTERN = re.compile(r'\*\d+\.\.(\d+)')

MAX_RESULT_LIMIT = 200
MAX_HOP_DEPTH = 6


def is_safe(cypher: str) -> tuple[bool, str]:
    """校验生成的 openCypher 查询是否安全。

    Args:
        cypher: 待检查的 openCypher 查询字符串

    Returns:
        (True, "OK") 表示安全；(False, reason) 表示不安全及原因
    """
    if _WRITE_KEYWORDS.search(cypher):
        match = _WRITE_KEYWORDS.search(cypher)
        keyword = match.group(0).upper() if match else '未知'
        return False, f"查询包含写操作关键字: {keyword}"

    for hop_str in _HOP_PATTERN.findall(cypher):
        if int(hop_str) > MAX_HOP_DEPTH:
            return False, f"遍历深度 {hop_str} 超过限制（最大 {MAX_HOP_DEPTH}）"

    return True, "OK"


def ensure_limit(cypher: str) -> str:
    """确保查询包含 LIMIT 子句，不存在时自动追加。

    Args:
        cypher: openCypher 查询字符串

    Returns:
        带有 LIMIT 子句的查询字符串
    """
    if 'LIMIT' not in cypher.upper():
        cypher = cypher.rstrip().rstrip(';') + f' LIMIT {MAX_RESULT_LIMIT}'
    return cypher

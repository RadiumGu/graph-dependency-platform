"""
test_01_security.py — NL 查询引擎安全校验（纯函数测试）

Tests: S-01 ~ S-05, U-B2-05 ~ U-B2-13
"""
import pytest


def test_s01_reject_delete():
    """S-01: 拦截 DELETE 指令。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) DELETE n")
    assert not safe
    assert "DELETE" in reason.upper() or "写操作" in reason


def test_s02_reject_set():
    """S-02: 拦截 SET 修改指令。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) SET n.pwned = true")
    assert not safe


def test_s03_reject_create():
    """S-03 / U-B2-05: 拦截 CREATE 语句。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("CREATE (n:Test {name: 'hack'})")
    assert not safe


def test_s04_reject_merge():
    """S-04 / U-B2-08: 拦截 MERGE 语句（写操作场景）。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) MERGE (n)-[:BAD]->(m)")
    assert not safe


def test_s05_auto_limit():
    """S-05 / U-B2-12: 无 LIMIT 的查询自动补 LIMIT 200。"""
    from neptune.query_guard import ensure_limit
    result = ensure_limit("MATCH (n) RETURN n")
    assert "LIMIT" in result.upper()
    assert "200" in result


def test_s05_existing_limit_unchanged():
    """U-B2-13: 已有 LIMIT 的查询不重复添加。"""
    from neptune.query_guard import ensure_limit
    original = "MATCH (n) RETURN n LIMIT 10"
    result = ensure_limit(original)
    assert result == original


def test_ub2_06_reject_delete_inline():
    """U-B2-06: 拦截 DELETE 语句（行内）。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) DELETE n")
    assert not safe


def test_ub2_07_reject_set():
    """U-B2-07: 拦截 SET 语句。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) SET n.pwned = true")
    assert not safe


def test_ub2_09_allow_match():
    """U-B2-09: 合法 MATCH 查询放行。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n) RETURN n")
    assert safe
    assert reason == "OK"


def test_ub2_10_reject_deep_traversal():
    """U-B2-10: 拦截超深遍历（>6 跳）。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n)-[:Calls*1..10]->(m) RETURN m")
    assert not safe
    assert "10" in reason


def test_ub2_11_allow_reasonable_depth():
    """U-B2-11: 放行合理遍历深度（<=6 跳）。"""
    from neptune.query_guard import is_safe
    safe, reason = is_safe("MATCH (n)-[:Calls*1..5]->(m) RETURN m")
    assert safe
    assert reason == "OK"


def test_reject_detach_delete():
    """额外：拦截 DETACH DELETE。"""
    from neptune.query_guard import is_safe
    safe, _ = is_safe("MATCH (n) DETACH DELETE n")
    assert not safe


def test_reject_remove():
    """额外：拦截 REMOVE。"""
    from neptune.query_guard import is_safe
    safe, _ = is_safe("MATCH (n) REMOVE n.name")
    assert not safe


def test_reject_drop():
    """额外：拦截 DROP。"""
    from neptune.query_guard import is_safe
    safe, _ = is_safe("DROP INDEX idx_name")
    assert not safe


def test_reject_call():
    """额外：拦截 CALL（存储过程调用）。"""
    from neptune.query_guard import is_safe
    safe, _ = is_safe("CALL db.schema()")
    assert not safe

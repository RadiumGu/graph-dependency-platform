"""
test_00_regression.py — 回归测试，确保 Phase A/B 改动不破坏现有功能

Tests: R-01 ~ R-07
"""
import os
import sys

import pytest

PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'


def test_r01_rca_core_imports():
    """R-01: rca 核心模块正常导入（无 ImportError）。"""
    from core import rca_engine, fault_classifier
    from actions import playbook_engine
    assert rca_engine is not None
    assert fault_classifier is not None
    assert playbook_engine is not None


def test_r02_dr_plan_imports():
    """R-02: dr-plan-generator 核心模块正常导入。"""
    from graph.graph_analyzer import GraphAnalyzer
    from validation.plan_validator import PlanValidator
    analyzer = GraphAnalyzer()
    assert analyzer is not None
    validator = PlanValidator()
    assert validator is not None


def test_r03_q1_blast_radius(neptune_rca):
    """R-03a: Q1 blast_radius 查询不抛异常，返回 dict。"""
    from neptune import neptune_queries as nq
    result = nq.q1_blast_radius('petsite')
    assert isinstance(result, dict)
    assert 'services' in result
    assert 'capabilities' in result


def test_r03_q2_tier0_status(neptune_rca):
    """R-03b: Q2 tier0_status 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q2_tier0_status()
    assert isinstance(result, list)


def test_r03_q3_upstream_deps(neptune_rca):
    """R-03c: Q3 upstream_deps 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q3_upstream_deps('petsite')
    assert isinstance(result, list)


def test_r03_q4_service_info(neptune_rca):
    """R-03d: Q4 service_info 查询正常返回 dict 或空 dict。"""
    from neptune import neptune_queries as nq
    result = nq.q4_service_info('petsite')
    assert isinstance(result, dict)


def test_r03_q5_similar_incidents(neptune_rca):
    """R-03e: Q5 similar_incidents 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q5_similar_incidents('petsite')
    assert isinstance(result, list)


def test_r03_q6_pod_status(neptune_rca):
    """R-03f: Q6 pod_status 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q6_pod_status('petsite')
    assert isinstance(result, list)


def test_r03_q7_db_connections(neptune_rca):
    """R-03g: Q7 db_connections 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q7_db_connections('petsite')
    assert isinstance(result, list)


def test_r03_q8_log_source(neptune_rca):
    """R-03h: Q8 log_source 查询正常返回 str。"""
    from neptune import neptune_queries as nq
    result = nq.q8_log_source('petsite')
    assert isinstance(result, str)


def test_r03_q9_service_infra_path(neptune_rca):
    """R-03i: Q9 service_infra_path 查询正常返回 list。"""
    from neptune import neptune_queries as nq
    result = nq.q9_service_infra_path('petsite')
    assert isinstance(result, list)


def test_r03_q10_infra_root_cause(neptune_rca):
    """R-03j: Q10 infra_root_cause 查询正常返回 dict。"""
    from neptune import neptune_queries as nq
    result = nq.q10_infra_root_cause('petsite')
    assert isinstance(result, dict)
    assert 'has_infra_fault' in result


def test_r03_q11_broader_impact(neptune_rca):
    """R-03k: Q11 broader_impact 空输入返回 []。"""
    from neptune import neptune_queries as nq
    result = nq.q11_broader_impact([])
    assert result == []


def test_r03_q12_az_dependency_tree(neptune_rca):
    """R-03l: DR Q12 az_dependency_tree 不抛异常。"""
    from graph.queries import q12_az_dependency_tree
    result = q12_az_dependency_tree('apne1-az1')
    assert isinstance(result, list)


def test_r03_q17_exists(neptune_rca):
    """R-03m: Q17 incidents_by_resource 正常返回 list（Phase A 新增）。"""
    from neptune import neptune_queries as nq
    result = nq.q17_incidents_by_resource('petsite')
    assert isinstance(result, list)


def test_r03_q18_exists(neptune_rca):
    """R-03n: Q18 chaos_history 正常返回 list（Phase A 新增）。"""
    from neptune import neptune_queries as nq
    result = nq.q18_chaos_history('petsite')
    assert isinstance(result, list)


def test_r05_rca_handler_import():
    """R-05: rca handler.py Lambda 入口正常导入，无 ImportError。"""
    import handler  # rca/handler.py (rca/ is on sys.path)
    assert handler is not None


def test_r06_chaos_main_import():
    """R-06: chaos main.py 正常导入。"""
    chaos_code_path = os.path.join(PROJECT_ROOT, 'chaos', 'code')
    if chaos_code_path not in sys.path:
        sys.path.insert(0, chaos_code_path)
    import main as chaos_main
    assert chaos_main is not None


def test_r07_dr_main_import():
    """R-07: dr-plan main.py 正常导入。"""
    dr_path = os.path.join(PROJECT_ROOT, 'dr-plan-generator')
    if dr_path not in sys.path:
        sys.path.insert(0, dr_path)
    import main as dr_main
    assert dr_main is not None

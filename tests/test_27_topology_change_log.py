"""
test_27_topology_change_log.py — 拓扑变更日志（T-030）

覆盖测试清单:C-01 ~ C-05

## 这个机制解决什么

「上周拓扑长什么样」此前完全无法回答:所有写入都是就地覆盖
(`property(single,...)` / `mergeV` onMatch),无版本标签、时间分区、快照导出。

## 为什么选追加式事件日志而不是双时态边

三个方案的取舍(完整记录见 tasks.md 的 T-030):

  A 定期全量快照到 S3    只答「T 时刻全貌」,答不了「变了什么」
  B 双时态边 valid_from/valid_to
      **否决** —— 要改 6 个写入方,且现有 19 条查询会静默返回被取代的旧版本,
      除非每条都加时间过滤。回归面太大;图规模按「边 × 变更频率」无界增长。
  C 追加式变更事件日志
      **采用** —— 纯追加,现有查询不动,增长与**变更次数**成正比而非边数×时间。

## 为什么图谱侧的变更日志不可被 CloudTrail 替代

CloudTrail 记录 AWS API 级变更(部署、实例停止、RDS 修改、扩缩容),
按构造**看不见**两类对依赖图谱因果上最相关的变化:

  · 依赖消失 —— A 不再调用 B。这不产生任何 AWS API 调用,是「流量缺席」
  · 依赖出现 —— 应用内配置/开关导致 A 开始调 B

## 两条自定的硬约束(本文件即其验收)

1. **必须有消费方**。cycle-1 查出的核心缺陷就是 `active`/`last_seen` 写了
   但全仓无人读。加机制不接消费方是重复同一个错误 → C-04 断言 Q19 存在且
   已注册进 MCP;C-05 断言 RCA 提示词真的包含该段。
2. **必须有保留期**。否决方案 B 的理由之一是无界增长,自建日志不能无界
   → C-03 断言超期事件被清理。
"""
import os
import sys
import time

import pytest

from paths import PROJECT_ROOT


def _load_etl():
    """加载 etl_deepflow 模块（需要 shared layer 的 neptune_client_base）。"""
    shared = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'shared', 'python')
    etl_dir = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow')
    for p in (shared, etl_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    import neptune_etl_deepflow as m
    return importlib.reload(m)


def _code_only(fn_body: str) -> str:
    """剥掉 docstring 与行注释，只留可执行代码。

    2026-08-28:本文件初版的 C-02 直接对整个函数体做子串检查，结果把
    docstring 里「刻意用 mergeV 而非 addV」这句**散文**当成代码，误报失败。
    这是同一类错误的第三次（前两次:用裸 grep 数装饰器、用正则扫映射表时
    把注释掉的条目算成生效）。凡是对源码做文本断言，都必须先剥注释。
    """
    lines = []
    in_doc = False
    doc_q = None
    for raw in fn_body.split('\n'):
        s = raw.strip()
        if in_doc:
            if doc_q in s:
                in_doc = False
            continue
        if s.startswith('"""') or s.startswith("'''"):
            doc_q = s[:3]
            # 单行 docstring
            if not (len(s) > 3 and s.endswith(doc_q)):
                in_doc = True
            continue
        if s.startswith('#'):
            continue
        # 去掉行尾注释（本项目不在字符串里放 # ，够用）
        if '#' in raw and '"' not in raw.split('#')[0] and "'" not in raw.split('#')[0]:
            raw = raw.split('#')[0]
        lines.append(raw)
    return '\n'.join(lines)


def _fn_source(path: str, start_marker: str, end_marker: str) -> str:
    with open(path, encoding='utf-8') as f:
        body = f.read()
    return body[body.index(start_marker):body.index(end_marker)]


# ── C-01: 事件量有界（只在状态转变时产生） ──────────────────────────────────


def test_c01_reconcile_filters_on_active_true():
    """C-01: 对账过滤条件必须含 active=true,保证只在 true→false 转变时发事件。

    若过滤掉了这一条,稳态失效边会**每轮** ETL 都重新产生一条事件 ——
    日志量将与 ETL 轮次成正比而非与变更次数成正比,这正是否决双时态方案时
    批评的无界增长。
    """
    src = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow',
                       'neptune_etl_deepflow.py')
    fn = _code_only(_fn_source(src, 'def reconcile_calls_edges', 'def _extract_pairs'))
    assert "has('active', true)" in fn, (
        "reconcile_calls_edges 必须用 .has('active', true) 过滤，"
        "否则稳态失效边会每轮重复产生变更事件（事件量失去上界）"
    )


# ── C-02: 幂等（同一轮重试不产生重复事件） ──────────────────────────────────


def test_c02_emit_is_idempotent_via_mergev():
    """C-02: 事件写入必须用 mergeV 而非 addV。

    ETL 可能因重试被同一轮触发两次（SQS / EventBridge 均为 at-least-once）,
    裸 addV 会留下重复事件,而变更日志一旦重复就没法用来做时序推断。
    """
    src = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow',
                       'neptune_etl_deepflow.py')
    fn = _code_only(_fn_source(src, 'def _emit_topology_changes',
                               'def _prune_change_log'))
    assert 'mergeV' in fn, "_emit_topology_changes 必须用 mergeV 保证幂等"
    assert 'addV' not in fn, "_emit_topology_changes 不应使用 addV（会产生重复事件）"
    assert 'change_id' in fn, "必须有幂等键 change_id"


# ── C-03: 保留期（追加式日志必须有上限） ────────────────────────────────────


def test_c03_retention_configured_and_wired():
    """C-03: 必须有保留期且在对账里被调用。

    否决双时态方案的理由之一是无界增长 —— 自建的追加日志不能重复这个问题。
    """
    etl = _load_etl()
    assert hasattr(etl, 'CHANGE_LOG_RETENTION_SECONDS')
    assert etl.CHANGE_LOG_RETENTION_SECONDS > 0
    assert hasattr(etl, '_prune_change_log')

    src = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow',
                       'neptune_etl_deepflow.py')
    fn = _code_only(_fn_source(src, 'def reconcile_calls_edges', 'def _extract_pairs'))
    assert '_prune_change_log' in fn, (
        "保留期清理必须在 reconcile_calls_edges 里被调用，"
        "否则日志只增不减"
    )


# ── C-04: 有消费方 —— 查询存在且已注册进 MCP ────────────────────────────────


def test_c04_query_exists_and_registered_in_mcp():
    """C-04: Q19 必须存在,且已注册进 MCP 端点。

    cycle-1 查出的核心缺陷就是 `active` / `last_seen` 写了但全仓无人读 ——
    加机制不接消费方是重复同一个错误。
    """
    from neptune import neptune_queries as nq
    assert hasattr(nq, 'q19_topology_changes'), "缺少 Q19 查询"

    import importlib.util
    mcp_path = os.path.join(PROJECT_ROOT, 'rca', 'neptune', 'graph_mcp_server.py')
    spec = importlib.util.spec_from_file_location('_mcp_probe', mcp_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert 'q19_topology_changes' in mod.QUERY_REGISTRY, (
        "Q19 未注册进 MCP 的 QUERY_REGISTRY —— 外部 agent 无法调用"
    )


def test_c05_rca_prompt_includes_topology_changes():
    """C-05: RCA 提示词必须包含拓扑变更段。

    这是「必须有消费方」这条约束的最终验收:事件写进去、能查出来,
    但如果不进提示词,对 RCA 推理就毫无影响。
    """
    src = os.path.join(PROJECT_ROOT, 'rca', 'core', 'graph_rag_reporter.py')
    with open(src, encoding='utf-8') as f:
        body = f.read()
    assert 'q19_topology_changes' in body, "graph_rag_reporter 未调用 Q19"
    assert 'tc_text' in body, "未构建拓扑变更文本段"
    # 必须真的插进 prompt，而不是只算不用
    prompt_start = body.index('你是一位资深 SRE')
    prompt_end = body.index('请直接输出以下 JSON 格式')
    assert '{tc_text}' in body[prompt_start:prompt_end], (
        "tc_text 已构建但未插入 prompt —— 与「写了没人读」是同一类缺陷"
    )


# ── C-06: schema 已声明（否则 T-024 一致性测试会失败） ──────────────────────


def test_c06_label_declared_in_schema():
    """C-06: TopologyChange 必须在 profile 的 schema 里声明。

    tests/test_24_live_schema_consistency.py 断言活图标签 ⊆ schema 声明,
    新增标签不声明会让那个测试失败 —— 那正是它该起的作用。
    schema_prompt 只把 YAML 喂给 LLM,未声明的类型对自然语言查询是隐形的。
    """
    p = os.path.join(PROJECT_ROOT, 'profiles', 'petsite.yaml')
    with open(p, encoding='utf-8') as f:
        text = f.read()
    assert 'TopologyChange' in text, "profile 未声明 TopologyChange 标签"
    assert 'dependency_deactivated' in text, "未在 schema 中说明 kind 的取值"

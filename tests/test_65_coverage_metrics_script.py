"""test_65_coverage_metrics_script.py — 覆盖率指标脚本的判据必须与运行时同源。

## 这个脚本在做什么

`scripts/emit_graph_coverage_metrics.py` 每 6 小时把两组数发到 CloudWatch
命名空间 `GraphDependency/Coverage`：

  依赖图验证覆盖   边总数 / 四态分布 / verified_ratio / **判伪通道可达的边数**
  agent skill 守卫  资产还在吗 / 状态 ACTIVE 吗 / 内容哈希与仓库一致吗

## 为什么需要它

本会话里依赖边总数被观察到 119 → 113 → 97 → 115，验证覆盖率
10.92% → 11.5% → 13.4%。方向是对的（并发会话在清假边），但事后无法回答
「什么时候变的、变了多少」：

  · `q19_topology_changes` 只记录 **deepflow 观测到的**依赖出现/消失
    （`active` true→false 的状态转变）。实测最近 24h **0 条事件**，
    而边数在同一天里从 97 变到 115 —— 它按构造看不见 aws-etl / cfn-etl /
    清理作业增删的边。
  · 图里**没有任何聚合量的历史**（`GraphSnapshot` 等节点类型实查都是 0）。

## 为什么发 CloudWatch 而不是往图里写快照节点

依赖图应该建模**被观测的系统**，不该建模**它自己的指标**。往图里塞
`:CoverageSnapshot` 会让「节点类型数」这类契约数字把运维遥测也算进去，
而那些数字正是首页用来和契约对账的。

## skill 守卫为什么用哈希而不是重跑采样

skill 注册后自发查询率从 25% 升到 100%。要监测退化，直觉做法是定期重跑那
8 次采样 —— 但一次 45~60 秒、烧 Bedrock token，而 skill 生效时结果恒为 100%，
信噪比极低。最可能的回归是**有人删了或改了那个 skill**，那用三次只读调用
就能查出来。哈希变了再跑全量采样，那时候它才有信息量。
"""
import re
from pathlib import Path

from paths import PROJECT_ROOT

SCRIPT = Path(PROJECT_ROOT) / 'scripts' / 'emit_graph_coverage_metrics.py'
RUNNER = (Path(PROJECT_ROOT) / 'chaos' / 'code' / 'runner'
          / 'edge_verification.py')
SKILL = (Path(PROJECT_ROOT) / 'mcp' / 'agent_skill'
         / 'dependency-verification-graph.md')


def test_m01_脚本存在且可编译():
    import ast
    assert SCRIPT.exists(), f'缺少 {SCRIPT}'
    ast.parse(SCRIPT.read_text(encoding='utf-8'))


def test_m02_观测标记必须与运行时同源():
    """少一个标记，「判伪通道可达的边数」就会偏大 —— 静默地。"""
    src = SCRIPT.read_text(encoding='utf-8')
    runner = RUNNER.read_text(encoding='utf-8')
    m = re.search(r'_OBSERVER_MARKERS\s*=\s*\{([\s\S]*?)\n\}', runner)
    assert m, '找不到运行时的 _OBSERVER_MARKERS'
    props = set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))
    props -= {'xray', 'nfm', 'deepflow', 'k8s-image-spec'}   # 分组名不是属性名
    missing = sorted(p for p in props if p not in src)
    assert not missing, (
        f'脚本的零观测判据缺少 {missing}。它必须与 '
        '`chaos/code/runner/edge_verification.py` 的 `_OBSERVER_MARKERS` 同源 —— '
        '少一个标记会把有观测的边算成可判伪，发到 CloudWatch 的'
        '「判伪通道可达边数」就偏大，而且没有任何报错。')


def test_m02b_被阻断的边不得再被算成untested():
    """四类必须互斥分桶，且 blocked 的判定要**优先于** untested。

    2026-09-09 之前脚本用 `coalesce(r.verify_status,'untested')` 一刀切，
    于是 13 条「用任何后端都打不到」的 AgentCore 边被算进 untested ——
    读数的人看到「78+13 条待覆盖」，以为再跑几轮战役就能推进，
    实际上那 13 条永远轮不到。这是**错误的努力方向**，
    而且没有任何报错。

    这条门禁钉住两件事：
      1. 分桶查询里 blocked 分支必须在 untested 之前
      2. 必须同时发 addressable 分母 —— 否则比率的分母里
         永久掺着不可能项
    """
    src = SCRIPT.read_text(encoding='utf-8')
    code = _code_only(src)

    assert 'verify_blocked_reason' in code, (
        '覆盖率脚本没有读 verify_blocked_reason —— '
        '被阻断的边会重新混进 untested。')

    i_blocked = code.find("'blocked_by_backend'")
    i_untested = code.find("ELSE 'untested'")
    assert i_blocked != -1 and i_untested != -1, (
        f'找不到四类分桶的 CASE 分支（blocked={i_blocked}, untested={i_untested}）')
    assert i_blocked < i_untested, (
        'CASE 里 untested 分支排在 blocked 之前 —— '
        'untested 是 ELSE 兜底，排在前面会把被阻断的边吞掉。')

    for m in ('blocked_by_backend', 'addressable_edges',
              'verified_ratio_addressable_pct'):
        assert m in code, (
            f'缺指标 {m}。四类分开报的意义在于让「进度」与「能力天花板」'
            f'各自可见：untested 会随战役下降，blocked 只会因获得新注入能力而下降。')


def test_m02c_既有比率的分母不得被悄悄换掉():
    """`verified_ratio_pct` 的分母必须仍是 total，不得改成 addressable。

    CloudWatch 是**时序**数据。换分母不会报错，但会让改动前后的同一条曲线
    不可比 —— 而这条曲线正是用来看战役进度的。要按可达分母看，
    应该并排加一条新指标（verified_ratio_addressable_pct），而不是改旧的。
    """
    src = SCRIPT.read_text(encoding='utf-8')
    m = re.search(r"'verified_ratio_pct':\s*(.+)", src)
    assert m, "找不到 verified_ratio_pct 的算式"
    expr = m.group(1)
    assert 'total' in expr and 'addressable' not in expr, (
        f"verified_ratio_pct 的分母被换成了非 total: {expr.strip()}\n"
        f"这会让 CloudWatch 上改动前后的历史数据不可比，且不会报错。"
        f"按可达分母看请用 verified_ratio_addressable_pct。")


def _code_only(text: str) -> str:
    """剥 docstring 与注释。

    第一版 m03 直接在全文里找 `CoverageSnapshot`，结果匹配到本文件与脚本
    docstring 里那段**解释为什么不要写快照节点**的文字。
    本会话第 11 次「判据对准了字符串而不是语法位置」。
    """
    text = re.sub(r'"""[\s\S]*?"""', '', text)
    out = []
    for ln in text.split('\n'):
        if ln.count('"') % 2 == 1 or ln.count("'") % 2 == 1:
            out.append(ln)
        else:
            out.append(ln.split('#', 1)[0])
    return '\n'.join(out)


def test_m03_不得往图里写快照节点():
    """依赖图建模被观测的系统，不建模它自己的指标。"""
    code = _code_only(SCRIPT.read_text(encoding='utf-8'))
    for bad in ('CREATE (', 'MERGE (', 'CoverageSnapshot', 'GraphSnapshot'):
        assert bad not in code, (
            f'脚本代码里出现 `{bad}` —— 覆盖率是运维遥测，不该进依赖图。'
            '往图里塞快照节点会让「节点类型数」这类契约数字把遥测也算进去，'
            '而那些数字正是首页用来和契约对账的。')


def test_m04_必须守卫agent_skill资产():
    """25% → 100% 那个成果没有守卫就会静默退化。"""
    src = SCRIPT.read_text(encoding='utf-8')
    assert SKILL.exists(), f'缺少 skill 源文件 {SKILL}'
    assert 'skill_present' in src and 'skill_matches_repo' in src, (
        '脚本没有守卫 agent skill 资产。skill 被删或被改，自发查询率会掉回 '
        '25% 基线，而这件事没有任何其他信号 —— 图谱照常、页面照常。')
    assert 'sha256' in src, (
        '没有做内容哈希比对。只查「资产存在」挡不住「有人在控制台改了它」。')
    assert 'hashlib' in src, '没有导入 hashlib'


def test_m05_低覆盖率不得当成故障():
    """天天报警只会让人把告警关掉。"""
    src = SCRIPT.read_text(encoding='utf-8')
    assert '低覆盖是现状不是故障' in src, (
        '脚本没有说明为什么不因覆盖率低而退非零。'
        '当前 57/62 条可判伪边未测是**现状**，把它当故障报，'
        '结果是这个告警被永久静音，连带真问题也看不见了。')
    # 退出码只应由 skill 守卫与「判伪通道消失」触发
    m = re.search(r'bad\s*=\s*\(([\s\S]*?)\)\n', src)
    assert m, '找不到退出码判定'
    cond = m.group(1)
    assert 'verified_ratio' not in cond, (
        '退出码判定里包含了 verified_ratio —— 覆盖率低是现状，不是故障。')
    assert 'refutable_edges' in cond, (
        '退出码判定里应包含 refutable_edges == 0：'
        '判伪通道消失意味着「这张图能推翻自己」失去可证伪性，那才是真故障。')

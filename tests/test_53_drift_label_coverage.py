"""tests/test_53_drift_label_coverage.py — 依赖边清单不得脱离契约

## 这条测试要防什么

「依赖边有哪些」这个判据在仓库里有多处副本。契约提供了单一入口
`graph_contract.dependency_edge_labels()`，但有些位置读不到契约（Lambda 部署
副本的父目录里没有 `profiles/`）或需要字面量（Gremlin 字符串、prompt 文本），
所以**兜底清单本身是正确设计** —— 错的是兜底与契约脱钩且无人看守。

2026-09-08 实测到两处已经漂移的兜底，各自的后果都不是「少一点覆盖」：

### 一、etl_deepflow 的 drift 对账清单 → 产生假的监管信号

原本手写成 `('AccessesData', 'PublishesTo', 'InvokesVia', 'ConsumesFrom')`：

  · `ConsumesFrom` 在契约与图谱里**都不存在**（0 条）。查一个不存在的标签，
    Gremlin 不报错只返回空，所以这一项对账从来没生效过。
  · `InvokesVia` 只有一条没有写入方的孤儿边。
  · 漏掉 8 种依赖边，其中 `DependsOn` 正是 etl_xray 表示
    `Microservice → SQSQueue` 用的标签。

后果是**误报**而非漏报：判 `has_declared` 时看不见 `DependsOn`，于是
「声明存在且运行时已观测到」被判成 `declared_not_observed`。
实测 9 条 `declared_not_observed` 里 2 条是这样的假告警：

    petsite -> SQS           PublishesTo 判「没观测到」，而 DependsOn(xray) 就在旁边
    petsite -> StepFunction  InvokesVia  判「没观测到」，而 AccessesData(xray) 就在旁边

`declared_not_observed` 是本平台相对合同型登记册的差异化所在
（「你申报了但我们观测不到」这类审计发现）。带 22% 误报比没有它更糟。

### 二、dr-plan-generator 的 ordering 清单 → 恢复顺序算错

注释写「契约里 dependency: true 的 6 种」、实际列 6 项，而契约已是 10 种。
恢复顺序按依赖边拓扑排，漏 `Invokes`（16 条）/ `PublishesTo`（2 条）
就会把该先恢复的排到后面。

## 为什么用静态扫描而不是运行时断言

漏写的代码路径压根不会调用任何校验函数 —— 运行时断言对「忘了同步」这件事
结构性地无能为力。这与 test_51::m01 用静态扫描抓漏写 source 同理。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / 'infra' / 'lambda' / 'shared' / 'python'))


@pytest.fixture(scope='module')
def dep_labels() -> frozenset:
    """契约声明的依赖边集合 —— 本文件唯一的真值来源。"""
    from graph_contract import dependency_edge_labels  # type: ignore
    labels = dependency_edge_labels()
    assert labels, '契约里应有 dependency 边；取不到说明测试自身失效'
    return frozenset(labels)


def _tuple_literal_after(src: str, name: str) -> set:
    """取形如 `NAME = ( 'a', 'b', ... )` 的字面量元素集合。"""
    m = re.search(rf'^{re.escape(name)}\s*=\s*\((.*?)\)', src, re.S | re.M)
    assert m, f'没找到 {name} 的元组字面量'
    return set(re.findall(r'''['"]([A-Za-z]+)['"]''', m.group(1)))


def test_m01_deepflow_drift_兜底清单必须等于契约依赖边集合(dep_labels):
    """etl_deepflow 的 drift 对账兜底清单不得与契约漂移。"""
    p = _ROOT / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py'
    src = p.read_text(encoding='utf-8')
    fallback = _tuple_literal_after(src, '_DRIFT_LABELS_FALLBACK')
    assert fallback == dep_labels, (
        f'drift 兜底清单与契约不一致。\n'
        f'契约有兜底没有: {sorted(dep_labels - fallback)}\n'
        f'兜底有契约没有: {sorted(fallback - dep_labels)}\n'
        f'漏掉的标签会让 has_declared 判错，把「已声明且已观测」误报成 '
        f'declared_not_observed。')


def test_m02_deepflow_drift_查询不得硬编码标签(dep_labels):
    """drift 查询必须走 `_drift_edge_labels()`，不得内联标签字面量。

    这条防的是「加了函数但调用点没改」——那种改动看起来完成了，
    实际一行都没生效。
    """
    p = _ROOT / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py'
    src = p.read_text(encoding='utf-8')
    # 只看 has_declared 那段查询：它以 hasLabel('Microservice','LambdaFunction') 起头
    m = re.search(r"hasLabel\('Microservice','LambdaFunction'\)\.has\('name'.*?\.outE\(([^)]*)\)",
                  src, re.S)
    assert m, '没找到 drift 的 has_declared 查询'
    outE_arg = m.group(1)
    assert '_dl' in outE_arg or '_drift_edge_labels' in outE_arg, (
        f"drift 查询的 outE() 仍在内联标签字面量：{outE_arg[:120]}\n"
        f'应改为由 _drift_edge_labels() 生成。')


def test_m03_不得再引用不存在的边类型(dep_labels):
    """`ConsumesFrom` 这类幽灵标签不得出现在任何 ETL 的**边**查询里。

    幽灵标签的危害在于**静默**：Gremlin 查一个不存在的标签不报错、只返回空，
    所以带着它的对账逻辑会长期看起来在工作。

    ## 判据只扫边位置，不扫 hasLabel

    第一版把 `hasLabel(...)` 也算进来，结果报出 30+ 处「引用了 Microservice /
    Region / LambdaFunction」—— 全是**节点**类型。`hasLabel()` 在 Gremlin 里
    同时用于顶点和边，光看字面量无法判断当前遍历位置是点还是边，
    所以不能一视同仁。只有 `outE()` / `inE()` / `bothE()` 的实参可以确定是边标签。

    `hasLabel` 上的边标签因此扫不到 —— 那是本判据已知的覆盖边界，
    宁可漏报也不要制造假阳性：假阳性会训练人忽略告警，比没有测试更糟
    （与 test_51::m01 第一版把 9 处合规调用报成违规是同一教训）。
    """
    from graph_contract import EDGE_TYPES  # type: ignore
    declared = set(EDGE_TYPES)
    etl_dir = _ROOT / 'infra' / 'lambda'
    offenders = []
    for p in etl_dir.rglob('*.py'):
        if 'shared' in p.parts:          # 契约模块本身会列出全部标签
            continue
        src = p.read_text(encoding='utf-8', errors='replace')
        # 只取 outE/inE/bothE —— 它们的实参一定是边标签
        for m in re.finditer(r"\.(?:outE|inE|bothE)\(([^)]*)\)", src):
            for lb in re.findall(r"'([A-Z][A-Za-z]+)'", m.group(1)):
                if lb not in declared:
                    line = src[:m.start()].count('\n') + 1
                    offenders.append(f'{p.relative_to(_ROOT)}:{line} 引用了 {lb}')
    assert not offenders, (
        '以下位置在边查询里引用了契约不存在的标签 —— 查询会静默返回空而不报错：\n  '
        + '\n  '.join(sorted(set(offenders))))


def test_m04_dr_plan_ordering_兜底清单必须等于契约依赖边集合(dep_labels):
    """dr-plan-generator 的恢复顺序边集合不得与契约漂移。

    `_FALLBACK_SCOPE_EDGES` 刻意**不**在本断言范围内 —— 那是子图抽取范围，
    包含 `RunsOn` / `BelongsTo` 等承载边是正确的（做 DR 计划要把承载关系
    拉进来）。两个常量是两件事，别合并。
    """
    p = _ROOT / 'dr-plan-generator' / 'graph' / 'scope.py'
    if not p.exists():
        pytest.skip('dr-plan-generator 不在本工作树')
    src = p.read_text(encoding='utf-8')
    fallback = _tuple_literal_after(src, '_FALLBACK_ORDERING_EDGES')
    assert fallback == dep_labels, (
        f'dr-plan ordering 兜底清单与契约不一致。\n'
        f'契约有兜底没有: {sorted(dep_labels - fallback)}\n'
        f'兜底有契约没有: {sorted(fallback - dep_labels)}\n'
        f'恢复顺序按依赖边拓扑排，漏边会把该先恢复的排到后面。')


def test_m05_profile_示例cypher不得用参数占位符():
    """profile 的 few-shot 示例里不得出现 `$param` —— LLM 路径不绑定参数。

    ## 实测（2026-09-09）

    `profiles/petsite.yaml` 30 条示例 cypher 实跑，29 条通过，唯一失败的一条是
    `WHERE inc.start_time >= $since` → **MissingParameter**。

    原因在执行路径：LLM 生成的 cypher 走 `strands_tools.execute_cypher(cypher)`，
    **只收一个形参**。`neptune_client.query(cypher, params=None)` 虽然支持
    params，但那条路径从不传。

    few-shot 的作用就是让模型照抄模式，所以一条带 `$param` 的示例会教模型
    持续生成运行时必然失败的查询。危害在于**信号隐蔽**：用户看到的是 RCA
    回答变空或答非所问，`MissingParameter` 只出现在 Lambda 日志里，没人会
    把两者联系起来。

    ## 为什么锁「示例」而不是「加参数支持」

    加参数支持要让 LLM 同时产出 cypher 和参数字典，是 NL 层的接口变更；
    而 29/30 条示例本来就用字面量，把第 30 条对齐是零风险的一致性修正。
    真要支持参数化查询，应该连 `execute_cypher` 的签名一起改，那是另一件事。
    """
    import yaml
    p = _ROOT / 'profiles' / 'petsite.yaml'
    if not p.exists():
        pytest.skip('profiles/petsite.yaml 不在本工作树')
    prof = yaml.safe_load(p.read_text(encoding='utf-8'))

    found = []

    def walk(o):
        if isinstance(o, dict):
            c = o.get('cypher')
            if isinstance(c, str) and re.search(r'\$[a-zA-Z_]\w*', c):
                found.append((o.get('q') or o.get('question') or '?', c[:90]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(prof)
    assert not found, (
        '以下示例 cypher 含参数占位符，但 LLM 执行路径 '
        '(strands_tools.execute_cypher) 不绑定参数 —— 会教模型生成运行时'
        '必然 MissingParameter 的查询：\n  '
        + '\n  '.join(f'{q} → {c}' for q, c in found))


def test_m06_部署包里不得有从handler不可达的模块():
    """`infra/lambda/rca_window_flush/` 里每个模块都必须能从 handler 到达。

    ## 2026-09-09 删掉的四个文件

        neptune/schema_prompt.py     8847 B   上一代硬编码 prompt
        neptune/nl_query.py          4480 B
        neptune/nl_query_direct.py  15207 B
        neptune/query_guard.py       1520 B   只被上面两个用

    `core/`、`actions/`、`window_flush_handler.py` 对它们**零引用** ——
    它们只互相引用，构成一个自洽的孤岛，所以静态可达性查不出问题、
    运行时也不会报错。

    ## 为什么死代码在这里不是「无害的多余文件」

    契约 `profiles/graph_contract.yaml` 曾把
    `rca_window_flush/neptune/schema_prompt.py:135` 当作 `PublishesTo`
    改判依赖边的**论据之一**。那一行同时是：死代码、上一代（199 行硬编码，
    而现行是 63 行 profile 驱动）、且 cypher 实测 `HTTP 400 Variable 'r'
    not defined`。**一份没人执行的文件被当成了权威。**

    更实际的风险是误导后来者：将来谁要给这个 Lambda 接 NL 查询，会照着包里
    现存的那份接 —— 而那份是坏的、缺整个 agent 层（Delegates / InvokesTool /
    Retrieves）。留着它让未来的接线更可能出错，不是更不可能。

    ## 判据：从**全部**入口做可达性闭包

    第一版只用 `window_flush_handler` 当根，结果把 `handler.py` 和
    `actions/*` 五个模块全报成死代码 —— 那是**假阳性**。这个资产服务
    **两个** Lambda：

        gp-window-flush      handler = window_flush_handler.window_flush_handler
                             （由 infra/lib/alert-buffer-stack.ts:166 部署）
        petsite-rca-engine   handler = handler.lambda_handler
                             （CFN 外部署 —— 见 todo/tech-debt-etl-lambdas-outside-cfn.md，
                               所以 grep CDK 找不到它，只能从线上配置读出来）

    单入口假设会让这条门禁每次都红，而一条总是红的门禁等于没有门禁。
    所以两个 handler 都必须当根。新增入口时要同步加进 `_ENTRYPOINTS`。

    遍历用 `ast.walk` 而非只看模块头部：这个包大量使用**函数体内的延迟导入**
    来压 Lambda 冷启动（`window_flush_handler.py:42` 的
    `from core.alert_buffer import AlertBuffer` 就在函数里），
    只扫顶层 import 会把几乎所有模块误报成孤儿。
    """
    import ast

    # 两个入口都必须列全 —— 少一个就会把那一支的整条依赖链误报成死代码
    _ENTRYPOINTS = ('window_flush_handler', 'handler')

    #: 已知不可达但**刻意保留**的模块。
    #:
    #: `actions.feedback_collector`（Phase 4 用户反馈回写 Neptune）是一个
    #: **功能完整但尚未接线**的模块：两个 handler 都不调它，
    #: `tests/test_15_unit_rca_actions.py` 有 4 处单测覆盖它。
    #:
    #: 它与本轮删掉的四个 NL 文件性质不同 —— 那四个是**上一代的重复实现**
    #: （现行版本在 `rca/neptune/` 且已 profile 驱动），留着只会误导；
    #: 这一个是**唯一实现**，删了功能就没了。所以白名单而不是删除。
    #:
    #: 接线之后应把它从本白名单移除。
    _INTENTIONAL_ORPHANS = frozenset({'actions.feedback_collector'})

    pkg = _ROOT / 'infra' / 'lambda' / 'rca_window_flush'
    if not pkg.exists():
        pytest.skip('rca_window_flush 不在本工作树')

    mods = {}
    for f in pkg.rglob('*.py'):
        if f.name == '__init__.py':          # 包声明，不参与可达性
            continue
        name = '.'.join(f.relative_to(pkg).with_suffix('').parts)
        mods[name] = f

    for ep in _ENTRYPOINTS:
        assert ep in mods, f'入口 {ep} 不在包里 —— 门禁的根写错了'

    def local_imports(path: pathlib.Path) -> set:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            return set()
        out = set()
        for n in ast.walk(tree):          # walk = 含函数体内的延迟导入
            if isinstance(n, ast.Import):
                for a in n.names:
                    out.add(a.name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
                for a in n.names:         # from neptune import neptune_queries
                    out.add(f'{n.module}.{a.name}')
        return {m for m in out if m in mods}

    reachable, queue = set(), list(_ENTRYPOINTS)
    while queue:
        m = queue.pop()
        if m in reachable:
            continue
        reachable.add(m)
        queue.extend(local_imports(mods[m]))

    orphans = sorted(set(mods) - reachable - _INTENTIONAL_ORPHANS)
    stale_allow = sorted(_INTENTIONAL_ORPHANS & reachable)
    assert not stale_allow, (
        f'白名单里的模块其实已可达，应从 _INTENTIONAL_ORPHANS 移除：{stale_allow}\n'
        '白名单留着过期条目会掩盖真正的死代码。')
    assert not orphans, (
        '以下模块在部署包里但从任何入口都不可达（死代码）：\n  '
        + '\n  '.join(orphans)
        + '\n\n死代码会被后来者当成权威（契约曾引用已删的 schema_prompt.py:135'
          ' 作为分类论据，而那行 cypher 实测 HTTP 400），也会误导将来的接线。'
          '要么接上，要么删掉。')

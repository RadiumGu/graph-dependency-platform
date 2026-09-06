"""`source` 溯源完整性的守门测试。

2026-09-06 起因：UI 上三条 `Calls` 边的「数据源」列是空的
（`trafficgenerator`/`gateway-service`/`order-service` → `petsite`）。
查下来是两个独立问题，本文件各钉一条。

## 问题一：dependency 边缺 source 无人告警（m01/m02）

实测全图 **235 条边没有 `source`**，其中 **12 条是 dependency 边**
（`Calls` 11 + `AccessesData` 1），另 223 条是结构边（`dependency: false`，
生命周期跟随端点，本来就不需要 source）。

`source` 不是装饰属性 —— 每个 ETL 的按源对账只管自己那个 source
（`etl_deepflow` 只管 `source='deepflow-etl'`、`etl_xray` 只管 `'xray'`）。
一条没有 source 的 dependency 边**没有任何源认领它**，于是永远不会被刷新、
也不会被任何源的 reconcile 清理，只能靠 TTL 置 `active=false` 后永久留在图里。
这与节点侧那 7 个孤儿 LambdaFunction 是同一类：不是「资源没了」，
而是「没人负责它了」。

契约有 `sources` 词表、代码有 `assert_source()`，但两者只校验
「**写进去的** source 必须在词表里」，不校验「dependency 边**必须有** source」。
于是 11/20 条 `Calls` 边缺 source 却零告警 —— 这是「写了没人读」的反面：
**没写也没人查**。

## 问题二：写一次属性被无条件写（m03）

契约声明 `edge_write_once_attrs: [source, dependency_kind, first_seen]`，
`test_35::g08` 断言了**契约声明**这三个是写一次的，但**没有断言写入方遵守**。

实测 `etl_deepflow/neptune_etl_deepflow.py` 的 `Calls` 写入里，
`.property('source','deepflow-etl')` 在 `coalesce(...)` **之外** ——
即每轮无条件覆盖。后果：xray 先发现的 3 条 `Calls` 边，一旦 deepflow 也观测到，
`source='xray'` 会被改写成 `'deepflow-etl'`，**发现史被抹掉**。
g08 的 docstring 自己就记着 etl_aws 与 etl_cfn 犯过同一个错，
却没人检查 etl_deepflow。

声明在、门禁不在 —— 正是本项目实测过的那条：
**漂移量与有没有门禁相关，与声明得好不好无关。**
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = REPO / 'profiles' / 'graph_contract.yaml'
ETL_ROOT = REPO / 'infra' / 'lambda'
ETL_DIRS = ['etl_aws', 'etl_deepflow', 'etl_xray', 'etl_cfn', 'etl_agentcore']
VENDORED = ('requests', 'urllib3', 'certifi', 'charset', 'idna', '/yaml/', '_yaml',
            '__pycache__')


@pytest.fixture(scope='module')
def contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text())


def _etl_sources() -> list[tuple[pathlib.Path, str]]:
    out = []
    for d in ETL_DIRS:
        root = ETL_ROOT / d
        if not root.exists():
            continue
        for f in sorted(root.rglob('*.py')):
            if any(p in str(f) for p in VENDORED):
                continue
            out.append((f, f.read_text(errors='ignore')))
    return out


def _dependency_labels(contract: dict) -> set[str]:
    return {lb for lb, spec in (contract.get('edge_types') or {}).items()
            if spec.get('dependency') is True}


def _helper_guarantees_source(helper: str, caller_path: pathlib.Path) -> bool:
    """该 helper 是否在自己函数体里就写了 source。

    只在**同一个 ETL 目录内**找定义 —— 各 ETL 的 helper 同名但实现不同
    （etl_agentcore 的 `_upsert_edge` 写死 `source='{SOURCE}'`，
    etl_aws 的 `upsert_edge` 依赖调用方传入）。跨目录判断会得出错误结论。
    """
    for f in sorted(caller_path.parent.rglob('*.py')):
        if any(p in str(f) for p in VENDORED):
            continue
        txt = f.read_text(errors='ignore')
        m = re.search(rf'^def {re.escape(helper)}\(', txt, re.M)
        if not m:
            continue
        # 函数体 = 到下一个顶层 def / 文件末尾
        nxt = re.search(r'^def ', txt[m.end():], re.M)
        body = txt[m.start():m.end() + (nxt.start() if nxt else len(txt))]
        if "property('source'" in body or 'property("source"' in body:
            return True
    return False


# ── m01/m02：dependency 边必须能追溯到 source ──────────────────────────────

def test_m01_每个写dependency边的addE都必须带source(contract):
    """静态扫描：写 dependency 边的地方必须带 source。

    为什么静态扫描而不是运行时断言：`assert_source()` 只在**调用它**时生效，
    而漏写 source 的代码路径压根不会调用它 —— 运行时断言对「忘了写」这件事
    结构性地无能为力。这与 test_35::g14 用静态扫描抓标签字面量拼错同理。

    ⚠️ 第一版只扫字面量 `addE('X')`，**漏掉了通过 helper 写的边**。实测代价：
    `neptune-etl-trigger -[AccessesData]-> neptune-etl-from-aws` 至今仍无 source
    且每轮都在被刷新（last_seen 是当天），而 m01 判它通过 —— 因为它走的是
    `upsert_edge(src, dst, edge_lbl, props)`，标签是**变量**。
    所以这里必须同时扫两条路径。
    """
    dep_labels = _dependency_labels(contract)
    assert dep_labels, '契约里应有 dependency 边；扫不到说明测试自身失效'

    missing = []
    for path, src in _etl_sources():
        # 路径一：字面量 addE('Label')
        for lb in dep_labels:
            for m in re.finditer(rf"addE\(\s*['\"]{lb}['\"]", src):
                # 取该 addE 之后 1200 字符：足以覆盖一条 Gremlin 链的属性串
                seg = src[m.start():m.start() + 1200]
                if "property('source'" in seg or 'property("source"' in seg:
                    continue
                line = src[:m.start()].count('\n') + 1
                missing.append(f"{path.parent.name}/{path.name}:{line} 写 {lb} 未带 source")

        # 路径二：helper 调用。标签可能是变量，字面量扫描看不见。
        #
        # 但不能一律要求调用方传 source —— 有的 helper **在自己函数体里**写死
        # （etl_agentcore 的 `_upsert_edge` 就是 `property('source','{SOURCE}')`），
        # 那样调用方不必传。第一版没区分，把 9 处这类调用报成违规 ——
        # 假阳性会训练人忽略告警，比没有测试更糟。
        #
        # 正确判据：**被调的 helper 自己保不保证写 source**。
        for m in re.finditer(r'\b(_?upsert_edge)\(', src):
            helper = m.group(1)
            if _helper_guarantees_source(helper, path):
                continue
            line = src[:m.start()].count('\n') + 1
            all_lines = src.splitlines()
            raw_line = all_lines[line - 1] if line <= len(all_lines) else ''
            if raw_line.lstrip().startswith('#') or f'def {helper}' in raw_line:
                continue
            seg = src[m.start():m.start() + 700]
            depth = 0
            end = len(seg)
            for i, ch in enumerate(seg):
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            call = seg[:end]
            if "'source'" in call or '"source"' in call:
                continue
            missing.append(
                f"{path.parent.name}/{path.name}:{line} {helper} 未传 source"
                f"（该 helper 自身也不写）")

    assert not missing, (
        "这些地方写 dependency 边却没写 source：\n  " + "\n  ".join(missing)
        + "\n没有 source 的 dependency 边不被任何源的 reconcile 认领 —— "
          "永远不会被刷新，也不会被清理，只能靠 TTL 置 active=false 后永久留在图里。")


def test_m02_图级审计能查出缺source的dependency边(contract):
    """把「没写也没人查」这个缺口真正堵上：要有一个**查得出来**的审计。

    为什么不是往契约里加一个 `dependency_edge_requires_source: true`：
    那个布尔值没有任何消费方，本身就是本项目反复警惕的「写了没人读」——
    声明一条谁都不读的不变量，与不声明的效果相同。

    真正的堵法是有一个函数能对活图谱查出违规条数，并被 ETL 每轮上报。
    实测正是缺了它：全图 12 条 dependency 边没有 source（Calls 11 + AccessesData 1），
    却零告警，直到有人在 UI 上看见「数据源」列是空的才发现。
    """
    from graph_cleanup import audit_dependency_edges_without_source

    seen = []

    def fake(q):
        seen.append(q)
        return {'result': {'data': {'@value': [3]}}}

    out = audit_dependency_edges_without_source(fake)
    dep_labels = _dependency_labels(contract)
    assert set(out['per_label']) == dep_labels, (
        f"审计必须覆盖全部 dependency 边类型。\n"
        f"漏掉: {sorted(dep_labels - set(out['per_label']))}\n"
        f"多出: {sorted(set(out['per_label']) - dep_labels)}")
    assert out['total'] == 3 * len(dep_labels)
    for q in seen:
        assert "hasNot('source')" in q, f'审计查询应筛无 source 的边: {q[:120]}'
    # 结构边不得被审计 —— 它们本来就不需要 source，报进去是噪声
    struct = {lb for lb, spec in contract['edge_types'].items()
              if spec.get('dependency') is not True}
    for lb in struct:
        assert not [q for q in seen if f"hasLabel('{lb}')" in q], \
            f'结构边 {lb} 不该进审计'


# ── m03：写一次属性必须真的写一次 ─────────────────────────────────────────

def test_m03_写一次属性不得被无条件写(contract):
    """`source`/`dependency_kind`/`first_seen` 必须写在 coalesce 的 addE 分支内，
    或用 `coalesce(values(x), constant(...))` 的幂等写法。

    无条件写的后果是抹掉发现史：xray 先发现的 3 条 Calls 边，deepflow 一观测到
    就会把 source='xray' 改写成 'deepflow-etl'。
    test_35::g08 只断言契约**声明**了写一次，没有断言写入方**遵守**。
    """
    woa = set(contract.get('edge_write_once_attrs') or [])
    assert woa, '契约应声明 edge_write_once_attrs'

    offenders = []
    for path, src in _etl_sources():
        lines = src.splitlines()
        for attr in woa:
            for m in re.finditer(rf"\.property\(\s*['\"]{attr}['\"]\s*,", src):
                line_no = src[:m.start()].count('\n') + 1
                # 跳过注释行 —— 第一版没跳，把 1196 行注释里引用的
                # `.property('first_seen', ts)` 报成了违规。守门测试的假阳性
                # 比没有测试更糟：它训练人忽略告警。
                raw = lines[line_no - 1] if line_no <= len(lines) else ''
                if raw.lstrip().startswith('#'):
                    continue
                # 幂等写法：property('x', __.coalesce(__.values('x'), ...))
                tail = src[m.start():m.start() + 220]
                if 'coalesce' in tail and f"values('{attr}')" in tail:
                    continue
                before = src[:m.start()]
                last_adde = before.rfind('.addE(')
                if last_adde == -1:
                    continue      # 不是建边路径（如更新已有边的独立语句）

                # 判断这处 property 是落在 coalesce(...) 之内还是之外。
                #
                # 第一版用「`")` 单独成行」当闭合标志 —— 那只是本仓库的一种书写
                # 惯例，不是语法事实。实测漏报：etl_aws/handler.py 把闭合与属性写在
                # 同一行（`f").property('source','aws-etl')"`），于是那处**无条件
                # 覆盖 source** 的真违规被判为合规。漏报比误报更隐蔽 ——
                # 测试是绿的，问题却在线上跑着。
                #
                # 改成括号配平：从最近的 `.coalesce(` 起逐字符计深度，深度归零处
                # 就是它的闭合位置。Gremlin 字符串字面量里的括号（如
                # `containing('x')`）本身是配平的，不影响计数。
                c = before.rfind('.coalesce(')
                if c == -1:
                    continue      # 没有 coalesce，无从谈「在分支外」
                # 注意：coalesce 必然**开在 addE 之前** —— addE 是它的其中一个分支。
                # 第一版这里写成 `if c < last_adde: continue`，把唯一的正常情形
                # 当成跳过条件，于是测试恒绿、什么都抓不到。
                depth = 0
                closed_at = None
                for i, ch in enumerate(src[c:m.start()], start=c):
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                        if depth == 0:
                            closed_at = i
                            break
                if closed_at is None:
                    continue      # 到 property 处仍未闭合 → 在分支内 → 合规
                if closed_at < last_adde:
                    continue      # 闭合发生在 addE 之前 → 这不是同一条链
                offenders.append(
                    f"{path.parent.name}/{path.name}:{line_no} 无条件写 {attr}"
                    f"（coalesce 已在第 "
                    f"{src[:closed_at].count(chr(10)) + 1} 行闭合）")

    assert not offenders, (
        "这些地方在 coalesce 之外无条件写了写一次属性，会抹掉先发现方的记录：\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n修法：把它移进 addE 分支，或改用 "
          "property('x', __.coalesce(__.values('x'), __.constant(...))) 幂等写法。")

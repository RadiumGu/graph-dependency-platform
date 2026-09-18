"""test_52_rca_query_direction.py — 钉住 RCA 两个方向查询的**遍历方向**与**边类型覆盖**。

## 为什么需要这一条门禁

`q1_blast_radius`（影响面）与 `q3_upstream_deps`（根因候选）问的是图上**两个相反
方向**的问题。方向搞反不会报错、不会抛异常、返回结构完全正常 —— 它只是把
受害者当成嫌疑人。这类错误没有任何运行时信号，只能靠断言遍历方向本身来防。

实测它已经错了很久（2026-09-06 修）：

    契约里依赖边一律从依赖方指向被依赖方
      AgentTool -[DependsOn]-> LambdaFunction
      BusinessCapability -[DependsOn]-> RDSCluster
      LambdaFunction -[AccessesData]-> DynamoDBTable

    因此   影响面   = 入边 (u)-[dep]->(n)     谁依赖 n
           根因候选 = 出边 (n)-[dep]->(d)     n 依赖谁

    而原实现里 q1 走出边、q3 走入边，**两个都反了**。
    更隐蔽的是 q1 自己内部矛盾：services 部分走出边、capabilities 部分走入边，
    两半都标「受影响」。

决定性证据是一对对称结果 —— 图里唯一一条 live 服务间边 `petsite → petsearch`：

    q1('petsite')   → petsearch     标为「petsite 的影响面」
    q3('petsearch') → petsite       标为「petsearch 的根因候选」

同一条边的两种读法不可能同时成立，而且都是错的：petsite 挂了 petsearch 不受影响
（petsearch 不依赖 petsite）；petsearch 挂了 petsite 是受害者而非根因。

## 为什么顺带钉边类型

原实现硬编码 `Calls|DependsOn`，只覆盖契约 7 类 dependency 边中的 2 类。
实测漏掉的 5 类里 `AccessesData` 有 58 条（服务 → RDS / DynamoDB / S3 / SSM /
StepFunctions）—— **真实根因基本都在数据层**。硬编码让根因候选里永远不出现
数据库，`petsite` 的候选数从 17 塌成 1。

方向对了但只看两类边，等于把门修好了却只开一条缝。两件事一起钉。
"""
import re
from pathlib import Path

import pytest

from paths import PROJECT_ROOT

_ROOT = Path(PROJECT_ROOT)
LAMBDA_COPY = (_ROOT / 'infra' / 'lambda' / 'rca_window_flush'
               / 'neptune' / 'neptune_queries.py')
RCA_COPY = _ROOT / 'rca' / 'neptune' / 'neptune_queries.py'


@pytest.fixture
def captured():
    """替掉 nc.results，捕获真实生成的 Cypher，不连库。

    比静态扫源码强：断言的是**实际交给 Neptune 的那条语句**，
    参数拼接、f-string 插值、条件分支都已经生效。
    """
    from neptune import neptune_queries as nq

    seen: list[str] = []
    orig = nq.nc.results

    def fake(cypher, params=None):
        seen.append(cypher)
        return []

    nq.nc.results = fake
    try:
        yield nq, seen
    finally:
        nq.nc.results = orig


def _contract_dep_labels() -> set:
    import yaml
    with open(_ROOT / 'profiles' / 'graph_contract.yaml', encoding='utf-8') as fh:
        gc = yaml.safe_load(fh) or {}
    return {k for k, v in (gc.get('edge_types') or {}).items()
            if isinstance(v, dict) and v.get('dependency')}


def test_m01_根因候选必须走出边(captured):
    """q3 = 「故障服务依赖谁」。出边方向，因为依赖边指向被依赖方。"""
    nq, seen = captured
    nq.q3_upstream_deps('petsite', kind='live')
    assert seen, 'q3 没有生成任何查询'
    q = seen[0]
    # 出边：锚点在箭头左侧  MATCH (n {name: $svc})-[r:...]->(dep)
    assert re.search(r'MATCH\s*\(\s*n\s*\{\s*name:\s*\$svc\s*\}\s*\)\s*-\[', q), (
        '根因候选必须从故障服务**出发**（出边）。\n'
        '入边查的是「谁调用了它」= 受害者，不是根因。\n'
        f'实际生成:\n{q}')
    assert not re.search(r'-\[[^\]]*\]->\s*\(\s*n\s*\{\s*name:\s*\$svc', q), (
        f'q3 出现了指向锚点的入边模式，方向反了：\n{q}')


def test_m02_影响面必须走入边(captured):
    """q1 = 「谁依赖故障节点」。入边方向。"""
    nq, seen = captured
    nq.q1_blast_radius('petsite', kind='live')
    assert seen, 'q1 没有生成任何查询'
    # 第一条是 services 查询，第二条是 capabilities
    svc_q = seen[0]
    assert re.search(r'-\[[^\]]*\]->\s*\(\s*n\s*\{\s*name:\s*\$node', svc_q), (
        '影响面必须是**指向**故障节点的入边（谁依赖它）。\n'
        '出边查的是「它依赖谁」= 根因方向。\n'
        f'实际生成:\n{svc_q}')
    assert not re.search(r'MATCH\s*\(\s*n\s*\{\s*name:\s*\$node\s*\}\s*\)\s*-\[', svc_q), (
        f'q1 的 services 查询出现了从锚点出发的出边模式，方向反了：\n{svc_q}')

    # capabilities 部分同样必须是入边 —— 原实现这半是对的，但另有一段 UNION
    # 查的是「和 n 依赖同一个 svc 的 capability」，即 n 的**兄弟**，
    # n 挂了它们不受影响。那段已删除，这里钉住不许回来。
    bc_q = seen[1] if len(seen) > 1 else ''
    assert 'BusinessCapability' in bc_q, f'q1 第二条应查 capability，实际:\n{bc_q}'
    assert '<-[' not in bc_q and 'UNION' not in bc_q, (
        'capability 影响面不得包含「兄弟节点」那段 UNION —— '
        '与 n 依赖同一个下游的 capability 并不依赖 n，n 挂了它们不受影响。\n'
        f'实际生成:\n{bc_q}')


def test_m03_两个查询都必须覆盖契约全部dependency边类型(captured):
    """不许硬编码 Calls|DependsOn —— 那让根因候选里永远没有数据库。"""
    nq, seen = captured
    dep = _contract_dep_labels()
    assert len(dep) >= 3, f'契约里 dependency 边太少，测试自身可能失效: {dep}'

    nq.q3_upstream_deps('petsite', kind='live')
    nq.q1_blast_radius('petsite', kind='live')
    svc_queries = [q for q in seen if 'BusinessCapability' not in q]
    assert svc_queries, '没抓到服务侧查询'

    for q in svc_queries:
        used = set(re.findall(r'\[[^\]]*?:([A-Za-z|]+)', q))
        labels = set()
        for grp in used:
            labels |= set(grp.split('|'))
        missing = dep - labels
        assert not missing, (
            f'查询漏掉了契约里这些 dependency 边类型: {sorted(missing)}\n'
            f'实测 AccessesData 有 58 条（服务 → RDS/DynamoDB/S3/SSM/StepFunctions），'
            f'真实根因基本都在数据层 —— 漏掉它等于让根因候选里永远没有数据库。\n'
            f'实际生成:\n{q}')


def test_m04_兜底边类型清单不得与契约漂移():
    """`_dependency_edge_labels()` 的 fallback 是 Lambda 里真正生效的那份。

    实测它漂移过：契约 7 类，兜底只有 6 类（少 `Invokes`），
    而 `infra/lambda/rca_window_flush/` 那份副本的父目录里没有 `profiles/`，
    只能走兜底 —— 线上因此漏掉 16 条 `Invokes` 边，本地却是对的。
    """
    dep = _contract_dep_labels()
    src = RCA_COPY.read_text(encoding='utf-8')
    m = re.search(r'def _dependency_edge_labels.*?\n    return (\[[^\]]*\])',
                  src, re.S)
    assert m, '没找到 _dependency_edge_labels 的兜底 return'
    fallback = set(re.findall(r'"([A-Za-z]+)"', m.group(1)))
    assert fallback == dep, (
        f'兜底清单与契约不一致。\n契约有兜底没有: {sorted(dep - fallback)}\n'
        f'兜底有契约没有: {sorted(fallback - dep)}')


def test_m05_两份副本的查询实现必须一致():
    """本文件在仓库里有两份，第二份打进 gp-window-flush Lambda。

    只改一份会让线上与本地对同一个问题给出相反的答案，
    而这种不一致不会有任何运行时信号。比较代码而非 docstring ——
    两份的 docstring 刻意不同（各自解释自己那份为什么需要）。
    """
    def body(text: str, name: str) -> str:
        m = re.search(rf'^def {re.escape(name)}\(', text, re.M)
        assert m, f'{name} 未找到'
        nxt = re.search(r'^def ', text[m.end():], re.M)
        seg = text[m.start():m.end() + (nxt.start() if nxt else len(text) - m.end())]
        # 去掉三引号 docstring 与注释行，只留代码
        seg = re.sub(r'""".*?"""', '', seg, flags=re.S)
        return '\n'.join(ln for ln in seg.split('\n')
                         if ln.strip() and not ln.strip().startswith('#'))

    a = RCA_COPY.read_text(encoding='utf-8')
    b = LAMBDA_COPY.read_text(encoding='utf-8')
    for fn in ('q1_blast_radius', 'q3_upstream_deps', '_dependency_edge_labels'):
        assert body(a, fn) == body(b, fn), (
            f'{fn} 两份副本的代码不一致：\n'
            f'  {RCA_COPY}\n  {LAMBDA_COPY}\n'
            '方向类改动必须同时落到两份，否则线上与本地对同一问题给出相反答案。')

"""test_38_source_vocabulary.py —— `source` 词表门禁。

## 这组测试守什么

`SOURCES` 从引入起就在契约里，也被 `graph_contract.py` re-export，
但**没有任何一处检查读它**。2026-08-31 对活图谱普查实测的漂移量：

    取值              条数    谁在写
    eks-etl           1228    handler.py 13 处（语义正确，契约漏声明）
    aws-etl-static       3    handler.py:376（语义正确，契约漏声明）
    deepflow             8    etl_deepflow 节点（deepflow-etl 的同义漂移）
    manual               1    无代码在写，手工遗留

对照同一份 YAML 里**被** `assert_edge_type` 检查的节点/边类型：零漂移。
漂移量与「有没有门禁」相关，与「声明得好不好」无关 —— 所以这里补的是门禁，
外加一个静态扫描把没有运行时收口的写入路径也纳进来。

## 为什么需要 g04 这条静态扫描

`upsert_vertex` / `upsert_edge` 是 etl_aws 的收口点，能在运行时挡住。
但 etl_deepflow / etl_xray / etl_cfn 各自手拼 Gremlin 字符串，没有共同收口点。
重构它们风险远大于收益，所以改用源码扫描：任何 `'source': 'xxx'` 字面量都要在词表内。
这样新增一个源时，忘记更新契约会在测试里失败，而不是等普查时才发现。
"""
from __future__ import annotations

import os
import re
import pathlib

import pytest

from paths import PROJECT_ROOT  # noqa: F401  （注入 sys.path）

LAYER = pathlib.Path(PROJECT_ROOT) / 'infra' / 'lambda' / 'shared' / 'python'


def _contract():
    import importlib
    import sys
    if str(LAYER) not in sys.path:
        sys.path.insert(0, str(LAYER))
    gc = importlib.import_module('graph_contract')
    return importlib.reload(gc)


# ── g01：词表里应有的取值 ────────────────────────────────────────────────────

def test_g01_declared_sources_cover_every_value_the_code_writes():
    """代码在写的四个取值必须都已声明。

    `eks-etl` 与 `aws-etl-static` 是刻意的语义区分（K8s API vs AWS 控制面、
    静态声明 vs 运行时观测），不是拼写错误，所以是补声明而不是改代码。
    """
    gc = _contract()
    for src in ('aws-etl', 'eks-etl', 'aws-etl-static', 'deepflow-etl',
                'cfn-etl', 'xray', 'nfm', 'business-layer'):
        assert gc.is_declared_source(src), f"{src} 应在契约词表内"


def test_g02_synonym_drift_is_not_blessed():
    """`deepflow` 是 `deepflow-etl` 的同义漂移，**不得**通过扩词表消化。

    两个名字指同一个源。按源分派的逻辑（edge_verification._OBSERVER_MARKERS、
    etl_deepflow 的存量清理查询）认的是全名，放行同义词会造成静默漏判。
    """
    gc = _contract()
    assert not gc.is_declared_source('deepflow')
    assert not gc.is_declared_source('manual')  # 手工遗留，正式名是 manual-fix


# ── g03：门禁三种模式 ────────────────────────────────────────────────────────

def test_g03_assert_source_enforce_warn_off(monkeypatch):
    gc = _contract()

    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'enforce')
    gc.assert_source('aws-etl')                      # 合法取值不抛
    with pytest.raises(gc.GraphContractError) as ei:
        gc.assert_source('deepflow', 'unit-test')
    msg = str(ei.value)
    assert 'deepflow' in msg
    # 报错必须给出可行动信息：合法取值清单 + 出错位置
    assert 'graph_contract.yaml' in msg
    assert 'unit-test' in msg

    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'warn')
    gc.assert_source('deepflow')                     # warn 只 log，放行

    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'off')
    gc.assert_source('whatever-未声明')               # off 完全跳过


# ── g04：源码里的 source 字面量全部在词表内 ─────────────────────────────────

_ETL_DIRS = ('infra/lambda/etl_aws', 'infra/lambda/etl_cfn',
             'infra/lambda/etl_deepflow', 'infra/lambda/etl_xray')

# 只匹配"写入图谱的 source 属性"这三种形态，避免误伤 EventBridge 的
# body.get('source') 与 rca 的告警 signal source（那两个不是图谱属性）。
#
# 刻意**不**加更宽的 `'source'\s*,\s*'(...)'`：它会命中 etl_deepflow:1280
# 那行 docstring 里的 `{'kind','subject','source','target',...}`，
# 把字段名清单当成取值。宽正则带来的假阳性会让这条测试被当噪声关掉。
_PATTERNS = (
    re.compile(r"""'source':\s*'([a-z0-9-]+)'"""),
    re.compile(r"""\.property\(\s*single\s*,\s*'source'\s*,\s*'([a-z0-9-]+)'\s*\)"""),
    re.compile(r"""\.property\(\s*'source'\s*,\s*'([a-z0-9-]+)'\s*\)"""),
)


def test_g04_no_undeclared_source_literal_in_etl_source_code():
    gc = _contract()
    offenders = []
    for rel in _ETL_DIRS:
        d = pathlib.Path(PROJECT_ROOT) / rel
        if not d.is_dir():
            continue
        for f in d.rglob('*.py'):
            if '__pycache__' in f.parts or 'site-packages' in f.parts:
                continue
            # 只扫本仓库自己的文件，跳过打进部署目录的第三方依赖
            if any(part in ('requests', 'urllib3', 'certifi', 'idna',
                            'charset_normalizer', 'yaml', '_yaml', 'bin')
                   for part in f.parts):
                continue
            text = f.read_text(errors='replace')
            for pat in _PATTERNS:
                for m in pat.finditer(text):
                    val = m.group(1)
                    if not gc.is_declared_source(val):
                        line = text[:m.start()].count('\n') + 1
                        offenders.append(f"{f.relative_to(PROJECT_ROOT)}:{line} source={val!r}")
    assert not offenders, (
        "以下写入点的 source 取值未在 profiles/graph_contract.yaml 的 sources 里声明：\n"
        + "\n".join(sorted(set(offenders)))
        + "\n合法取值：" + str(sorted(gc.SOURCES))
    )


# ── g05：upsert_edge 必须采纳调用方的 source（回归） ────────────────────────

def _load_etl_aws_client(monkeypatch):
    """隔离加载 etl_aws/neptune_client.py，并把 neptune_query 换成捕获器。

    为什么不能直接 `import neptune_client`：
    conftest 刻意**不**把 `infra/lambda/etl_aws` 放进全局 sys.path
    （那里有 vendored 的 urllib3/requests 副本，会遮蔽已安装版本），
    而且 `config` 这个顶层名已被 rca+dr 的合并模块占用，
    etl_aws 自己的 `config.py` 拿不到 —— 实测报
    `ImportError: cannot import name 'FAULT_BOUNDARY_MAP' from 'config'`。

    这里用 spec_from_file_location 按路径加载，并只在本测试内
    （monkeypatch.setitem，测试结束自动还原）把 `config` 指向合并后的模块。
    好处是不污染 sys.path，也不影响 test_12 已建立的全局状态。
    """
    import importlib.util as iu
    import sys
    import types

    etl = pathlib.Path(PROJECT_ROOT) / 'infra' / 'lambda' / 'etl_aws'

    # etl_aws 自己的 config，按路径加载后并入 sys.modules['config']
    spec = iu.spec_from_file_location('_etl_cfg_for_t38', etl / 'config.py')
    cfg = iu.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    merged = types.ModuleType('config')
    for src in (sys.modules.get('config'), cfg):
        if src is None:
            continue
        for attr in dir(src):
            if not attr.startswith('_'):
                setattr(merged, attr, getattr(src, attr))
    monkeypatch.setitem(sys.modules, 'config', merged)
    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'enforce')

    spec = iu.spec_from_file_location('_etl_nc_for_t38', etl / 'neptune_client.py')
    nc = iu.module_from_spec(spec)
    spec.loader.exec_module(nc)

    captured = []

    def fake_query(g):
        captured.append(g)
        return {'result': {'data': {'@value': []}}}

    # 用 setattr 而非 monkeypatch：模块是本次新建的，不存在需要还原的全局状态
    nc.neptune_query = fake_query
    return nc, captured


def test_g05_upsert_edge_honors_caller_source(monkeypatch):
    """回归：调用方传 eks-etl，生成的 Gremlin 就必须写 eks-etl。

    修复前的实现把「写一次」做成了**丢弃调用方取值**：
        write_once = {'source': 'aws-etl'}   # 硬编码
        if ks in write_once: continue        # 调用方的 source 被跳过
    于是 handler.py 里 13 处 eks-etl + 1 处 aws-etl-static 全部静默失效。
    活图谱看不出来，因为 coalesce 保护了存量边 —— 只有新建的边会被写错。
    """
    nc, captured = _load_etl_aws_client(monkeypatch)

    nc.upsert_edge('v1', 'v2', 'RunsOn', {'source': 'eks-etl'})
    assert len(captured) == 1
    g = captured[0]
    assert "__.constant('eks-etl')" in g, f"调用方的 source 被丢弃了：{g}"
    assert "'aws-etl'" not in g

    captured.clear()
    nc.upsert_edge('v1', 'v2', 'AccessesData', {'source': 'aws-etl-static'})
    assert "__.constant('aws-etl-static')" in captured[0]


def test_g06_upsert_edge_defaults_to_aws_etl(monkeypatch):
    """不传 source 时回落 aws-etl —— 绝大多数调用点依赖这个缺省。"""
    nc, captured = _load_etl_aws_client(monkeypatch)
    nc.upsert_edge('v1', 'v2', 'LocatedIn')
    assert "__.constant('aws-etl')" in captured[0]


def test_g07_upsert_edge_rejects_undeclared_source(monkeypatch):
    """未声明的取值在 enforce 下必须写不进去。"""
    nc, captured = _load_etl_aws_client(monkeypatch)
    import graph_contract as gc
    with pytest.raises(gc.GraphContractError):
        nc.upsert_edge('v1', 'v2', 'RunsOn', {'source': 'deepflow'})
    assert not captured, "违约的写入不得发出 Gremlin"


def test_g08_caller_cannot_override_dependency_kind(monkeypatch):
    """`dependency_kind` 仍由函数按边类型判定，调用方不得指定。

    这条是上一版行为里**正确**的部分，修 source 时不能一起放开 ——
    否则 static/dynamic 会随调用点各说各话。
    """
    nc, captured = _load_etl_aws_client(monkeypatch)
    nc.upsert_edge('v1', 'v2', 'AccessesData', {'dependency_kind': 'dynamic'})
    g = captured[0]
    assert "__.constant('static')" in g
    assert ".property('dependency_kind', 'dynamic')" not in g

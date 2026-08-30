"""图谱契约门禁测试。

这一组测试的作用是让 profiles/graph_contract.yaml **不是装饰品**：

- g01/g02  契约与 graph_schema_text 的类型名集合必须完全相同 ——
           两份文档平级、互不生成，漂移由测试拦。这消灭了「声明说 33 种、
           实际写进去 34 种」这类计数漂移。
- g03      Lambda 层数据产物必须与来源同步（改了 YAML 忘了重新生成会失败）。
- g04–g08  契约自身的完整性：每个类型都有身份键、身份键都不可变、
           端点都是已声明的类型、依赖边都有 TTL。
- g09/g10  门禁真的会拒绝（enforce 抛错、warn 放行）—— 证明不是空转。
- g11      **契约声明的身份键 == ETL 实际传入的 identity_prop**。
           这是最要紧的一条：没有它，契约可以和代码任意脱节。
- g12      时间戳字段唯一。

参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 1、2、7。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
CONTRACT_YAML = REPO / 'profiles' / 'graph_contract.yaml'
PROFILE_YAML = REPO / 'profiles' / 'petsite.yaml'

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


# ── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def contract() -> dict:
    return yaml.safe_load(CONTRACT_YAML.read_text())


@pytest.fixture(scope='module')
def schema_types() -> tuple[set, set]:
    """从 graph_schema_text 抽出 (节点类型集, 边类型集)。

    字符类必须含 0-9：EC2Instance / K8sService / S3Bucket 都带数字，
    漏掉会让整行不匹配（实测把 26 种边少解析成 23 种）。
    """
    prof = yaml.safe_load(PROFILE_YAML.read_text())
    txt = prof['neptune']['graph_schema_text']
    lines = txt.splitlines()
    ni = next(i for i, l in enumerate(lines) if l.strip().startswith('## 节点类型'))
    ei = next(i for i, l in enumerate(lines) if l.strip().startswith('## 边类型'))

    nodes = set()
    for l in lines[ni:ei]:
        m = re.match(r'^\s{0,6}-\s+([A-Z][A-Za-z0-9]*)\s*:', l)
        if m:
            nodes.add(m.group(1))

    edges = set()
    for m in re.finditer(r'-\[:([A-Za-z0-9|:]+)\]->', txt):
        for lb in m.group(1).split('|:'):
            if lb:
                edges.add(lb)
    return nodes, edges


@pytest.fixture()
def gc(monkeypatch):
    """门禁模块。每个用例都重置模式环境变量，避免相互污染。"""
    monkeypatch.delenv('GRAPH_CONTRACT_MODE', raising=False)
    monkeypatch.delenv('GRAPH_CONTRACT_ENDPOINTS', raising=False)
    import graph_contract
    return graph_contract


# ── g01/g02 契约 ↔ schema 文本 类型名集合一致 ─────────────────────────────

def test_g01_node_type_names_match_schema_text(contract, schema_types):
    nodes, _ = schema_types
    declared = set(contract['node_types'])
    assert declared == nodes, (
        f"契约与 graph_schema_text 的节点类型不一致。\n"
        f"  仅契约有: {sorted(declared - nodes)}\n"
        f"  仅文本有: {sorted(nodes - declared)}\n"
        f"两份文档平级，任一侧新增类型都必须同步另一侧。"
    )


def test_g02_edge_type_names_match_schema_text(contract, schema_types):
    _, edges = schema_types
    declared = set(contract['edge_types'])
    assert declared == edges, (
        f"契约与 graph_schema_text 的边类型不一致。\n"
        f"  仅契约有: {sorted(declared - edges)}\n"
        f"  仅文本有: {sorted(edges - declared)}"
    )


# ── g03 Lambda 层产物未过期 ───────────────────────────────────────────────

def test_g03_generated_artifact_is_current():
    r = subprocess.run(
        [sys.executable, str(REPO / 'scripts' / 'gen_graph_contract.py'), '--check'],
        capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, (
        f"infra/lambda/shared/python/graph_contract_data.py 与 "
        f"profiles/graph_contract.yaml 不同步。\n"
        f"  修复: python3 scripts/gen_graph_contract.py --write\n"
        f"  stderr: {r.stderr}"
    )


# ── g04–g08 契约自身完整性 ────────────────────────────────────────────────

def test_g04_every_node_type_declares_identity(contract):
    missing = [n for n, v in contract['node_types'].items() if not v.get('identity')]
    assert not missing, f"这些节点类型没有声明身份键: {missing}"


def test_g05_no_node_type_has_mutable_identity(contract):
    """身份键可变 = 同一实体会在图里裂成两份。

    实测后果见 infra/lambda/etl_aws/neptune_client.py:62-77 —— 14 个
    EC2Instance 里 4 个是重复实体。允许的取值只有 True 与 'lifetime'
    （后者指 K8s Pod 这类重建即换名的对象，那是预期语义不是缺陷）。
    """
    bad = {n: v.get('immutable') for n, v in contract['node_types'].items()
           if v.get('immutable') not in (True, 'lifetime')}
    assert not bad, f"这些节点类型的身份键不是不可变的: {bad}"


def test_g06_edge_endpoints_are_declared_node_types(contract):
    """端点必须是已声明的节点类型。

    这条断言持续拦住一类笔误：graph_schema_text 里 WritesTo 的 dst 原本写的是
    S3 / SNS / SQS，而真实类型名是 S3Bucket / SNSTopic / SQSQueue。
    """
    nodes = set(contract['node_types'])
    bad = []
    for e, v in contract['edge_types'].items():
        for side in ('src', 'dst'):
            for n in v.get(side) or []:
                if n not in nodes:
                    bad.append(f"{e}.{side}={n}")
    assert not bad, f"这些端点不是已声明的节点类型: {bad}"


def test_g07_dependency_edges_declare_expiry(contract):
    """依赖边必须有 TTL。

    结构边（LocatedIn / Contains / BelongsTo…）的 expires_seconds 可以是 None，
    语义是生命周期跟随两端节点；但「A 依赖 B」这类边必须能独立失效，
    否则观测停止后会永久留在图里变成 ghost 边。
    """
    bad = [e for e, v in contract['edge_types'].items()
           if v.get('dependency') and not v.get('expires_seconds')]
    assert not bad, f"这些依赖边没有声明 expires_seconds: {bad}"


def test_g08_write_once_attrs_declared(contract):
    """source / dependency_kind / first_seen 必须是写一次属性。

    它们记录的是「谁首先发现了这条依赖」。被后写的源覆盖等于抹掉发现史 ——
    实测 etl_aws/neptune_client.py:161 与 etl_cfn/neptune_etl_cfn.py:143
    都是无条件 .property('source', ...)，会覆盖 xray/deepflow 先写的值。
    """
    woa = set(contract.get('edge_write_once_attrs') or [])
    assert {'source', 'dependency_kind', 'first_seen'} <= woa, (
        f"写一次属性声明不全: {sorted(woa)}")


# ── g09/g10 门禁真的生效 ──────────────────────────────────────────────────

def test_g09_enforce_mode_rejects_undeclared_types(gc, monkeypatch):
    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'enforce')
    gc.assert_node_type('EC2Instance')          # 已声明 → 放行
    gc.assert_edge_type('Calls')                # 已声明 → 放行
    with pytest.raises(gc.GraphContractError):
        gc.assert_node_type('EC2Instancee')     # 拼错 → 必须拒绝
    with pytest.raises(gc.GraphContractError):
        gc.assert_edge_type('Callz')


def test_g10_warn_mode_allows_but_logs(gc, monkeypatch, caplog):
    monkeypatch.setenv('GRAPH_CONTRACT_MODE', 'warn')
    with caplog.at_level('WARNING'):
        gc.assert_node_type('TotallyBogusType')  # 灰度模式：放行，不抛
    # 用 getMessage() 而不是 r.message —— 后者是未插值的模板（含 %s），
    # 直接对它做 % r.args 在 args 为空时会抛 TypeError。
    assert any('TotallyBogusType' in r.getMessage() for r in caplog.records), \
        "warn 模式必须 log 出违约，否则灰度期看不到真实违约"


# ── g11 契约声明的身份键 == ETL 实际传入的 identity_prop ──────────────────

_UPSERT_RE = re.compile(r"upsert_vertex\(\s*'([A-Za-z0-9]+)'", re.M)


def _etl_identity_call_sites() -> dict[str, set]:
    """静态扫描 etl_aws 的 upsert_vertex 调用点，取 (label → 传入的 identity_prop 集合)。

    做法是从每个调用点起向后扫到括号配平，再在该片段里找 identity_prop=。
    不用固定字符窗口 —— handler.py 里的属性字典很长，固定窗口会漏
    （实测 400 字符窗口漏掉了 EC2 的 identity_prop='instance_id'）。
    """
    etl = REPO / 'infra' / 'lambda' / 'etl_aws'
    out: dict[str, set] = {}
    vendored = ('requests', 'urllib3', 'certifi', 'charset', 'idna', 'yaml')
    for f in sorted(etl.rglob('*.py')):
        if any(p in str(f) for p in vendored):
            continue
        src = f.read_text(errors='ignore')
        for m in _UPSERT_RE.finditer(src):
            label = m.group(1)
            i = src.index('(', m.start())
            depth, j = 0, i
            while j < len(src):
                if src[j] == '(':
                    depth += 1
                elif src[j] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            seg = src[i:j + 1]
            idp = re.search(r"identity_prop\s*=\s*'([A-Za-z_]+)'", seg)
            out.setdefault(label, set()).add(idp.group(1) if idp else 'name')
    return out


def test_g11_etl_identity_prop_matches_contract(contract):
    """ETL 传入的身份键必须与契约声明一致。

    没有这条断言，契约就可以和代码任意脱节 —— 那正是引入契约之前的状态
    （profiles/petsite.yaml 被称作权威，但四个 ETL 无一 import 它）。
    """
    sites = _etl_identity_call_sites()
    assert sites, "扫不到任何 upsert_vertex 调用点，测试自身失效了"
    mismatches = []
    for label, passed in sorted(sites.items()):
        declared = contract['node_types'].get(label, {}).get('identity')
        if declared is None:
            mismatches.append(f"{label}: 代码在写，但契约里没声明")
            continue
        if passed != {declared}:
            mismatches.append(
                f"{label}: 契约声明 identity={declared!r}，代码实际传入 {sorted(passed)}")
    assert not mismatches, (
        "契约与代码的身份键不一致:\n  " + "\n  ".join(mismatches) +
        "\n修法：在 upsert_vertex 调用处传 identity_prop=<契约声明的属性名>，"
        "并确保该属性在 extra_props 里。"
    )


# ── g13 cfn 的身份键假设 ──────────────────────────────────────────────────

def test_g13_cfn_written_types_use_name_identity(contract):
    """etl_cfn 的 get_or_create_vertex 硬编码以 name 匹配，所以它写的每种类型
    在契约里的身份键必须就是 name。

    没有这条断言的后果：将来把某个类型（例如 LambdaFunction）的身份键改成 arn
    时，etl_aws 会跟着契约走，而 etl_cfn 仍按 name 匹配 —— 两个 ETL 用不同的
    身份键写同一类节点，图里必然裂成两份。这正是身份键类缺陷最隐蔽的形态。
    """
    cfn = REPO / 'infra' / 'lambda' / 'etl_cfn' / 'neptune_etl_cfn.py'
    src = cfn.read_text()
    m = re.search(r'TYPE_TO_LABEL\s*=\s*\{(.*?)\n\}', src, re.S)
    assert m, "找不到 TYPE_TO_LABEL 映射，测试自身失效了"
    labels = set(re.findall(r":\s*'([A-Za-z0-9]+)'", m.group(1)))
    assert labels, "TYPE_TO_LABEL 解析为空"

    bad = {}
    for lb in sorted(labels):
        spec = contract['node_types'].get(lb)
        if spec is None:
            # cfn 刻意映射了 APIGateway / KinesisStream 但本账号零实例、
            # 也刻意不写进 schema（见 etl_cfn 的注释）。这类不算违约。
            continue
        if spec.get('identity') != 'name':
            bad[lb] = spec.get('identity')
    assert not bad, (
        f"etl_cfn 以 name 匹配，但契约给这些类型声明了别的身份键: {bad}\n"
        f"要改身份键必须同时改 etl_cfn/neptune_etl_cfn.py 的 get_or_create_vertex，"
        f"否则 etl_aws 与 etl_cfn 会用不同身份键写同一类节点。"
    )


# ── g14 四个 ETL 里的标签字面量都必须已声明 ───────────────────────────────

_LABEL_PATTERNS = (
    r"\(T\.label\):\s*'([A-Za-z0-9]+)'",      # mergeV/mergeE 的身份 map
    r"\baddV\('([A-Za-z0-9]+)'\)",
    r"\baddE\('([A-Za-z0-9]+)'\)",
    r"\bhasLabel\('([A-Za-z0-9]+)'\)",
    r"\b(?:inE|outE|bothE)\('([A-Za-z0-9]+)'\)",
    r"\bhas\('([A-Z][A-Za-z0-9]+)',\s*'name'",  # has(label, 'name', v) 三参形态
)

# 这些标签在代码里出现但**刻意不进契约**，逐个给出理由。
# 白名单必须带理由 —— 无理由的豁免等于把门禁关掉。
_LABEL_ALLOWLIST = {
    'APIGateway':    'etl_cfn 映射了但本账号零实例，刻意不写进 schema（见 etl_cfn 注释）',
    'KinesisStream': '同上',
    'Serves':        '已废弃的遗留边类型。etl_aws/handler.py:1258 每轮主动 '
                     "hasLabel('Serves').drop() 清理它，**从不写入**；"
                     'rca/neptune/neptune_queries.py:30-32 也已移除对它的遍历。'
                     '2026-08-30 活图谱实测 0 条。加进契约等于把废弃类型重新合法化。',
}


def test_g14_all_etl_label_literals_are_declared(contract):
    """静态扫描四个写入 ETL 的 Gremlin 标签字面量，逐个对契约核验。

    为什么要静态扫描而不只靠运行时 assert：
      deepflow / xray 写的是**字面量**标签（'Microservice' / 'Calls' /
      'AWSServiceEndpoint'），运行时 assert 对字面量拼错毫无帮助 ——
      拼错的字面量会照样通过它自己那句 assert 的参数。
      静态扫描在 CI 期就能抓到，且零运行时风险。
    """
    declared = set(contract['node_types']) | set(contract['edge_types'])
    etl_root = REPO / 'infra' / 'lambda'
    targets = ['etl_aws', 'etl_deepflow', 'etl_xray', 'etl_cfn']
    vendored = ('requests', 'urllib3', 'certifi', 'charset', 'idna', '/yaml/', '_yaml')

    found: dict[str, set] = {}
    for t in targets:
        for f in sorted((etl_root / t).rglob('*.py')):
            if any(p in str(f) for p in vendored):
                continue
            src = f.read_text(errors='ignore')
            for pat in _LABEL_PATTERNS:
                for m in re.finditer(pat, src):
                    found.setdefault(m.group(1), set()).add(f"{t}/{f.name}")

    assert found, "扫不到任何标签字面量，测试自身失效了"
    undeclared = {lb: sorted(where) for lb, where in found.items()
                  if lb not in declared and lb not in _LABEL_ALLOWLIST}
    assert not undeclared, (
        "这些标签在 ETL 代码里被写入/查询，但未在 profiles/graph_contract.yaml 声明:\n  "
        + "\n  ".join(f"{lb}: {where}" for lb, where in sorted(undeclared.items()))
        + "\n新增类型请改契约并跑 scripts/gen_graph_contract.py --write；"
          "确实不该进契约的请加进 _LABEL_ALLOWLIST 并写明理由。"
    )


# ── g12 时间戳字段唯一 ────────────────────────────────────────────────────

def test_g12_single_timestamp_field(contract, gc):
    """跨源必须有唯一的「最后何时被看到」字段。

    实测三家分别叫 last_updated(aws) / last_scanned(cfn) / last_seen(deepflow,xray)，
    导致跨源查询无统一字段可用。契约声明 last_seen 为唯一写入字段，
    另两个只作为读取侧的兼容别名。
    """
    assert contract['timestamp_field'] == 'last_seen'
    aliases = set(contract['timestamp_legacy_aliases'])
    assert {'last_updated', 'last_scanned'} <= aliases
    assert contract['timestamp_field'] not in aliases, "统一字段不应同时列为遗留别名"
    assert gc.TIMESTAMP_FIELD == contract['timestamp_field']

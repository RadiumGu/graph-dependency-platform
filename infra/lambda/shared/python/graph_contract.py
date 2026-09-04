"""图谱契约门禁 —— 写入 Neptune 之前校验类型与身份键。

## 为什么存在

引入本模块之前，四个写入 ETL（etl_aws / etl_deepflow / etl_xray / etl_cfn）
**无一 import profiles**，运行时对节点与边类型零校验：`upsert_vertex` /
`upsert_edge` 拿到什么 label 就往 Gremlin 里直拼。后果是任何拼错的或新造的
标签都会被**静默**写进 Neptune，而所谓「权威 schema」
（profiles/petsite.yaml 的 graph_schema_text）只在测试期被比对一次。
参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 1。

业界的对照做法是 New Relic 的 entity-definitions：身份定义是声明式 YAML，
配 PR + 自动校验 + owner 双评审，而不是散在代码里的字符串字面量。

## 三种模式

由环境变量 `GRAPH_CONTRACT_MODE` 控制，默认 `enforce`：

- `enforce` —— 违约抛 `GraphContractError`，那一次写入失败。
                这是默认值：静默写入未声明类型正是要消除的缺陷。
- `warn`    —— 只 log warning 并放行。用于**灰度**：新接一个 ETL 或大改类型
                声明时先跑一轮 warn，把真实违约摸清再切 enforce，
                避免一上线就把整轮采集打断。
- `off`     —— 完全跳过。只应在离线回放/单测夹具里用。

## 刻意不做的事

- **不校验属性集**。schema 文本里的属性列表是文档性的、且各源写入的属性子集
  本来就不同（xray 只补度量、cfn 只写 declared_in）。强制属性集会把
  「这个源没有这项数据」误判成违约。
- **端点约束默认只 warn**。src/dst 白名单来自 schema 文本的
  `(:A)-[:E]->(:B)` 行，那份声明本身就不完整（实测 WritesTo 的 dst 写的是
  S3/SNS/SQS 这种非类型名）。先观察一段再决定是否升级为 enforce，
  由 `GRAPH_CONTRACT_ENDPOINTS` 单独控制。
"""
from __future__ import annotations

import logging
import os

from graph_contract_data import (  # noqa: F401 (re-export)
    CONTRACT_VERSION,
    EDGE_TYPES,
    EDGE_WRITE_ONCE_ATTRS,
    NODE_ATTR_AUTHORITY,
    NODE_TYPES,
    SOURCES,
    TIMESTAMP_FIELD,
    TIMESTAMP_LEGACY_ALIASES,
)

logger = logging.getLogger()

MODE_ENFORCE = 'enforce'
MODE_WARN = 'warn'
MODE_OFF = 'off'
_VALID_MODES = (MODE_ENFORCE, MODE_WARN, MODE_OFF)


class GraphContractError(ValueError):
    """写入违反图谱契约。"""


def _mode() -> str:
    """每次调用都读环境变量 —— 便于单测用 monkeypatch 切换模式。"""
    m = (os.environ.get('GRAPH_CONTRACT_MODE') or MODE_ENFORCE).strip().lower()
    return m if m in _VALID_MODES else MODE_ENFORCE


def _endpoints_mode() -> str:
    """端点约束的模式，默认 **enforce**（2026-08-30 从 warn 升级）。

    升级依据是对活图谱做的**全图三元组普查**，而不是「跑一轮 ETL 看日志」——
    普查覆盖全部 1731 条边 / 85 种 (srcLabel, edgeLabel, dstLabel) 形态，
    比单轮 ETL 的日志完整（一轮 ETL 只会碰到它自己那部分形态）。

    普查结果与处置：
      - 违约 204 条 → 其中 5 种形态（21 条）经逐条核对确认**语义正确、
        只是 schema 漏声明**，补进 PAIR_ADDITIONS；
      - 剩余 183 条全部是 find_vertex_by_name 不带标签造成的**错源边**，
        改代码修根因 + infra/fix_wrong_source_edges.py 清存量；
      - 收紧成配对校验后又浮出 LambdaFunction->S3Bucket（平铺白名单下被
        笛卡尔积掩盖的合法组合），一并补进声明。
    也就是说：升级 enforce 时，声明侧已无已知缺口。

    仍保留独立开关的理由：普查只能看到**当下图里存在**的形态。低频 ETL
    路径（例如周期很长的 Lambda）写的形态可能当时不在图里，一旦 enforce
    拒写会中断该步骤。真出现这种情况时设 GRAPH_CONTRACT_ENDPOINTS=warn
    先放行并收集，补进声明后再切回，不必回滚代码。
    """
    m = (os.environ.get('GRAPH_CONTRACT_ENDPOINTS') or MODE_ENFORCE).strip().lower()
    return m if m in _VALID_MODES else MODE_ENFORCE


def _violate(mode: str, msg: str) -> None:
    if mode == MODE_OFF:
        return
    if mode == MODE_ENFORCE:
        raise GraphContractError(msg)
    logger.warning("graph-contract: %s", msg)


# ── 类型门禁 ──────────────────────────────────────────────────────────────

def assert_source(source: str, context: str = '') -> None:
    """校验 source 取值在契约声明的词表内。

    ## 为什么补这个门禁

    `SOURCES` 从引入起就存在，且被 graph_contract.py **re-export**，但
    **没有任何一处检查读它** —— 声明了词表却不设门禁。2026-08-31 对活图谱普查
    实测后果：

        取值              条数    状态
        eks-etl           1228    代码在写（handler.py 13 处），未声明
        aws-etl-static       3    代码在写（handler.py:376），未声明
        deepflow             8    代码在写（节点，etl_deepflow:1124/1165），
                                  未声明（契约里叫 deepflow-etl）
        manual               1    无代码在写，手工遗留，未声明
                          ----
                          1240    占全图 1802 条边 + 1063 节点的 43%

    对照同一份 YAML 里**被** `assert_edge_type` 检查的那部分：零漂移。
    也就是说漂移量与「有没有门禁」完全相关，与「声明得好不好」无关。

    ## 为什么不顺手放宽词表了事

    `eks-etl` / `aws-etl-static` 是**刻意的语义区分**（K8s API 与 AWS 控制面是不同
    的真值来源、静态声明与运行时观测是不同的证据等级），补进声明是对的。
    而 `deepflow` 是 `deepflow-etl` 的**同义漂移**，两个名字指同一个源 ——
    这种要收敛写入侧，不能靠扩词表消化，否则 `edge_verification._OBSERVER_MARKERS`
    这类按源分派的逻辑会漏判。

    Args:
        source: 待写入的 source 取值。
        context: 出错信息里附带的调用位置，便于定位是哪条写入路径违约。
    """
    if source in SOURCES:
        return
    where = f"（{context}）" if context else ''
    _violate(_mode(), (
        f"source={source!r} 未在契约中声明{where}。"
        f"合法取值见 profiles/graph_contract.yaml 的 sources："
        f"{sorted(SOURCES)}"
    ))


def is_declared_source(source: str) -> bool:
    """只判断不抛错 —— 给需要自己决定如何处置的调用方（例如批量清理脚本）。"""
    return source in SOURCES


def assert_node_type(label: str) -> None:
    """节点类型必须已在契约里声明。"""
    mode = _mode()
    if mode == MODE_OFF or label in NODE_TYPES:
        return
    _violate(mode, f"未声明的节点类型 {label!r}（契约 v{CONTRACT_VERSION} 共 "
                   f"{len(NODE_TYPES)} 种）。新增类型请改 profiles/graph_contract.yaml "
                   f"并跑 scripts/gen_graph_contract.py --write")


def assert_edge_type(label: str, src_label: str = None, dst_label: str = None) -> None:
    """边类型必须已声明；端点若给出则按声明核对（端点部分默认只 warn）。

    ## 两端都知道时按 **配对** 校验，只知道一端时退回平铺白名单

    平铺 src/dst 白名单的校验强度退化成笛卡尔积，实测会放行真缺陷：
    `Manages` 加进真实存在的 HPA→Deployment 后，平铺白名单连
    Deployment→Deployment（本次查出的错源边之一）都会放行。
    所以两端已知时一律走 `pairs`。
    """
    mode = _mode()
    if mode == MODE_OFF:
        return
    spec = EDGE_TYPES.get(label)
    if spec is None:
        _violate(mode, f"未声明的边类型 {label!r}（契约 v{CONTRACT_VERSION} 共 "
                       f"{len(EDGE_TYPES)} 种）")
        return
    emode = _endpoints_mode()
    if emode == MODE_OFF:
        return

    pairs = spec.get('pairs')
    if pairs and src_label and dst_label:
        if [src_label, dst_label] not in pairs and (src_label, dst_label) not in pairs:
            _violate(emode,
                     f"边 {label} 的端点组合 ({src_label})->({dst_label}) 未声明。"
                     f"已声明的组合: {[f'{s}->{d}' for s, d in pairs]}")
        return

    if src_label and src_label not in spec['src']:
        _violate(emode, f"边 {label} 的源类型 {src_label!r} 不在声明的白名单 {spec['src']}")
    if dst_label and dst_label not in spec['dst']:
        _violate(emode, f"边 {label} 的目标类型 {dst_label!r} 不在声明的白名单 {spec['dst']}")


# ── 身份键 ────────────────────────────────────────────────────────────────

def identity_prop_for(label: str) -> str | None:
    """返回该节点类型声明的身份属性名。

    返回 'name' 时调用方无需特殊处理（那就是历史默认行为）；
    返回其它属性名时调用方应把它作为 mergeV 的匹配键，把 name 降级为普通属性。
    未声明的类型返回 None，让调用方保持原行为而不是崩掉。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('identity') if spec else None


def identity_is_stable_for_merge(label: str) -> bool:
    """身份键能否安全用作 mergeV 的匹配键。

    'lifetime' **算通过** —— K8s Pod 那类对象重建后换名，那是**新实体**而不是
    「同一实体被改名」，所以按名 merge 不会把一个实体裂成两份。
    可变身份键的真实危害是后者（实测 14 个 EC2Instance 里 4 个是重复实体，
    因为 name 取自可变的 Name 标签而机器本身一直是同一台）。

    这个函数取代了原来的 `identity_is_immutable`。改名的理由见
    `identity_survives_recreation` 的文档 —— 原名问的是一个问题，
    实现回答的是另一个。
    """
    spec = NODE_TYPES.get(label)
    if not spec:
        return False
    return spec.get('immutable') in (True, 'lifetime')


def identity_survives_recreation(label: str) -> bool:
    """身份键能否跨「对象被销毁重建」保持不变。只有 immutable: true 才行。

    'lifetime' 明确返回 **False** —— 这正是它与 `true` 的区别所在：
    Pod / K8sService / Deployment / HPA 重建即换名，同一逻辑对象在图里会换成
    一个新节点。

    ── 为什么要拆成两个函数（2026-09-04）────────────────────────────────
    原来只有一个 `identity_is_immutable`，函数体是现在
    `identity_is_stable_for_merge` 的实现（把 'lifetime' 判成不可变）。
    问题不是实现写错了，而是**一个名字混淆了两个问题**：

        Q1 「这个键可以安全 merge 吗」        -> 'lifetime' 是 **可以**
        Q2 「这个键跨对象重建还成立吗」        -> 'lifetime' 是 **不成立**

    函数名写的是 Q2（immutable 的字面意思），函数体答的是 Q1。因为它当时
    **零调用方**，两种读法都没被真正验证过，所以哪个语义"对"取决于第一个
    调用者以为自己在问什么 —— 这种歧义迟早会以一个静默错判的形式兑现。

    拆开之后两个问题各有名字，并由 tests/test_42 锁住「两者必须在且仅在
    lifetime 类型上分歧」这条不变量。
    """
    spec = NODE_TYPES.get(label)
    if not spec:
        return False
    return spec.get('immutable') is True


def scope_gap_for(label: str) -> str | None:
    """该类型身份键**已知的作用域缺口**，无缺口返回 None。

    契约里的 `scope_note` 原先没有任何读取方（纯注释），于是
    TargetGroup 那条过期半个月的 note 无人发现。给它一个 accessor 是让
    「已知缺陷」能被程序取用的最小代价 —— 报告与审计脚本据此提示，
    tests/test_42 据此要求每个 `lifetime` 类型必须申报缺口。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('scope_note') if spec else None


def preferred_identity_for(label: str) -> str | None:
    """更稳健的身份键候选（通常是 arn），未声明返回 None。

    这**不是**当前生效的身份键 —— 取当前值要用 `identity_prop_for`。
    它表达的是「存量回填完成后该切到哪个键」这个待办，切换的前提条件由
    tests/test_42 的 live 用例对活图谱自动核验（全部节点都有该属性、
    且取值唯一），而不是靠人记得回来看一眼 note。

    注意「回填完成」只是**必要**条件。2026-09-04 实测还有第二个条件：
    该类型不能被第二个按 name 匹配的 ETL 同时写入 —— 见 `preferred_blocked_by`。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('preferred') if spec else None


def preferred_blocked_by(label: str) -> str | None:
    """候选身份键为何**还不能**切换，无阻塞返回 None。

    存在的理由：光有 `preferred` 会重造它自己刚修掉的缺陷 —— 一个没有
    时限也没有阻塞说明的承诺，过期了没人知道（TargetGroup 那条 note 过期
    半个月就是这么来的）。声明了 `preferred` 却既不可切换又不说明原因的类型，
    由 tests/test_42 判失败。

    2026-09-04 的实测阻塞：6 个类型（LoadBalancer / DynamoDBTable / SQSQueue /
    SNSTopic / LambdaFunction / StepFunction）同时被 etl_cfn 写入，而
    etl_cfn 的 get_or_create_vertex 硬编码按 name 匹配，且它手上只有被
    规范化成短名的 physical_id。单方面切 arn 会让两个 ETL 用不同身份键写
    同一类节点 —— 这正是 test_35::g13 预告过的形态。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('preferred_blocked_by') if spec else None


def node_expires_seconds_for(label: str):
    """该**节点**类型的软过期阈值（秒）。None 表示不过期。

    ── 为什么节点也需要这个（2026-09-04）──────────────────────────────
    契约里结构边写的 `expires_seconds: None` 注解是「生命周期跟随两端节点」，
    但节点此前**根本没有生命周期机制**，所以那句话在实现上是悬空的。
    代价实测：Pod 在图里有 581 个节点，集群实际只跑 70 个 —— 511 个（88%）
    超过 1 天没刷新。任何「这个服务跑在哪些 Pod 上」的查询都会拖出一堆
    早已消失的 Pod。

    None 的类型必须同时声明 `expiry_note` 说明理由（追加式事件日志、
    或来自 json 的声明而非观测值），由 tests/test_43 强制 —— 不允许沉默地
    不声明，那与「忘了写」无法区分。
    """
    spec = NODE_TYPES.get(label)
    return spec.get('expires_seconds') if spec else None


def node_expiry_note_for(label: str) -> str | None:
    """节点类型不过期的理由（仅 expires_seconds 为 None 时应存在）。"""
    spec = NODE_TYPES.get(label)
    return spec.get('expiry_note') if spec else None


# ── 生命周期 ──────────────────────────────────────────────────────────────

def expires_seconds_for(label: str):
    """该边类型的软删除阈值（秒）。

    None 表示结构边，生命周期跟随两端节点、不独立过期
    （对应 Dynatrace 的 static edge 继承 node lifetime 语义）。
    """
    spec = EDGE_TYPES.get(label)
    return spec.get('expires_seconds') if spec else None


def retention_seconds_for(label: str):
    """硬删除边界（秒）。None 表示不硬删，只软删除。"""
    spec = EDGE_TYPES.get(label)
    return spec.get('retention_seconds') if spec else None


def is_dependency_edge(label: str) -> bool:
    """是否是「A 依赖 B」语义的边（需要 dependency_kind）。

    取代此前散在三个 ETL 里各自复制的 DEPENDENCY_EDGE_LABELS 常量 ——
    那份复制在 etl_aws/neptune_client.py 的注释里已被作者标注为待收敛项。
    """
    spec = EDGE_TYPES.get(label)
    return bool(spec and spec.get('dependency'))


def dependency_edge_labels() -> frozenset:
    return frozenset(lb for lb, s in EDGE_TYPES.items() if s.get('dependency'))


# ── 多源权威 ──────────────────────────────────────────────────────────────

def may_write_node_attr(label: str, attr: str, source: str) -> bool:
    """该来源是否有权写这个节点属性。

    未在 NODE_ATTR_AUTHORITY 里声明的 (类型, 属性) 一律放行 ——
    权威表是**例外清单**而非白名单，只登记已实测出冲突的属性。
    这样引入门禁不会把大量正常写入判成违约。
    """
    authority = NODE_ATTR_AUTHORITY.get(label, {}).get(attr)
    return True if authority is None else source in authority


def filter_node_props(label: str, props: dict, source: str) -> tuple[dict, list]:
    """按权威表过滤节点属性，返回 (放行的属性, 被拒的属性名)。

    被拒的属性**明示返回**而不是静默丢弃 —— 对应 ServiceNow IRE 的
    maskedAttributes：调用方应把它 log 出来，否则「谁赢」永远查不清。
    """
    kept, masked = {}, []
    for k, v in (props or {}).items():
        if may_write_node_attr(label, k, source):
            kept[k] = v
        else:
            masked.append(k)
    return kept, masked

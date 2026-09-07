"""按「注入后端 × 目标类型 × 注入位置」判定一条边能否被验证（T-305b，2026-09-05）。

## 为什么要替掉原来的判据

T-305 第一版读**边上存储的理由字符串**（`chaos-mesh-cannot-target-lambda` 这类
token，上一次注入失败后写下的）。两个问题：

1. **对从未验证过的边无效** —— 没有 token 就排除不了。这正是把 `Invokes` 改成
   `dependency: true`（T-306）之后立刻会遇到的情况：那 16 条边的 `verify_*` 已被
   清除，改完会以 `untested` 进队列，字符串判据拿它们没办法。
2. **判据本身把三件不同的事混成了一类**，而且其中两条结论是错的：

   | 类别 | 例子 | 与注入后端有关？ | 第一版的错 |
   |---|---|---|---|
   | 工具触不到目标 | Chaos Mesh 打不到 Lambda | **是** | 判成 permanent，但 **FIS 有 3 个 Lambda 动作**，边其实可验证 |
   | 可注入但稳态不可观测 | 镜像仓库依赖（只在拉镜像时用到） | 否 | 判成 permanent，实际是「需要复合实验」 |
   | 没有观测方 | 自环、合成流量源 | 否 | 判成 permanent —— 这一类**是对的** |

## 三条轴

**轴一：callee-side reach —— 后端能否直接打到目标节点。**
判据取自 `fault_catalog.yaml` 里每条动作的 `requires`（它声明需要哪种 ARN），
不是猜的。Chaos Mesh 全部 19 条 `requires: []` 且作用于 Pod netns。

**轴二：caller-side cut —— 在调用方侧切断出向，与目标类型无关。**
Chaos Mesh 的 `NetworkChaos` 支持 `externalTargets`（runner 在
`chaos_mcp.py:207-208` 真的写进 spec），DNSChaos 同理。只要**源**是集群内的 Pod，
就能切断它到任意外部地址的流量 —— 目标是 Lambda 还是 S3 都无所谓。
这条轴是 2026-08-31 引入的第三种注入拓扑，首条 `petsite->ssm` 由它判 confirmed。

**轴三：可观测性 —— 与后端完全无关，不该混进矩阵。**
`no_observer`（自环无下游、合成流量源无消费方）与
`needs_compound_experiment`（依赖只在启动期被用到，稳态注入看不出退化）
都不是工具能力问题，所以单列。

## 刻意的取舍

* **不认子串**：理由 token 只取冒号前的部分。子串匹配会让「说明文字里恰好提到某
  token」的边被误排除 —— 与「不要用裸词 grep 做判据」是同一条教训。
* **`requires` 里有 5 种 ARN 在图谱里没有对应节点类型**（volume / nodegroup /
  asg / route_table / IAM role），这些动作永远选不出靶点。如实登记在
  `_UNMAPPED_REQUIRES`，与 test_41 查出的「37 条里 5 条从来不可执行」一致。
* **矩阵只回答「能不能施加」**，不回答「该不该打」（那是 scope 的职责，T-306）。
"""
from __future__ import annotations

import os
import pathlib

import yaml

_CATALOG = pathlib.Path(__file__).with_name('fault_catalog.yaml')

# ── 图谱节点类型 → 集群内 Pod 语义 ────────────────────────────────────────────
# Chaos Mesh 与 FIS 的 aws:eks:pod-* 动作都靠 Pod 选择器定位，这三种节点类型
# 最终都落到 Pod 上（Microservice/Deployment 经 label selector 展开）。
POD_BACKED_LABELS = frozenset({'Pod', 'Microservice', 'Deployment'})

# ── FIS `requires` 里的 ARN 种类 → 图谱节点类型 ───────────────────────────────
# 只登记图谱里真有对应类型的；没有的进 _UNMAPPED_REQUIRES。
_REQUIRES_TO_LABEL = {
    'function_arn': frozenset({'LambdaFunction'}),
    'cluster_arn': frozenset({'RDSCluster', 'RDSInstance'}),
    'global_table_arn': frozenset({'DynamoDBTable'}),
    'bucket_arn': frozenset({'S3Bucket'}),
    'subnet_arn': frozenset({'Subnet'}),
    'vpc_endpoint_arn': frozenset({'AWSServiceEndpoint'}),
}

# 图谱里没有对应节点类型 ⇒ 这些动作永远选不出靶点。如实登记而不是假装能用。
_UNMAPPED_REQUIRES = frozenset({
    'volume_arns',          # EBS 卷：图谱无 EBSVolume 类型
    'nodegroup_arn',        # EKS 节点组：图谱无 NodeGroup 类型
    'asg_arn',              # 弹性伸缩组：图谱无 AutoScalingGroup 类型
    'route_table_arn',      # 路由表：图谱无 RouteTable 类型
    'role_arn',             # IAM 角色（API 错误注入按角色限定）：图谱无 IAMRole 类型
    'managed_resource_arn',  # ARC zonal autoshift
    'availability_zone',    # 不是资源，是参数
})

# ── 轴二：支持在源侧切断出向的后端能力 ───────────────────────────────────────
# 只要源是集群内 Pod，就能切断它到任意目标的流量，目标类型无关。
CALLER_SIDE_CUT_TYPES = frozenset({'network_partition', 'dns_chaos'})

# ── 轴三：与后端无关的不可验证原因 ───────────────────────────────────────────
# 这一类不进矩阵：换任何后端结论都一样，因为缺的不是注入能力。
NO_OBSERVER = 'no_observer'
NEEDS_COMPOUND = 'needs_compound_experiment'
PRECONDITION_UNMET = 'precondition_unmet'
UNREACHABLE = 'unreachable_by_any_backend'
INJECTABLE = 'injectable'

# 理由 token → 轴三分类。仅用于**标注既有判定**，不再作为排除的主判据。
REASON_TOKEN_CLASS = {
    'self-loop-from-trace': NO_OBSERVER,
    'synthetic-traffic-source': NO_OBSERVER,
    # 依赖只在 Pod 启动拉镜像时被用到：注入**可以**施加（源是 Pod，externalTargets
    # 能切），但稳态下没有可观测退化。配合删 Pod 就是可验证的复合实验，
    # 所以不是「不可注入」。
    'image-repo-dependency': NEEDS_COMPOUND,
    # 环境条件，变了就该重测 —— 必须实查集群，不能靠这张表下结论。
    'target-scaled-to-zero': PRECONDITION_UNMET,
    'source-absent-from-cluster': PRECONDITION_UNMET,
    # 工具能力边界：**由矩阵回答**，这里只保留映射以便识别历史文本。
    # 注意它不再等于「不可验证」—— FIS 有 3 个 Lambda 动作。
    'chaos-mesh-cannot-target-lambda': UNREACHABLE,
}

_matrix_cache: dict | None = None


def _load_catalog() -> dict:
    path = os.environ.get('FAULT_CATALOG_PATH') or str(_CATALOG)
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def build_matrix() -> dict:
    """从故障目录构建 {backend: {'callee': set(labels), 'caller_side': bool}}。

    不硬编码动作清单 —— 目录是唯一权威源，加一条动作矩阵自动跟着变。
    """
    global _matrix_cache
    if _matrix_cache is not None:
        return _matrix_cache

    cat = _load_catalog()
    matrix: dict = {}

    # Chaos Mesh：全部作用于 Pod netns；其中两类支持源侧切断
    cm = cat.get('chaosmesh') or []
    cm_types = {it.get('type') for it in cm}
    matrix['chaosmesh'] = {
        'callee': set(POD_BACKED_LABELS),
        'caller_side': bool(cm_types & CALLER_SIDE_CUT_TYPES),
        'actions': len(cm),
    }

    # FIS：requires 为空的是 aws:eks:pod-*（Pod 语义）；其余按 ARN 种类映射
    #
    # ⚠️ 先滤掉 category: orchestration —— 那类条目（aws:fis:wait）不注入故障、
    #    也不作用于任何资源，它的 requires 天然为空。若不滤掉，下面「requires
    #    为空 ⇒ Pod 语义」这条推断会把一个空动作当成「FIS 能打到 Pod」的证据。
    #    `actions` 计数同样只算真正能注入的动作，否则可注入性判断会被虚高的
    #    数字带偏。
    fis_all = [it for it in (cat.get('fis') or [])
               if it.get('category') != 'orchestration']
    fis_callee: set = set()
    unmapped: set = set()
    for it in fis_all:
        req = tuple(it.get('requires') or ())
        if not req:
            fis_callee |= set(POD_BACKED_LABELS)
            continue
        for r in req:
            if r in _REQUIRES_TO_LABEL:
                fis_callee |= set(_REQUIRES_TO_LABEL[r])
            else:
                unmapped.add(r)
    matrix['fis'] = {
        'callee': fis_callee,
        # FIS 的 disrupt-connectivity 按子网限定，等效于源侧切断，但它要求源在该子网内，
        # 判定比 Chaos Mesh 的 externalTargets 复杂，保守起见不计入源侧能力。
        'caller_side': False,
        # 只算能注入的动作（已滤掉 orchestration），不是目录条目总数。
        'actions': len(fis_all),
        'unmapped_requires': unmapped,
    }
    _matrix_cache = matrix
    return matrix


def reason_class(reason_text: str) -> str:
    """既有判定理由的轴三分类（只认冒号前的 token，不做子串匹配）。"""
    if not reason_text:
        return ''
    token = str(reason_text).split(':', 1)[0].strip()
    return REASON_TOKEN_CLASS.get(token, '')


def injectability(src_label: str, dst_label: str, reason_text: str = '') -> tuple[str, str]:
    """一条边能否被注入验证，返回 (verdict, 人类可读依据)。

    判定顺序有意如此：
      1. 轴三里**与后端无关**的先判 —— 换后端结论不变，没必要过矩阵。
         但 `unreachable` 不在此列：它恰恰是矩阵要重新裁决的那一类。
      2. 再过矩阵（callee-side reach ∪ caller-side cut）。
    """
    cls = reason_class(reason_text)
    if cls in (NO_OBSERVER, NEEDS_COMPOUND, PRECONDITION_UNMET):
        return cls, f'与注入后端无关：{cls}'

    m = build_matrix()
    reach = [b for b, spec in m.items() if dst_label in spec['callee']]
    if reach:
        return INJECTABLE, f"目标类型 {dst_label} 可由 {'/'.join(sorted(reach))} 直接注入"

    if src_label in POD_BACKED_LABELS:
        cutters = [b for b, spec in m.items() if spec['caller_side']]
        if cutters:
            return INJECTABLE, (
                f"目标类型 {dst_label} 无后端可直接打，但源 {src_label} 在集群内，"
                f"可由 {'/'.join(sorted(cutters))} 在源侧切断出向流量")

    return UNREACHABLE, (
        f'无任何后端能打到目标类型 {dst_label}，且源 {src_label} 不在集群内、'
        f'无法在源侧切断')


def should_skip_target(src_label: str, dst_label: str, reason_text: str = '') -> tuple[bool, str]:
    """选靶时是否跳过这条边。

    `PRECONDITION_UNMET` **返回不跳过**：前置条件可能已经变了（副本扩回来、Pod
    重新部署），而复核要实查集群、不属选边职责。永久排除会造出自己的盲区 ——
    与第一版把 conditional 与 permanent 分开是同一个理由，但这里更进一步：
    交给下游做廉价复核，而不是在选边阶段就替它决定。
    """
    verdict, why = injectability(src_label, dst_label, reason_text)
    if verdict in (NO_OBSERVER, UNREACHABLE):
        return True, why
    if verdict == NEEDS_COMPOUND:
        return True, why + '（需复合实验：切断 + 触发重启，当前 runner 未实现）'
    return False, why

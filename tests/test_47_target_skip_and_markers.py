"""T-305 / T-307：选靶前排除结构性不可注入的边 + 观测源标记覆盖派生证据。

## T-305 起因（实测，不是设想）

活图谱上 16 条边的 `verify_experiment` 文本**完全相同**、时间戳也相同
（`1788561628`）：

    chaos-mesh-cannot-target-lambda: Chaos Mesh operates on Pod netns;
    Lambda/StepFunctions run outside the cluster

也就是这 16 条记录的信息量是**一条规则**而不是 16 份证据 —— 注入手段的能力边界由
**目标的运行平台**决定。而 `select_targets_for_verification` 原先把 `inconclusive`
判成 pri=2「上次未能下结论，需重试」，于是这批边被永久排进重试队列；再叠加它们被
旧写入路径写坏的 `confidence=0.0`（越低越优先），它们还会霸占队首。

判据早就写在图里，选边器不读 —— 属「写了但没人读」那一类。

## 关键区分：permanent vs conditional

`target-scaled-to-zero` / `source-absent-from-cluster` 是**环境条件**，扩容或重新
部署后就该重测；永久排除会让这些边再也不被验证。而
`chaos-mesh-cannot-target-lambda` / `self-loop-from-trace` 在现有工具下重试多少次
结果都一样。两者必须分开，否则要么白烧注入预算，要么造出永久盲区。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'chaos' / 'code' / 'runner'
LAYER = ROOT / 'infra' / 'lambda' / 'shared' / 'python'
CHAOS = ROOT / 'chaos' / 'code'
# 必须以 `runner.edge_verification` 形式导入：该模块内部用相对 import，
# 直接把 runner/ 加进 sys.path 再 `import edge_verification` 会报
# 「attempted relative import with no known parent package」。
for p in (LAYER, CHAOS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(scope='module')
def ev():
    from runner import edge_verification
    return edge_verification


@pytest.fixture(scope='module')
def inj():
    from runner import injectability
    return injectability


# ── T-305 ────────────────────────────────────────────────────────────────────

def test_t305_01_token_table_covers_all_observed_reasons(inj):
    """活图谱上实际出现过的六种不可注入理由必须全部登记。

    漏登记一种，那批边就会继续被无意义地重试。
    """
    observed = {
        'chaos-mesh-cannot-target-lambda',
        'target-scaled-to-zero',
        'source-absent-from-cluster',
        'self-loop-from-trace',
        'synthetic-traffic-source',
        'image-repo-dependency',
    }
    missing = observed - set(inj.REASON_TOKEN_CLASS)
    assert not missing, f"这些实测出现过的不可注入理由没登记：{missing}"


def test_t305_02_conditional_reasons_are_not_permanently_excluded(inj):
    """环境条件类理由不得被永久排除。

    副本数为 0 / 源不在集群 都会随环境变化。永久排除会让服务扩容回来后
    这条边再也不被验证 —— 那是自己造的永久盲区，比白重试更糟。
    """
    for token in ('target-scaled-to-zero', 'source-absent-from-cluster'):
        assert inj.REASON_TOKEN_CLASS[token] == inj.PRECONDITION_UNMET
        skip, _ = inj.should_skip_target('Microservice', 'Microservice', f'{token}: x')
        assert skip is False, f'{token} 不得在选边阶段被排除'


def test_t305_03_observer_bound_reasons_are_backend_independent(inj):
    """「没有观测方」类理由与注入后端无关，换任何后端结论都一样。"""
    for token in ('self-loop-from-trace', 'synthetic-traffic-source'):
        assert inj.REASON_TOKEN_CLASS[token] == inj.NO_OBSERVER


def test_t305_04_dead_table_is_gone_not_left_dangling(ev):
    """旧的 `_NON_INJECTABLE_REASONS` / `non_injectable_kind` 必须已删除。

    被矩阵取代后它成了**零生产调用方**的死代码。留着一份「看起来还在生效、
    实际没人调」的判据，正是本项目反复踩的那个坑（写了但没人读）——
    下一个人读到它会以为排除逻辑走的是这张表。
    """
    assert not hasattr(ev, '_NON_INJECTABLE_REASONS'), '旧表应已删除'
    assert not hasattr(ev, 'non_injectable_kind'), '旧函数应已删除'


def test_t305_05_selector_queries_reason_and_labels(ev):
    """选边查询必须取回判定理由**和两端节点类型**。

    类型是矩阵的输入 —— 判「哪个后端能打到它」取决于**类型**而不是名字。
    只取名字的话矩阵拿不到输入。
    """
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    assert "'reason'" in src and "'experiment'" in src, 'project 里缺理由字段'
    assert "'src_label'" in src and "'dst_label'" in src, 'project 里缺节点类型'
    assert '__.outV().label()' in src and '__.inV().label()' in src
    assert 'should_skip_target' in src, '选边函数没有调用矩阵判据'


def test_t305_06_exclusion_happens_before_scoring(ev):
    """排除必须发生在打分之前。

    这批边恰好同时命中两个让它们抢占队首的条件：status=inconclusive
    （pri=2「需重试」）与被旧路径写坏的低 confidence。放在打分之后再过滤，
    会先把它们排到队首再剔掉，limit 截断时挤掉真正该测的边。
    """
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    i_excl = src.find('should_skip_target')
    i_score = src.find("pri = 3")
    assert i_excl != -1 and i_score != -1
    assert i_excl < i_score, '排除逻辑必须在打分之前'


# ── T-305b：后端 × 目标类型 × 注入位置 矩阵 ──────────────────────────────────

def test_t305b_01_matrix_comes_from_catalog_not_hardcoded(inj):
    """矩阵必须从 fault_catalog.yaml 构建，加一条动作应自动跟着变。"""
    m = inj.build_matrix()
    assert set(m) == {'chaosmesh', 'fis'}
    assert m['chaosmesh']['actions'] == 19
    assert m['fis']['actions'] == 36


def test_t305b_02_fis_can_reach_lambda_so_it_is_not_unverifiable(inj):
    """**这是本卡要修的核心缺陷**：Chaos Mesh 打不到 Lambda ≠ 边不可验证。

    FIS 有 3 个 Lambda 动作（aws:lambda:invocation-add-delay / -error /
    -http-integration-response）。T-305 第一版把
    `chaos-mesh-cannot-target-lambda` 判成 permanent，等于把 FIS 能验证的边
    永久排除 —— 那正是「自己造永久盲区」，而我在同一张表里犯了它。
    """
    assert 'LambdaFunction' in inj.build_matrix()['fis']['callee']
    v, _ = inj.injectability('StepFunction', 'LambdaFunction',
                             'chaos-mesh-cannot-target-lambda: ...')
    assert v == inj.INJECTABLE, 'FIS 能打 Lambda，不得判不可达'
    skip, _ = inj.should_skip_target('SNSTopic', 'LambdaFunction', '')
    assert skip is False, 'SNSTopic->LambdaFunction 必须可选为靶点（T-306 前置）'


def test_t305b_03_caller_side_cut_makes_pod_sourced_edges_injectable(inj):
    """源是集群内 Pod 时，可在源侧切断出向流量，目标类型无关。

    依据是 Chaos Mesh NetworkChaos 的 externalTargets（runner 在
    chaos_mcp.py:207-208 真的写进 spec），2026-08-31 首条 petsite->ssm
    就是靠它判 confirmed 的。
    """
    assert inj.build_matrix()['chaosmesh']['caller_side'] is True
    # SQSQueue 没有任何后端能直接打，但源是 Microservice ⇒ 源侧可切
    v, why = inj.injectability('Microservice', 'SQSQueue', '')
    assert v == inj.INJECTABLE and '源侧' in why
    # 源不是 Pod ⇒ 两条轴都不成立
    v2, _ = inj.injectability('BusinessCapability', 'SQSQueue', '')
    assert v2 == inj.UNREACHABLE


def test_t305b_04_image_repo_is_compound_not_unreachable(inj):
    """镜像仓库依赖是「需复合实验」，不是「不可注入」。

    它**可以**注入（源是 Pod，externalTargets 能切），缺的是稳态观测窗口 ——
    配合删 Pod 就是可验证的复合实验。判成不可注入会永久放弃一条本可验证的边。
    """
    v, _ = inj.injectability('Microservice', 'ECRRepository',
                             'image-repo-dependency: ...')
    assert v == inj.NEEDS_COMPOUND
    skip, why = inj.should_skip_target('Microservice', 'ECRRepository',
                                       'image-repo-dependency: ...')
    assert skip is True and '复合实验' in why


def test_t305b_05_precondition_unmet_is_never_permanently_excluded(inj):
    """前置条件不成立的边不得被排除 —— 排除会造出永久盲区。

    副本扩回来 / Pod 重新部署后这条边就该重测。选边器把它们压到最低优先级
    （pri=9）而不是丢掉：有真靶点时永不被选中，队列空了才轮到。
    """
    for token in ('target-scaled-to-zero', 'source-absent-from-cluster'):
        v, _ = inj.injectability('Microservice', 'Microservice', f'{token}: ...')
        assert v == inj.PRECONDITION_UNMET
        skip, _ = inj.should_skip_target('Microservice', 'Microservice', f'{token}: ...')
        assert skip is False, f'{token} 不得在选边阶段被排除'


def test_t305b_06_unmapped_requires_are_declared_not_hidden(inj):
    """`requires` 里图谱无对应节点类型的 ARN 种类必须如实登记。

    这些动作永远选不出靶点 —— 与 test_41 查出的「37 条里 5 条从来不可执行」
    是同一件事。藏起来会让人以为目录里的动作都能用。
    """
    unmapped = inj.build_matrix()['fis']['unmapped_requires']
    for r in ('volume_arns', 'nodegroup_arn', 'asg_arn', 'route_table_arn', 'role_arn'):
        assert r in unmapped, f'{r} 在图谱里没有对应节点类型，应登记为未映射'


def test_t305b_07_token_match_is_prefix_not_substring(inj):
    """轴三的 token 同样只认冒号前的部分。"""
    assert inj.reason_class('self-loop-from-trace: no downstream') == inj.NO_OBSERVER
    mention = 'degradation-observed: not a self-loop-from-trace case'
    assert inj.reason_class(mention) == '', '子串匹配会造成误分类'
    assert inj.reason_class('') == ''
    assert inj.reason_class(None) == ''


def test_t305b_08_three_axes_are_not_conflated(inj):
    """轴三的分类不得与「工具触不到」混为一谈。

    第一版把三件不同的事塞进一张 permanent/conditional 表，导致两条错误结论。
    可观测性问题（no_observer / needs_compound）与注入后端无关，
    换任何后端结论都一样。
    """
    backend_independent = {inj.NO_OBSERVER, inj.NEEDS_COMPOUND, inj.PRECONDITION_UNMET}
    for token, cls in inj.REASON_TOKEN_CLASS.items():
        if token == 'chaos-mesh-cannot-target-lambda':
            assert cls == inj.UNREACHABLE, '工具能力边界应由矩阵裁决'
        else:
            assert cls in backend_independent, (
                f'{token} 被分到了与后端相关的类别，但它与后端无关')


# ── T-307 ────────────────────────────────────────────────────────────────────

def test_t307_01_image_spec_marker_registered(ev):
    """Pod spec 派生的启动依赖必须有自己的观测源标记档位。

    6 条 `微服务 → ECRRepository` 边只写了 source='deepflow-etl' 而没写任何标记，
    于是证据计数器把它们算成零证据（confidence 落到 0.5）——
    `source` 说有观测源、标记说没有。这是「读的人找不到」，与「写了但没人读」
    互为镜像。
    """
    assert 'k8s-image-spec' in ev._OBSERVER_MARKERS
    assert ev._OBSERVER_MARKERS['k8s-image-spec'] == ('image_ref',)


def test_t307_02_image_spec_not_folded_into_deepflow(ev):
    """不得把 image_ref 塞进 deepflow 档，也不得给这类边写 calls/error_rate。

    deepflow 的 ('calls','error_rate') 语义是「eBPF 观测到的流量计数」，
    而这类边来自镜像引用、不存在流量计数。写假的 calls 等于伪造观测证据 ——
    与「采集失败的 fallback 值不得与合法测量值同形」是同一条禁令。
    """
    assert 'image_ref' not in ev._OBSERVER_MARKERS['deepflow']
    assert set(ev._OBSERVER_MARKERS['deepflow']) == {'calls', 'error_rate'}


def test_t307_03_etl_writes_the_marker():
    """写入方（etl_deepflow 的 ECR 启动依赖路径）必须真的写 image_ref。

    只在读取侧登记档位、写入侧不写，等于什么都没修。
    """
    etl = (ROOT / 'infra' / 'lambda' / 'etl_deepflow'
           / 'neptune_etl_deepflow.py').read_text(encoding='utf-8')
    i_edge = etl.find("__.addE('DependsOn').from('s')")
    assert i_edge != -1, '找不到 ECR 启动依赖的建边语句'
    window = etl[i_edge:i_edge + 2000]
    assert "property('image_ref'" in window, 'ECR 启动依赖没有写 image_ref'
    assert "property('calls'" not in window, '不得给 spec 派生边伪造流量计数'
    assert "property('error_rate'" not in window


def test_t307_04_evidence_counter_sees_the_new_marker(ev):
    """带 image_ref 的边必须被算作有 1 个观测源，而不是零证据。"""
    props = {'source': 'deepflow-etl', 'dependency_kind': 'dynamic',
             'phase': 'startup', 'image_ref': 'pet-food-pod'}
    static_n, obs_n, conf_n, ref_n = ev.evidence_from_props(props)
    assert obs_n == 1, f'image_ref 应算 1 个观测源，实得 {obs_n}'
    assert static_n == 0 and conf_n == 0 and ref_n == 0

    import graph_confidence as gc
    assert gc.confidence(static_n, obs_n, conf_n, ref_n) == 0.6225, (
        '有一个观测源时置信度应为 sigmoid(0.5)=0.6225，而不是零证据的 0.5')

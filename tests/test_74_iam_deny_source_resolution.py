"""tests/test_74_iam_deny_source_resolution.py

IAM deny 的**源侧**角色解析。

## 为什么源侧值得单独一组门禁

IAM deny 的做法是给**调用方的角色**加 deny 内联策略。所以它有两个前提，
而 2026-09-15 我在这两个上各栽了一次：

1. **两侧都要判**。第一版第四轴只看目标类型在不在 `SEVERANCE_METHODS`，
   于是 `BusinessCapability -> SQSQueue` 也判成可注入 ——
   而 `BusinessCapability` 是抽象节点、根本没有 IAM 主体。
   `tests/test_47::t305b_03` 抓到了它。

2. **源侧清单只能登记实现真做得到的**。补上源侧判定后我把清单设成
   `POD_BACKED_LABELS`，因为当时只有 `_irsa_role_for`（K8s SA 注解）。
   后来补了 Lambda 与 AgentCore 的解析，清单才扩大。
   顺序不能反 —— 先扩清单后补实现，判定会说"能打"，
   然后在选靶之后、真要加策略那一刻失败。

## 半径纪律：角色必须独占

`_agentcore_role_for` 会逐个核对角色是否被别的运行时共用，共用就拒绝。
共用时加 deny 会连带切掉别的运行时，那就超出了被测的那条边 ——
而"半径恰好一个服务"是这个手段相对网络层切断的核心优势。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
# 导入约定照 `tests/test_47` 的注释（它早就写清了）：
# **必须以 `runner.xxx` 形式导入** —— 该包内模块用相对 import，
# 把 `runner/` 本身加进 sys.path 再 `import xxx` 会报
# 「attempted relative import with no known parent package」。
#
# 所以只加包的父目录 `chaos/code`，另加 Layer（graph_contract 等在那里）。
# 2026-09-15 实测：自插 `chaos/code/runner` 会让 `runner` 优先解析成模块，
# 连带把 test_47 的 18 个 fixture setup 全打成 error，
# 而单独跑各文件都是绿的。
for _p in (ROOT / 'infra' / 'lambda' / 'shared' / 'python',
           ROOT / 'chaos' / 'code'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iam_deny_source() -> str:
    """读脚本源码而**不导入**它。

    导入 `verify_via_iam_deny` 会连带 `from runner import service_names`，
    需要 `runner` 是包；而多数测试把 `chaos/code/runner` 放在 sys.path 上，
    此时 `runner` 解析成模块、它的 `from .experiment import` 会炸。
    症状是单独跑绿、组合跑红 —— 取决于谁先污染了 path。
    """
    return (ROOT / 'scripts' / 'verify_via_iam_deny.py').read_text(
        encoding='utf-8')


def test_t74_01_源侧清单与实现必须一致():
    """`IAM_DENY_SOURCE_LABELS` 里的每个类型，`_execution_role_for` 都要有分派。

    这条守的是"先扩清单后补实现"这个错误顺序 —— 那会让判定说能打、
    真要加策略那一刻才失败。
    """
    import re

    from runner import injectability as inj

    src = _iam_deny_source()
    m = re.search(r'def _execution_role_for[\s\S]*?\n(?=def |\Z)', src)
    assert m, '找不到 _execution_role_for'
    body = m.group(0)
    missing = [lbl for lbl in sorted(inj.IAM_DENY_SOURCE_LABELS)
               if f"'{lbl}'" not in body and f'"{lbl}"' not in body]
    assert not missing, (
        f'IAM_DENY_SOURCE_LABELS 有 {missing}，但 _execution_role_for 里没有'
        f'对应的分派。判定会说这些源"能打"，而真要加 deny 策略时取不到角色。'
        f'\n先在 _execution_role_for 补分派，再往清单里加类型。')


def test_t74_02_抽象节点不得进源侧清单():
    """没有 IAM 主体的节点类型不能作为 IAM deny 的源。"""
    from runner import injectability as inj

    for lbl in ('BusinessCapability', 'AgentTool', 'KnowledgeBase',
                'DynamoDBTable', 'S3Bucket', 'SQSQueue'):
        assert lbl not in inj.IAM_DENY_SOURCE_LABELS, (
            f'{lbl} 进了 IAM deny 的源侧清单 —— 它不是调用主体，'
            f'没有可加 deny 策略的角色。')


def test_t74_03_两侧条件都不满足时理由必须摊开():
    """不可达的理由要说清是哪一侧不满足，否则无从诊断该补什么。"""
    from runner import injectability as inj

    # 目标在能力表内、源无 IAM 主体
    v1, why1 = inj.injectability('BusinessCapability', 'SQSQueue')
    assert v1 == inj.UNREACHABLE
    assert '目标在能力表内' in why1 and '源有可加策略的角色' in why1, (
        f'理由没摊开两侧条件: {why1}')

    # 源有 IAM 主体、目标不在能力表内
    v2, why2 = inj.injectability('AgentRuntime', 'AgentTool')
    assert v2 == inj.UNREACHABLE
    assert 'False' in why2, f'理由里应能看出哪一侧是 False: {why2}'


def test_t74_04_AgentCore与Lambda已在清单内():
    """2026-09-15 补的两支解析，正面钉住它们不被回退。"""
    from runner import injectability as inj

    for lbl in ('LambdaFunction', 'AgentRuntime'):
        assert lbl in inj.IAM_DENY_SOURCE_LABELS, (
            f'{lbl} 不在 IAM deny 源侧清单里。'
            f'它的角色解析已实现（_lambda_role_for / _agentcore_role_for），'
            f'移除它会让一批可验的边重新被判成永久不可达。')


def test_t74_05_共用角色必须拒绝():
    """半径纪律：角色被别的运行时共用时不得施加 deny。

    共用时加 deny 会连带切掉它们，半径超出被测的那条边 ——
    而"恰好一个服务"是本手段相对网络层切断的核心优势。
    """
    import re

    src_all = _iam_deny_source()
    m = re.search(r'def _agentcore_role_for[\s\S]*?\n(?=def |\Z)', src_all)
    assert m, '找不到 _agentcore_role_for'
    src = m.group(0)
    assert 'sharers' in src or '共用' in src, (
        '_agentcore_role_for 没有核对角色是否被共用 —— '
        '共用时加 deny 的半径会超出被测的那条边')
    assert 'return None' in src, '发现共用时必须拒绝（返回 None）而不是继续'


@pytest.mark.neptune
def test_t74_06_Delegates边已回到验证队列(neptune_rca):
    """那 3 条 `Delegates AgentRuntime -> AgentRuntime` 不该再带阻断标注。"""
    rows = neptune_rca.results("""
MATCH (a:AgentRuntime)-[r:Delegates]->(b:AgentRuntime)
WHERE r.verify_blocked_class IS NOT NULL
RETURN a.name AS src, b.name AS dst, r.verify_blocked_class AS k
""")
    assert not rows, (
        f'这些 Delegates 边仍带阻断标注: {rows}\n'
        f'AgentRuntime 已进 IAM deny 源侧清单（_agentcore_role_for 已实现），'
        f'它们应回到验证队列。用 scripts/reclassify_blocked_edges.py --apply 清。')


def test_t74_07_判定器不得import那个脚本():
    """`injectability` 只能**读源码**取能力表，不得 import 它。

    import 会连带 `from runner import service_names`，需要 `runner` 是包；
    而多数测试把 `chaos/code/runner` 放在 sys.path 上，此时 `runner`
    解析成模块，它的 `from .experiment import ...` 立刻炸：

        ImportError: attempted relative import with no known parent package

    这个坑的症状很阴：**单独跑 tests/test_67 或 tests/test_74 都绿，
    组合跑才红**，取决于哪个测试先污染了 sys.path。
    2026-09-15 真踩到过，改成 AST 解析才解决。
    """
    import ast

    raw = (ROOT / 'chaos' / 'code' / 'runner'
           / 'injectability.py').read_text(encoding='utf-8')
    # 只看**真实的 import 语句**，不做字符串匹配 ——
    # 文档注释里为解释历史而提到这个名字是正常的，
    # 判据对准文本而不是语义，是本仓库反复踩过的坑。
    tree = ast.parse(raw)
    imported = {
        (n.module or '') if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n.names[0]])
    }
    src = raw
    assert 'verify_via_iam_deny' not in imported, (
        'injectability.py 在 import verify_via_iam_deny —— '
        '那会引入 runner 包/模块的解析歧义，造成"单独跑绿、组合跑红"。'
        '请改用 ast 只读源码（见 iam_deny_targets 的注释）。')
    assert 'ast.parse' in src, (
        'iam_deny_targets 应当用 ast.parse 读 SEVERANCE_METHODS')

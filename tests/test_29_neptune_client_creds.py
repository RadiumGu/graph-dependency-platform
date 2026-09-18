"""
test_29_neptune_client_creds.py — Neptune 客户端的凭证与连接复用

覆盖测试清单:N-01 ~ N-04

## 背景:T-022 的三条前提有两条经不起实测

原卡片主张把 6 份 `neptune_client*.py` 收敛成一个 SDK，理由三条。实测后:

| 卡片声称 | 实测结论 |
|---|---|
| 消除 6 份重复 | 重复真实存在，但各客户端在查询语言（Gremlin vs openCypher）、GraphSON 解析、Lambda layer 约束上有实质差异 |
| 把 query_guard 给到 chaos / dr-plan | **不成立**。query_guard 是防**LLM 生成 Cypher** 的；chaos 与 dr-plan 只发手写静态查询。且 chaos 有写路径（neptune_sync），加只读守卫会直接拦掉它 |
| 修 chaos 连接不复用 | 成立，但客户端互比只差 6.7 ms/次 —— 不足以支撑 6 文件重构 |

所以 6 路收敛被降范围。但排查过程找到两个**比重构更有价值**的问题。

## 发现 1:共享 Lambda layer 永久缓存冻结凭证（潜伏缺陷）

`infra/lambda/shared/python/neptune_client_base.py` 原本:

    _frozen_creds = None
    def _get_creds():
        global _frozen_creds
        if _frozen_creds is None:
            _frozen_creds = ...get_frozen_credentials()
        return _frozen_creds

`get_frozen_credentials()` 返回含固定 session token 的**不可变快照**。
Lambda 容器可复用数小时，而执行角色凭证有有效期 —— 过期后该热容器的每次
Neptune 调用都会 403，直到容器被回收。三个 ETL 都用这个 layer。

如实说明:近 7 天 ETL 日志里**没有**观测到 403 / ExpiredToken，
所以这是「明确写错但在观测窗口内尚未触发」的潜伏缺陷。

## 发现 2:每次调用新建 boto3 Session（已确认的实况开销）

四份客户端、6 个调用点**全部**在函数体内 `boto3.Session()`。
生产 ETL 日志佐证:同一次调用（同一 request ID）2 秒内出现 4 次
「Found credentials in environment variables」。

实测每次查询耗时:

    rca    32.8 ms → 16.8 ms   (-49%)
    chaos  39.5 ms → 15.9 ms   (-60%)

**方法教训**:一开始把两个客户端互相比较，只看到 6.7 ms 之差 ——
因为它们**共有**这个开销，互比把共同缺陷掩盖了。只有单独测
`boto3.Session()` 的绝对成本（9.7 ms）才暴露出来。

## 正确模式（本文件断言的就是它）

缓存 **Session**（构造昂贵），每次调用**重新冻结**凭证
（boto3 可刷新凭证会在临近过期时自动续期）。
原 layer 恰好两者都反了:Session 每次新建，冻结凭证永久缓存。
"""
import importlib
import os

import pytest

from paths import PROJECT_ROOT


def _load(rel: str, name: str):
    """加载指定客户端模块。

    chaos/code/runner/neptune_client.py 内有**相对导入**（`from .config import ...`），
    按裸文件路径加载会报 `attempted relative import with no known parent package`，
    所以它必须作为包的一部分导入。这里按需分流，而不是硬套一种加载方式。
    """
    import importlib
    import importlib.util
    import sys

    if 'chaos' in rel:
        chaos_code = os.path.join(PROJECT_ROOT, 'chaos', 'code')
        if chaos_code not in sys.path:
            sys.path.insert(0, chaos_code)
        return importlib.import_module('runner.neptune_client')

    spec = importlib.util.spec_from_file_location(
        name, os.path.join(PROJECT_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLIENTS = [
    ('rca/neptune/neptune_client.py', '_rca_nc'),
    ('dr-plan-generator/graph/neptune_client.py', '_dr_nc'),
    ('chaos/code/runner/neptune_client.py', '_chaos_nc'),
]


@pytest.mark.parametrize("rel,name", CLIENTS)
def test_n01_session_is_reused(rel, name):
    """N-01: boto3 Session 必须被复用（两次调用拿到同一个 Session 对象）。

    每次新建 Session 实测约 9.7 ms —— 按 RCA 单次运行 15-30 次查询估，
    纯浪费 150-300 ms，而 RCA 在事故热路径上。
    """
    mod = _load(rel, name)
    assert hasattr(mod, '_get_frozen_creds'), f"{rel} 未提供 _get_frozen_creds"
    mod._get_frozen_creds()
    s1 = mod._boto_session
    mod._get_frozen_creds()
    s2 = mod._boto_session
    assert s1 is not None, "Session 未被缓存"
    assert s1 is s2, "Session 每次调用都被重建 —— 未复用"


@pytest.mark.parametrize("rel,name", CLIENTS)
def test_n02_frozen_creds_are_not_cached(rel, name):
    """N-02: 冻结凭证**不能**被缓存 —— 每次调用必须重新冻结。

    冻结凭证是含固定 session token 的快照。缓存它会让长生命周期进程
    在凭证过期后持续 403。boto3 的可刷新凭证在临近过期时自动续期，
    所以每次重新冻结才是正确做法。
    """
    mod = _load(rel, name)
    c1 = mod._get_frozen_creds()
    c2 = mod._get_frozen_creds()
    assert c1 is not c2, (
        "两次调用返回了同一个冻结凭证对象 —— 说明快照被缓存了，"
        "凭证过期后会持续 403"
    )
    # 未过期时内容应一致（证明不是拿到了错误的凭证）
    assert c1.access_key == c2.access_key


def test_n03_shared_layer_follows_same_pattern():
    """N-03: 共享 Lambda layer 也必须遵守同一模式。

    三个 ETL 都用这个 layer，它原本是「Session 每次新建 + 冻结凭证永久缓存」
    —— 两者都反了。
    """
    mod = _load('infra/lambda/shared/python/neptune_client_base.py', '_layer_nc')
    assert not hasattr(mod, '_frozen_creds') or mod.__dict__.get('_frozen_creds') is None, (
        "layer 仍保留 _frozen_creds 全局缓存"
    )
    c1 = mod._get_creds()
    c2 = mod._get_creds()
    assert c1 is not c2, "layer 仍在缓存冻结凭证快照"
    assert mod._boto_session is not None, "layer 未复用 Session"


def test_n04_query_guard_not_applied_to_write_paths():
    """N-04: query_guard **不能**被塞进客户端层。

    这是 T-022 降范围的关键理由之一，值得用测试固定下来:
    query_guard 是防**LLM 生成 Cypher** 的只读校验。而 chaos 的
    neptune_sync 通过同一个客户端**写**图谱（ChaosExperiment 节点 +
    TestedBy 边）。把守卫下沉到客户端会直接拦掉这条合法写路径。

    断言方式:确认 query_guard 会拒绝 chaos 实际使用的写查询 ——
    即「若下沉到客户端则必然破坏写入」。
    """
    import sys
    rca_dir = os.path.join(PROJECT_ROOT, 'rca')
    if rca_dir not in sys.path:
        sys.path.insert(0, rca_dir)
    from neptune import query_guard

    write_cypher = (
        "MERGE (e:ChaosExperiment {experiment_id: 'x'}) "
        "SET e.result = 'passed'"
    )
    safe, reason = query_guard.is_safe(write_cypher)
    assert not safe, (
        "query_guard 应拒绝写操作 —— 若它被下沉到客户端层，"
        "chaos 的 neptune_sync 写入会被拦掉"
    )
    assert reason, "拒绝时应给出原因"

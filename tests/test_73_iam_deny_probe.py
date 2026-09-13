"""tests/test_73_iam_deny_probe.py — IAM deny 切断实验器的纪律

`scripts/verify_via_iam_deny.py` 会给线上服务的 IRSA 角色加 deny 策略。它是本仓库
唯一会**主动让线上服务失去权限**的工具，所以它的纪律必须可校验。

四条约束，对应 2026-09-13 首发实验里实际踩到的四个坑：

    t73_01  方法表必须声明式（不许在流程里内联 if/else 决定切断手段）
    t73_02  回滚必须在 finally 里，且必须包含凭证刷新
    t73_03  基线闸门必须存在（请求数下限 + 成功率下限）
    t73_04  判定不得用 max 统计业务退化，且混淆时必须拒绝出结论
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "verify_via_iam_deny.py"


def _src() -> str:
    assert _SCRIPT.exists(), "找不到 %s" % _SCRIPT
    return _SCRIPT.read_text(encoding="utf-8")


def test_t73_01_方法表必须声明式():
    """「哪类目标用什么切断手段」必须是一张可审计的表，不是散落的分支。

    声明式的价值：审计时能一眼看完全部手段与其作用范围。写成
    `if label == 'DynamoDBTable': ... elif ...` 的话，
    要读完整个流程才能知道某类目标会被怎么处理。
    """
    src = _src()
    assert "SEVERANCE_METHODS" in src, "缺少声明式方法表"

    tree = ast.parse(src)
    node = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "SEVERANCE_METHODS":
            node = n.value
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "SEVERANCE_METHODS" for t in n.targets):
            node = n.value
    assert node is not None, "SEVERANCE_METHODS 不是模块级赋值"
    table = ast.literal_eval(node)
    assert table, "方法表为空"
    for label, spec in table.items():
        assert spec.get("actions"), "%s 没有声明 deny 的 action" % label
        assert "arn" in spec, "%s 没有声明资源 ARN 形态" % label
        for a in spec["actions"]:
            assert ":" in a, "%s 的 action %r 不像 IAM action" % (label, a)


def test_t73_02_回滚必须在finally且含凭证刷新():
    """删策略不足以恢复 —— 必须刷新凭证。

    ## 实测依据

    2026-09-13：策略删除后 6 分钟，被测边成功率仍只有 14.08%，petsearch 日志
    5 分钟内 539 条 AccessDenied，业务探针在 26 与 0 之间抖动。AWS 对**已有
    会话**的策略评估有缓存，删策略不会立刻作用到正在跑的 Pod 上。
    滚动重启后连续 8 次探针全部正常。

    少了这一步，实验会给线上留下一个**无界时长**的退化状态，
    而脚本已经退出、没人知道。
    """
    src = _src()
    assert "finally:" in src, "回滚不在 finally 里 —— 异常路径会留下 deny 策略"

    fin = src.index("finally:")
    tail = src[fin:]
    assert "delete-role-policy" in tail, "finally 里没有删除策略"
    assert "rollout" in tail and "restart" in tail, (
        "finally 里没有凭证刷新（rollout restart）。"
        "删策略不足以恢复 —— 已有会话的策略评估有缓存。")
    # 删除失败必须打印手工回滚命令
    assert "手工回滚" in tail, (
        "删除失败时没有给出手工回滚命令。"
        "一个加了 deny 没removed 的服务会一直 403，那是真实故障。")


def test_t73_03_基线闸门必须存在():
    """请求数下限 + 成功率下限，缺一不可。

    请求数下限：打不断一个没在跑的东西，样本不足时退化数字是噪声。
    成功率下限：2026-09-13 实测踩过 —— 连跑两次实验，第二次的「基线」
    成功率只有 56.63%（上一次的 deny 仍在生效），拿坏基线算 delta 无意义。
    """
    src = _src()
    assert "MIN_BASELINE_REQUESTS" in src, "缺少基线请求数下限"
    assert "MIN_BASELINE_SUCCESS_RATE" in src, "缺少基线成功率下限"
    # 两个闸门都必须真的拦（在 apply 之前 return）
    for name in ("MIN_BASELINE_REQUESTS", "MIN_BASELINE_SUCCESS_RATE"):
        i = src.rindex(name)
        seg = src[i:i + 400]
        assert "return 3" in seg, (
            "%s 只定义了没有用于拦截（附近没有 return 3）" % name)
    # 采集失败（ok=False）必须中止
    assert 'if not base["ok"]' in src, (
        "没有处理基线 ok=False。`ok=False / success_rate=100 / requests=0` "
        "这个形态与「真的健康」结构上无法区分。")


def test_t73_04_判定不得用max且混淆时必须拒绝():
    """业务退化必须按退化样本统计，不能用 max。

    ## 实测依据

    第一版用 `max_pets`：故障期采样 [0, 26, 0, 0] 的 max 是 26，
    判定写成「业务未退化」—— 一个未退化样本盖掉了三个退化样本，结论正好反了。
    抖动成因是多 Pod 各自传播策略状态，不是消费方有降级路径。
    """
    src = _src()
    vi = src.index("def _verdict")
    body = src[vi:]

    assert 'biz_during.get("max_pets")' not in body, (
        "判定里用了 max_pets 统计业务退化 —— 一个正常样本会盖掉多个退化样本。")
    assert "pet_counts" in body, "判定没有按逐次采样统计"
    assert "observation_only" in body, (
        "判定里没有 observation_only 出口。信号混淆时必须拒绝出结论 —— "
        "用混淆信号得出的结论会被 DR 影响面分析当真。")
    # confirmed 必须同时要求「注入生效」与「业务退化」两个条件
    assert "edge_drop >= 20 and" in body, (
        "confirmed 的条件里没有同时要求业务侧证据。"
        "只有边退化就写 confirmed 等于只证明了调用失败、没证明业务受损。")

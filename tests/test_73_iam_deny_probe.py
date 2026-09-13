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
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "verify_via_iam_deny.py"


def _src() -> str:
    assert _SCRIPT.exists(), "找不到 %s" % _SCRIPT
    return _SCRIPT.read_text(encoding="utf-8")


def _body_of(src: str, decl: str) -> str:
    """取一个顶层定义的函数体，到**下一个顶层 def 或 class** 为止。

    2026-09-13：这里原来有两种写法，都会误判。
    `src[src.index("def _verdict"):]` 取到文件末尾；按 `"\\ndef "` 找边界的那处
    则会被中间的 `class` 躲过去 —— 脚本后半部分有个限流器类，里面一行
    `self._gap = max(1.0, 60.0 * workers / max(1, rate_per_min))`
    于是被算进 `_biz_degraded` 的函数体，让「不得出现 max」这条断言红了。
    那行是限流间隔计算，与业务退化判定毫无关系。

    门禁误报比漏报更伤：它会让人怀疑门禁本身，下次就倾向于跳过它。
    """
    i = src.index(decl)
    m = re.search(r"\n(?:def |class )", src[i + len(decl):])
    return src[i:i + len(decl) + m.start()] if m else src[i:]


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
    """业务退化必须按每个探针各自的最小值统计，不能用 max。

    ## 实测依据

    第一版用 `max_pets`：故障期采样 [0, 26, 0, 0] 的 max 是 26，
    判定写成「业务未退化」—— 一个未退化样本盖掉了三个退化样本，结论正好反了。
    抖动成因是多 Pod 各自传播策略状态，不是消费方有降级路径。

    ## 判据盯语义，不盯变量名

    第二版这条门禁断言 `_verdict` 里含 `pet_counts` —— 那是当时的实现细节。
    逐样本比较后来搬进了 `_biz_degraded`（为支持多探针），`pet_counts` 随之消失，
    门禁于是红了。**红在这里是对的**（实现变了就该复核），但断言不该钉在变量名上。
    现在盯两件不随重构漂移的事：判定里不得出现 max 取值，
    且必须走 `_biz_degraded` 那条按最小值比的路径。
    """
    src = _src()
    body = _body_of(src, "def _verdict")

    assert "max_pets" not in body and 'get("max' not in body, (
        "判定里用了 max 统计业务退化 —— 一个正常样本会盖掉多个退化样本。")
    assert "_biz_degraded" in body, (
        "判定没有走 _biz_degraded —— 逐样本按最小值比较的判据在那里。")
    assert "observation_only" in body, (
        "判定里没有 observation_only 出口。信号混淆时必须拒绝出结论 —— "
        "用混淆信号得出的结论会被 DR 影响面分析当真。")
    assert "edge_drop >= 20 and" in body, (
        "confirmed 的条件里没有同时要求业务侧证据。"
        "只有边退化就写 confirmed 等于只证明了调用失败、没证明业务受损。")

    dbody = _body_of(src, "def _biz_degraded")
    assert "min(" in dbody, "_biz_degraded 没有按最小值比较"
    assert "max(" not in dbody, "_biz_degraded 出现了 max —— 会盖掉退化样本"


def test_t73_05_探针必须按源服务取且未登记时拒绝():
    """业务探针由**源服务**决定，没登记探针的服务必须拒绝开跑。

    被验证的命题是「这个服务失去这个依赖后，**它的**业务输出坏不坏」。
    退回某个默认探针会产出反向结论：拿 petsite 首页探针测
    `petsite -> SQSQueue` 得到「业务正常」，而 SQS 断了实际影响领养提交。
    """
    src = _src()
    assert "business_probes" in src, "实验器没有接入业务探针注册表"
    assert "probes_for(" in src, "没有按源服务取探针"
    assert "未登记业务探针" in src, (
        "源服务未登记探针时没有拒绝开跑。没有业务证据的 confirmed 是过度声称。")
    assert "def _biz_baseline_ok" in src, "缺少业务探针基线闸门"
    assert "基线抖动" in src, (
        "基线闸门没有拦「探针基线本身抖动」—— 抖动的基线无法与退化区分。")


def test_t73_06_方法表必须带xray类型前缀():
    """图谱用资源名、X-Ray 用服务级抽象，靠类型前缀才能对上。

    实测：队列在图谱里叫 `ServicesEks2-sqspetadoption...`，在 X-Ray 里叫 `SQS`
    （Type=AWS::SQS）—— 按名字永远匹配不上，`collect_edge_flow` 返回 ok=False，
    基线闸门于是拒绝开跑，看起来像「这条边没流量」。
    """
    src = _src()
    assert "xray_types" in src, "方法表缺少 xray_types"
    assert "dst_type_prefixes" in src, (
        "没有把类型前缀传给 collect_edge_flow —— 加了字段不用等于没加。")

    tree = ast.parse(src)
    node = None
    for n in ast.walk(tree):
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "SEVERANCE_METHODS":
            node = n.value
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "SEVERANCE_METHODS" for t in n.targets):
            node = n.value
    table = ast.literal_eval(node)
    for label, spec in table.items():
        assert "xray_types" in spec, (
            "%s 没有声明 xray_types（按名字能匹配就写空元组，"
            "但必须显式声明 —— 缺失与「空」在读代码时无法区分）" % label)

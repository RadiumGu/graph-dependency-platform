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
    # 两个闸门都必须真的拦（在 apply 之前 return）。
    #
    # 判据是「**至少有一处**用于拦截」，不是「任取一处都拦截」——
    # 原先用 `rindex` 取最后一处，而 `MIN_BASELINE_REQUESTS` 后来也被
    # `_verdict` 的完全切断分支用来判断基线是否有量（那里不该 return 3）。
    # 门禁于是指着一处正当用法报违规。这是本项目第五次门禁假阳性，
    # 成因与前四次同族：**判据比意图严/松，都会误报**。
    for name in ("MIN_BASELINE_REQUESTS", "MIN_BASELINE_SUCCESS_RATE"):
        spots = [m.start() for m in re.finditer(re.escape(name), src)]
        assert any("return 3" in src[i:i + 400] for i in spots), (
            "%s 的 %d 处出现里没有任何一处用于拦截（附近没有 return 3）"
            % (name, len(spots)))
    # 采集失败（ok=False）必须中止。
    #
    # ⚠️ 判据盯**语义**不盯字面：2026-09-15 并发会话把它改成了
    # `if not _semantic_only and not base["ok"]` —— 闸门没被删，而是为
    # `AgentRuntime -> AgentRuntime` 这一类**结构性测不出**的边开了一个窄口
    # （委派经 AgentCore 网关，X-Ray 里只有一条合流边，测不出目标粒度），
    # 那类边改用 `probe_waggle` 的语义判据（看回答内容而非状态码）。
    # 那个推理是对的，也与本文件其他地方的原则一致：主通道结构性不可用时
    # 改走有据可查的替代通道，而**不是**放宽闸门。
    #
    # 我第一版把断言钉在字面 `if not base["ok"]` 上，于是指着一处正当改动报违规
    # —— 本会话第四次同族失误（前三次见 t73_04 / t73_10 的说明）。
    assert 'base["ok"]' in src, (
        "没有处理基线 ok=False。`ok=False / success_rate=100 / requests=0` "
        "这个形态与「真的健康」结构上无法区分。")
    # 若存在逃逸口，它必须由**声明式判据**驱动，不能是一个自由开关
    if "_semantic_only" in src:
        assert "def edge_flow_measurable" in src, (
            "有 _semantic_only 逃逸口却没有声明式判据函数 —— "
            "自由开关会被用来绕过任何一次闸门失败，"
            "而窄的声明式判据（哪类边结构性测不出）才是可审计的。")
        assert "AgentRuntime" in src, (
            "逃逸口没有写明它适用于哪类边。范围不明的逃逸口等于没有闸门。")


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


def test_t73_07_完全切断必须被前后夹住才算生效():
    """边从服务图消失时，只有「基线有量 → 消失 → 回滚后重现」三环齐全才算生效。

    ## 为什么需要这条分支

    2026-09-13 实测 `petsite -> SNSTopic`：基线 43 次 → 故障期 ok=False/0 次
    → 回滚后 32 次。原因是 deny 让 SDK 抛异常，而埋点不为失败的 SDK 调用发
    子段，边就整个不出现在服务图里。这是「IAM deny + 这套埋点」的固有性质，
    不是偶发 —— 一律判 observation_only 会让 SNS / SQS / StepFunction
    这一整类边永远不可确认。

    ## 为什么这不是放宽闸门

    闸门的职责是证明**注入真的生效**。「被前后夹住的消失」比成功率下降更强地
    满足它：同一套采集在故障前后两个窗口都看得见这条边，唯独故障期看不见。
    **第三环（回滚后重现）是排除「聚合延迟/采样波动」的那一环**，缺了它就必须
    退回 observation_only —— 单看「消失」与「本窗口没聚合到」无法区分。

    而且业务侧证据仍然必需：完全切断也不能只凭边通道出 confirmed。
    """
    src = _src()
    body = _body_of(src, "def _verdict")

    # 必须检查回滚后窗口，而不是只看故障期 ok=False 就下结论
    assert "post" in body, "_verdict 没有使用回滚后窗口"
    assert ("MIN_BASELINE_REQUESTS" in body), (
        "完全切断分支没有要求基线达到请求数下限 —— "
        "零流量边「消失」毫无意义，它本来就不在跑。")

    # 用 spec_from_file_location 而不是裸 ModuleType + SourceFileLoader：
    # 脚本模块级用到 `__file__`（算 _ROOT），裸模块没有这个属性会 NameError。
    import importlib.util
    spec = importlib.util.spec_from_file_location("vd_t7", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    v = mod._verdict

    base_ok = {"ok": True, "success_rate": 100.0, "total_requests": 43}
    gone = {"ok": False, "success_rate": 100.0, "total_requests": 0}
    post_ok = {"ok": True, "success_rate": 100.0, "total_requests": 32}
    bizb = {"ok": True, "no_probe": False,
            "per_probe": {"adopt": [1, 1, 1], "home": [26, 26, 26]}}
    bizd_broke = {"ok": True, "no_probe": False,
                  "per_probe": {"adopt": [0, 0, 0, 0], "home": [26, 26, 26, 26]}}
    bizd_fine = {"ok": True, "no_probe": False,
                 "per_probe": {"adopt": [1, 1, 1], "home": [26, 26, 26]}}
    bizp = {"recovered": True}

    # 三环齐全 + 业务归零 → confirmed
    verdict, why = v(base_ok, gone, post_ok, bizb, bizd_broke, bizp)
    assert verdict == "confirmed", (verdict, why)
    assert "前后夹住" in why, why

    # 缺第三环（回滚后仍采不到）→ 必须退回 observation_only
    verdict, _ = v(base_ok, gone, {"ok": False, "total_requests": 0},
                   bizb, bizd_broke, bizp)
    assert verdict == "observation_only", verdict

    # 基线量不足（零流量边）→ observation_only，即便业务归零
    verdict, _ = v({"ok": True, "success_rate": 100.0, "total_requests": 3},
                   gone, post_ok, bizb, bizd_broke, bizp)
    assert verdict == "observation_only", verdict

    # 三环齐全但业务未退化 → inconclusive，不得 confirmed
    verdict, why = v(base_ok, gone, post_ok, bizb, bizd_fine, bizp)
    assert verdict == "inconclusive", (verdict, why)
    assert "不写 confirmed" in why, why


def test_t73_10_cron侧必须真的读得到互锁():
    """互锁两侧都要能用：实验器置位，cron 读得到并跳过。

    ## 实测依据：这段判据此前从未生效过

    2026-09-15：cron 里是裸 `import chaos_lock`，**没有设置 `sys.path`**。
    而 cron 运行器用 `spec_from_file_location` 加载脚本，那种方式不会把脚本
    所在目录加进 `sys.path` —— 导入必然 ImportError，然后被
    `except Exception` 静默吞掉，于是实验期照常跑并按设计报警。

    **两侧都坏**：两个新实验器从没置位（t73_09 管这一侧），
    cron 则从来读不到（本条管这一侧）。合起来解释了本轮全部归因不明的告警：
    两轮 Service 黑洞实验共产出 4 条假警报。

    ## 判据

    1. cron 必须显式把自身目录加进 `sys.path` —— 不能依赖执行环境凑巧正确
    2. 读不到互锁时**必须留痕**。原注释写着「宁可多报也不要静默」，
       而代码恰恰是静默的：既不跳过也不说自己读不到。
       静默失效的机制等于不存在的机制。
    """
    # **两个**合成流量 cron 都要检 —— 它们是同一个 bug 的两份拷贝。
    # 只检一个的话，另一个会继续静默失效（waggle cron 实测也是裸 import）。
    crons = [pathlib.Path("~/.kiro/crew/crons/%s" % n).expanduser()
             for n in ("adoption_synthetic_traffic.py",
                       "waggle_synthetic_traffic.py")]
    crons = [c for c in crons if c.exists()]
    if not crons:
        pytest.skip("合成流量 cron 不在此环境")
    for cron in crons:
        _assert_interlock_readable(cron)


def _assert_interlock_readable(cron) -> None:
    src = cron.read_text(encoding="utf-8")

    assert "import chaos_lock" in src, (
        "%s 没有检查实验期互锁" % cron.name)
    # 判据是「**至少有一处**真实导入前设了 sys.path」。
    #
    # 第一版用 `src.index()` 取首次出现，而首次出现落在**注释里**
    # （注释正写着「原实现是裸 import chaos_lock」），于是往前的窗口全是注释、
    # 断言必红。这是本会话第三次同族失误：**切片判据比意图松**
    # （前两次：t73_03 用 rindex 取到正当用法、t73_04 把紧随的 class 圈进函数体）。
    spots = [m.start() for m in re.finditer(r"import chaos_lock", src)]
    assert any("sys.path" in src[max(0, i - 800):i] for i in spots), (
        "cron 在 import chaos_lock 之前没有设置 sys.path —— "
        "cron 运行器用 spec_from_file_location 加载脚本，"
        "那种方式不会把脚本目录加进 sys.path，导入必然失败。")
    # 读不到必须留痕，不能静默
    assert "interlock-unreadable" in src, (
        "读不到互锁时没有留痕 —— 静默失效的机制等于不存在的机制。"
        "本项目正是因此让互锁在两侧都失效了很久而无人发现。")
    # 跳过留痕：领养 cron 有运行日志（_runlog），waggle cron 用 print。
    # 两种都接受 —— 判据是「跳过这件事能被看见」，不是用哪个函数。
    assert ("SKIP interlock" in src or "skipped: chaos experiment" in src), (
        "%s 跳过时没有任何痕迹 —— 无法区分「因互锁跳过」与「根本没跑」。"
        % cron.name)


def test_t73_09_每个注入实验器都必须置位实验期互锁():
    """注入故障的脚本必须置位互锁，且只能有一份互锁实现。

    ## 实测依据

    2026-09-15：新写的 Service 黑洞实验器**完全没置位互锁**，RDS 实验器只调了
    `end()` 从没 `begin()`。黑洞实验的故障窗口里 cron 报出
    「连续 3 次首页都取不到 petId」—— 存证页面是 petsite 的
    `Oops! Something went wrong` 错误页，正是 petsearch 不可达的形态。
    那是一条**实验自己造出来的假警报**。

    **假警报会训练人忽略真警报**，所以这不是卫生问题而是可靠性问题。

    ## 为什么还要求「只有一份实现」

    漏置位不会报错，只会安静地多出假警报 —— 这种失效形状最容易在复制粘贴中
    漂移。所以互锁的取用格式只允许有一处定义（`acquire_chaos_lock`），
    其余脚本必须调它，不得各自 `import chaos_lock` 再自己拼调用。
    """
    root = _SCRIPT.parent
    injectors = {
        "verify_via_iam_deny.py": "IAM deny",
        "verify_via_rds_fault.py": "RDS 故障注入",
        "verify_via_service_blackhole.py": "Service 选择器黑洞",
    }
    impl = (root / "verify_via_iam_deny.py").read_text(encoding="utf-8")
    assert "def acquire_chaos_lock" in impl, "缺少统一的互锁取用函数"
    assert "def release_chaos_lock" in impl, "缺少统一的互锁释放函数"

    for fn, desc in injectors.items():
        p = root / fn
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        assert "acquire_chaos_lock(" in src, (
            "%s（%s）没有置位实验期互锁 —— 故障期 cron 会报假警报，"
            "而假警报会训练人忽略真警报。" % (fn, desc))
        assert "release_chaos_lock(" in src, (
            "%s（%s）没有释放互锁 —— 漏释放会让 cron 一直跳过到标记过期。"
            % (fn, desc))
        if fn != "verify_via_iam_deny.py":
            assert "import chaos_lock" not in src, (
                "%s 自己 import 了 chaos_lock —— 互锁的取用格式只允许一处定义，"
                "各自拼调用就会漂移（漏置位不报错，只是安静地多出假警报）。" % fn)


def test_t73_08_同对多边必须全部写回且绑定切断作用域():
    """同一对节点之间的多条边要么全写、要么全不写，且理由必须写在代码里。

    ## 为什么从「>1 条就拒绝」改成「全部写回」

    2026-09-13 实测 `petsite -> SQSQueue`：同一对节点之间有 2 条边，
    来源不同、都是真的 ——

        DependsOn    source=xray,   dependency_kind=dynamic,
                     xray_call_count=562        观测到的
        PublishesTo  source=aws-etl,
                     evidence=PaymentController.cs#PostMessageToSqs
                                                声明的（已与 X-Ray 交叉核对）

    原先一律拒绝写回，理由是「写错一条边比不写更糟」。那条理由防的是**猜**，
    而这里不需要猜：**IAM deny 的作用域是整个资源**（`sqs:*` on 这个队列），
    切断的是这一对节点之间的全部交互，证据同等覆盖每一条边。

    ## 这条门禁盯的是那个推理前提

    结论绑定于「切断手段是资源级」。若换成更窄的手段（只 deny
    `sqs:SendMessage`），证据就只覆盖发送那条边，必须回到拒绝写回。
    所以要求代码里显式写出这个前提 —— 前提丢了，结论就成了没根据的放宽。
    """
    src = _src()

    # 1) 定位函数必须返回全部命中，而不是 >1 就放弃
    assert "def _edge_ids" in src, (
        "边定位函数没有改成返回多条（_edge_ids）。")
    body = _body_of(src, "def _edge_ids")
    assert "全部" in body, "_edge_ids 没有说明它返回全部同对边"
    # 前提与反例都必须写出来：证据范围绑定于切断手段的作用域
    assert "SendMessage" in body, (
        "_edge_ids 没有写出反例（更窄的切断手段只覆盖一条边），"
        "下一个人无法判断这个推理何时失效。")
    # 命中 0 条仍必须响
    assert "空结果必须响" in body, (
        "找不到边时不再报警。空结果与「名字对不上」在日志里长得一样，"
        "后者会被当成前者放过。")

    # 2) 写回必须遍历全部边，且理由里绑定「资源级」这个前提
    pbody = _body_of(src, "def _persist_verdict")
    assert "for h in hits" in pbody, "写回没有遍历全部边"
    assert ("资源" in pbody and "全部交互" in pbody), (
        "写回全部边的理由没有绑定「IAM deny 作用于整个资源」这个前提。"
        "前提丢了，结论就成了没根据的放宽 —— 换成更窄的切断手段时"
        "必须回到拒绝写回。")
    assert "SendMessage" in pbody or "更窄" in pbody, (
        "没有写出反例（更窄的切断手段），下一个人无法判断这个推理何时失效。")

    # 3) observation_only 仍然不写回
    assert 'verdict not in ("confirmed", "inconclusive")' in pbody, (
        "observation_only 的拦截被改掉了 —— 它的含义是「信号不足以判定」，"
        "写进 verify_status 会让它看起来像结论。")

    # 4) 部分失败必须报出来，不能把 2 条里写成 1 条报成成功
    assert "部分写回失败" in pbody, (
        "多条边部分写回失败时没有单独报告。"
        "2 条里只成功 1 条却报「已写回」，会让报告声称一条无证据的边已确证。")


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

def test_t73_11_cron告警必须自带事件时刻():
    """告警文案必须能自证是哪一刻的事件 —— 否则重放无法分辨。

    `crons.json` 里 `last_result` 只在 raise Report 时写入，成功的轮次既不刷新
    也不清除它。实测 10:32 那轮明明 `last_status=ok`，`last_result_ts` 仍卡在
    10:07:23，于是通知面把一条 10:02 的旧告警在 10:33 当成当前状态重新推了一遍。

    平台字段语义不由本仓库负责，但告警**自带时刻**是脚本侧能做到的，
    做到了就能一眼分辨重放，不必每次去翻存证目录和运行日志。
    """
    import re
    for name in ("adoption_synthetic_traffic.py", "waggle_synthetic_traffic.py"):
        cron = pathlib.Path("~/.kiro/crew/crons/%s" % name).expanduser()
        if not cron.exists():
            continue
        src = cron.read_text(encoding="utf-8")
        assert "def _stamp" in src, "%s 没有事件时刻辅助函数" % name
        # 每一处告警都要戳，漏一处那一处就会被误当成当前状态
        alarms = len(re.findall(r"raise Report\(", src))
        stamped = src.count("{_stamp()}")
        assert stamped >= alarms, (
            "%s 有 %d 处 raise Report 但只有 %d 处带时刻 —— "
            "漏戳的那处重放时无法分辨。" % (name, alarms, stamped))

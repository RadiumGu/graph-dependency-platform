"""停摆告警的 TreatMissingData 必须是 breaching。

## 守的是什么

`infra/tokyo/06-graph-platform-etl-liveness-alarms.yaml` 的 6 条告警回答
「ETL 是不是停了」。而停摆的表现**不是**「指标值为 0」，是
**「指标没有数据点」** —— Lambda 不被调用时 AWS/Lambda 的 Invocations
根本不发布数据。

所以：

    TreatMissingData: notBreaching  →  停摆时告警保持 OK
    TreatMissingData: missing       →  停摆时保持上一状态，也是 OK
    TreatMissingData: breaching     →  停摆时进 ALARM  ✓

把它改成 notBreaching 不会让任何测试失败、不会让部署报错、告警也会显示
一个健康的 OK —— **它只是再也不会叫**。这正是需要一道判据的那类改动。

## 为什么 05 栈的 notBreaching 是对的

同目录的 `05-graph-platform-capacity-alarms.yaml` 里那条 Duration 告警用
`notBreaching`，而且**那是正确的**：它回答「ETL 是不是变慢了」，
没有调用时问这个问题没有意义。

两个栈的 TreatMissingData 相反，恰恰因为它们问的是不同的问题。
所以本判据只管 liveness 栈，不能笼统地要求「所有告警都用 breaching」。

## 这个缺口是怎么被发现的

2026-10-04 一次真实故障：cdk deploy 把一个没装依赖的包部到
gp-window-flush（缺 urllib3，一旦调用就 ModuleNotFoundError），
而它在部署后 50 分钟里**零调用**，于是 Errors 和 Duration 都没有数据点，
盯它们的告警全部保持 OK。最后靠人工全量核对才发现。

见 docs/lessons/cdk-fromasset-packages-ungitted-deps.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cfn_yaml import load_cfn

ROOT = Path(__file__).resolve().parents[1]
LIVENESS = ROOT / "infra" / "tokyo" / "06-graph-platform-etl-liveness-alarms.yaml"
CAPACITY = ROOT / "infra" / "tokyo" / "05-graph-platform-capacity-alarms.yaml"


# ⚠️ CFN 模板的读取用 `cfn_yaml.load_cfn`，不要在这里自己装 YAML 构造器。
#
# 本文件第一版自己写了一个 `add_multi_constructor("!", …)` 的宽松 loader，
# 被 tests/test_97_cfn_loader_single_source.py 挡住了。它的报错说得很准：
#
#     6 份重复里曾有 3 份是错的，重复本身就是那些错误能存在的原因。
#
# 这是**同一轮里第二次**撞上「已有正确实现而我又写了一份」——
# 前一次是 build.sh 的 --platform（rca_window_flush/build.sh 一直做对，
# 而我补写 shared/build.sh 时没去看它）。
# 区别在于这一次有门禁，所以它当场被拦住了。


def _alarms(p: Path) -> dict:
    tpl = load_cfn(p)
    return {
        k: v for k, v in tpl.get("Resources", {}).items()
        if v.get("Type") == "AWS::CloudWatch::Alarm"
    }


LIVENESS_ALARMS = sorted(_alarms(LIVENESS).items()) if LIVENESS.exists() else []


def test_liveness_template_exists():
    """判据要能找到目标 —— 一个什么都没检查的门禁比没有门禁更坏。"""
    assert LIVENESS.exists(), f"{LIVENESS.relative_to(ROOT)} 不存在"
    assert LIVENESS_ALARMS, "模板里没有任何 CloudWatch 告警 —— 解析失效了？"


def test_covers_every_scheduled_etl():
    """6 个定时 ETL 都要有停摆告警。

    这张清单就是覆盖面本身。漏一个等于那个 ETL 停了也没人叫，
    而「手工清单漏项」在本仓已实测三次（见 crons/lambda_drift.py 顶部）。
    """
    functions = set()
    for _lid, a in LIVENESS_ALARMS:
        for d in a["Properties"].get("Dimensions", []):
            if d.get("Name") == "FunctionName":
                functions.add(d["Value"])
    expected = {
        "neptune-etl-from-deepflow",
        "neptune-etl-from-aws",
        "neptune-etl-from-appsignals",
        "neptune-etl-from-agentcore",
        "neptune-etl-from-xray",
        "neptune-etl-from-cfn",
    }
    assert expected <= functions, f"缺停摆告警的 ETL: {sorted(expected - functions)}"


@pytest.mark.parametrize("lid,alarm", LIVENESS_ALARMS, ids=[x[0] for x in LIVENESS_ALARMS])
def test_treat_missing_data_is_breaching(lid: str, alarm: dict):
    """核心判据。

    改成 notBreaching 不会让部署报错、告警还会显示健康的 OK ——
    它只是再也不会叫。
    """
    tmd = alarm["Properties"].get("TreatMissingData")
    assert tmd == "breaching", (
        f"{lid} 的 TreatMissingData 是 {tmd!r}。\n"
        f"  停摆的表现是「指标没有数据点」而不是「指标值为 0」——\n"
        f"  Lambda 不被调用时 Invocations 根本不发布数据。\n"
        f"  notBreaching / missing 都会让这条告警在停摆时保持 OK，\n"
        f"  也就是再也不会叫，而部署和告警面板都不会提示任何异常。"
    )


@pytest.mark.parametrize("lid,alarm", LIVENESS_ALARMS, ids=[x[0] for x in LIVENESS_ALARMS])
def test_metric_is_invocations(lid: str, alarm: dict):
    """盯的必须是 Invocations。

    Errors / Duration 都不行：它们在零调用时同样没有数据点，
    而那正是 2026-10-04 那次故障没被任何告警抓到的原因。
    """
    p = alarm["Properties"]
    assert p.get("MetricName") == "Invocations", (
        f"{lid} 盯的是 {p.get('MetricName')!r}。Errors/Duration 在零调用时"
        f"也没有数据点，抓不到停摆"
    )
    assert p.get("Statistic") == "Sum", (
        f"{lid} 用的是 {p.get('Statistic')!r}。停摆判据是「窗口内一次都没有」，"
        f"只有 Sum 能表达这个"
    )
    assert p.get("ComparisonOperator") == "LessThanThreshold"
    assert float(p.get("Threshold")) == 1.0, (
        f"{lid} 阈值是 {p.get('Threshold')} —— 应为 1（Sum < 1 即零调用）"
    )


@pytest.mark.parametrize("lid,alarm", LIVENESS_ALARMS, ids=[x[0] for x in LIVENESS_ALARMS])
def test_has_alarm_actions(lid: str, alarm: dict):
    """必须有 AlarmActions。

    2026-10-02 实测：线上既有告警（全是 ApplicationInsights 自动创建的）
    的 AlarmActions **全是 None** —— 建了但没人收。
    一条没有 action 的告警是「失败长得像成功」的标准形态。
    """
    p = alarm["Properties"]
    assert p.get("AlarmActions"), f"{lid} 没有 AlarmActions —— 建了但没人收"
    assert p.get("OKActions"), (
        f"{lid} 没有 OKActions —— 恢复时不通知，人就不知道它好了，"
        f"下次再响时也无从判断是新问题还是老问题没解决"
    )


@pytest.mark.parametrize("lid,alarm", LIVENESS_ALARMS, ids=[x[0] for x in LIVENESS_ALARMS])
def test_window_expects_multiple_invocations(lid: str, alarm: dict):
    """窗口必须足够长，覆盖多次预期调用。

    窗口 = Period × EvaluationPeriods。取太短会让单次偶发失败
    （节流、冷启动超时）立刻叫人，而那种误报会训练人忽略整个频道 ——
    与 test_121 记的「永远红着的告警」同一个机制，只是方向相反。

    实测调度（2026-10-06，7 天）：
        deepflow   rate(5 minutes)    288/日
        aws        rate(15 minutes)    96/日
        appsignals rate(15 minutes)    96/日
        agentcore  rate(15 minutes)    96/日
        xray       rate(1 hour)        24/日
        cfn        cron(0 18) daily     1/日
    """
    interval = {           # 函数 → 调用间隔（秒），实测
        "neptune-etl-from-deepflow": 300,
        "neptune-etl-from-aws": 900,
        "neptune-etl-from-appsignals": 900,
        "neptune-etl-from-agentcore": 900,
        "neptune-etl-from-xray": 3600,
        "neptune-etl-from-cfn": 86400,
    }
    p = alarm["Properties"]
    fn = next(
        d["Value"] for d in p["Dimensions"] if d["Name"] == "FunctionName"
    )
    window = int(p["Period"]) * int(p["EvaluationPeriods"])
    expected = window / interval[fn]
    assert expected >= 2, (
        f"{lid} 窗口 {window}s / 调用间隔 {interval[fn]}s = 期望 {expected:.1f} 次。\n"
        f"  少于 2 次意味着一次偶发失败就会叫人。"
    )
    # 低频函数（cfn）做不到 3 次 —— Period 上限 86400，拉到 3 天检测太慢。
    if interval[fn] < 86400:
        assert expected >= 3, (
            f"{lid} 期望只有 {expected:.1f} 次。定时间隔 {interval[fn]}s 的函数"
            f"应当覆盖 ≥3 次 —— 连续 3 个周期都没有才不是偶发"
        )


class TestCapacityStackKeepsItsOppositeSetting:
    """05 栈的 notBreaching 必须保留 —— 两者相反是有理由的。

    有人看到本判据后可能去把 05 也改成 breaching「保持一致」。
    那会让 Duration 告警在每个没有调用的周期里报警 —— 而 ETL 是定时 +
    事件驱动的，缺数据是常态。
    """

    def test_duration_alarm_still_not_breaching(self):
        alarms = _alarms(CAPACITY)
        dur = [
            a for a in alarms.values()
            if a["Properties"].get("MetricName") == "Duration"
        ]
        assert dur, "05 栈里找不到 Duration 告警"
        for a in dur:
            assert a["Properties"].get("TreatMissingData") == "notBreaching", (
                "05 的 Duration 告警不该是 breaching。它回答「是不是变慢了」，"
                "没有调用时问这个问题没有意义 —— 改了会在每个空周期误报。"
            )

    def test_the_difference_is_documented(self):
        """两个栈设置相反的理由必须写下来，否则下一个人会去「统一」它们。"""
        txt = LIVENESS.read_text(encoding="utf-8")
        assert "notBreaching" in txt, "没有解释 05 为什么用 notBreaching"
        assert "不同的问题" in txt or "不同的问题" in txt

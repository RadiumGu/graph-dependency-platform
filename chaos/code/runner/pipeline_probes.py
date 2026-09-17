"""内部数据管道类依赖的判据 —— 产出物新鲜度，而不是下游用户功能。

## 为什么需要另一套判据

`business_probes.py` 里 9 个探针全部打的是**面向用户的服务**
（petsite / petsearch / payforadoption / 4 个 WaggleAI 运行时 …）。
它们的判据是"用户功能是否还работ"。

但图谱里有一类依赖**根本没有用户侧消费方**：

    LambdaFunction neptune-etl-from-xray -> NeptuneCluster petsite-neptune
    LambdaFunction neptune-etl-from-agentcore -> NeptuneCluster petsite-neptune

ETL 的消费者是**图谱自己**。切断它，没有任何用户功能会变化 ——
下一次有人查图谱时才会发现数据是旧的。拿业务探针去验它，
只会得到"业务未退化"，而那**不是**"依赖不承重"，
是**观测方选错了**（本项目台账里同一个错误的第 N 次）。

## 两个必须一起解决的问题

**一、观测方**：产出物的新鲜度，不是用户功能。
对 ETL 就是它写进图谱的那批边的 `last_seen` 是否在推进。

**二、触发时机**：这类源是定时任务，周期常常远大于实验窗口。
实测 `neptune-etl-from-xray` 是 `rate(1 hour)`、近 1h 仅 1 次调用，
而实验窗口 180s —— **窗口内它根本不会被调用**。
等周期到点意味着实验要跑一小时以上，而且期间任何别的变更都会污染结论。

所以这类边必须**主动触发**：加 deny 之后直接 invoke 一次源，
看它是否失败、产出物是否停止推进。

主动触发的前提是**幂等**。`neptune-etl-from-xray` 满足：
它从 X-Ray 读一个时间窗、按 `last_seen` upsert 到图谱，
多跑一次只会把同一批边的时间戳再刷一遍。
⚠️ 不幂等的源不得用这个手段 —— 那会把混沌实验变成数据损坏。

## 判定语义（与 business_probes 保持一致的口径）

`value=1` 表示产出物在推进（健康），`value=0` 表示停滞。
`ok=False` 表示**探针本身失败**（查不到数据），
它与 `value=0` 必须分开 —— 拿"查不到"当"停滞"会造出假 confirmed。
"""

from __future__ import annotations

import time


#: 产出物被认为"新鲜"的上限（秒）。
#:
#: 取值依据：`neptune-etl-from-xray` 是 `rate(1 hour)`，
#: 所以正常情况下 `last_seen` 最旧也就落后一个周期。
#: 给 2 倍余量避免把"刚好卡在周期边界"判成停滞 ——
#: 这类误判会产出假 confirmed，比漏判更糟。
FRESH_WITHIN_SECONDS = 2 * 3600

#: 主动触发后等产出物落地的预算（秒）。
#: ETL 自己跑完 + 写图谱 + 图谱可见，实测个位数秒级，给足余量。
TRIGGER_SETTLE_SECONDS = 90


def _newest_last_seen(nc, source: str) -> int | None:
    """该 ETL 写进图谱的边里，最新的 `last_seen`。

    用 `max()` 而不是 `count()`：边的**数量**在稳态下不变，
    切断 ETL 不会让边消失（图谱是 upsert 而非重建），
    所以数量对这件事完全不敏感。**时间戳才是产出物的活性信号。**
    """
    rows = nc.results(
        "MATCH ()-[r]->() WHERE r.source = $src AND r.last_seen IS NOT NULL "
        "RETURN max(r.last_seen) AS newest", {"src": source})
    if not rows:
        return None
    v = rows[0].get("newest")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def probe_pipeline_freshness(nc, source: str,
                             fresh_within: int = FRESH_WITHIN_SECONDS) -> dict:
    """产出物新鲜度探针。

    `nc` 是 neptune 客户端（调用方注入，本模块不自己建连 ——
    建连逻辑在 rca/neptune 里，复制一份就会漂移）。
    `source` 是边上的 `source` 属性值，例如 `'xray'`。
    """
    newest = _newest_last_seen(nc, source)
    if newest is None:
        return {"ok": False, "value": 0,
                "detail": "查不到 source=%s 的 last_seen —— "
                          "探针失败，不是产出停滞" % source}
    age = int(time.time()) - newest
    fresh = age <= fresh_within
    return {"ok": True, "value": 1 if fresh else 0, "age_seconds": age,
            "newest": newest,
            "detail": "source=%s 最新产出 %ds 前（阈值 %ds）→ %s"
                      % (source, age, fresh_within,
                         "新鲜" if fresh else "停滞")}


def trigger_and_observe(nc, source: str, invoke,
                        settle: int = TRIGGER_SETTLE_SECONDS) -> dict:
    """主动触发一次源，看产出物有没有推进。

    这是这类边的**核心判据**：不等周期，直接问"现在还能不能写"。

    `invoke` 是一个可调用对象，由调用方提供（对 Lambda 源就是
    `lambda: boto3.client('lambda').invoke(FunctionName=...)`）——
    本模块不碰具体后端，否则又要在这里维护一套源类型分派。

    返回：
      `advanced=True`  产出物时间戳前进了 -> 管道通
      `advanced=False` 没前进 -> 管道断（结合 invoke 的错误更确定）
      `ok=False`       探针或触发本身失败 -> 不出判定

    ⚠️ `invoke` 成功但产出物没前进，与 `invoke` 失败，
    是**两种不同的证据**，都记下来：前者可能是源自己吞了错误
    （本项目已经在 agent 委派上被这种吞错骗过一次）。
    """
    before = _newest_last_seen(nc, source)
    if before is None:
        return {"ok": False, "advanced": False,
                "detail": "触发前取不到基线 last_seen —— 拒绝出判定"}

    invoke_error = None
    invoke_payload = None
    try:
        invoke_payload = invoke()
    except Exception as exc:            # noqa: BLE001 —— 要把错误当证据
        invoke_error = str(exc)

    time.sleep(settle)
    after = _newest_last_seen(nc, source)
    if after is None:
        return {"ok": False, "advanced": False,
                "invoke_error": invoke_error,
                "detail": "触发后取不到 last_seen —— 探针失败"}

    advanced = after > before
    return {"ok": True, "advanced": advanced,
            "before": before, "after": after,
            "invoke_error": invoke_error,
            "invoke_payload": invoke_payload,
            "detail": "产出物 last_seen %s（%d -> %d）%s"
                      % ("前进" if advanced else "未前进", before, after,
                         "；invoke 报错: %s" % invoke_error
                         if invoke_error else "；invoke 未报错")}


#: 图谱里"内部数据管道"类依赖 -> 它的产出物 `source` 值。
#:
#: 只登记**已核实幂等**的源 —— 不幂等的源用主动触发会损坏数据。
#: 这条纪律与 SEVERANCE_METHODS 那张表同源：
#: 登记做不到（或做了有害）的条目比不登记更糟。
PIPELINE_OUTPUTS = {
    # 从 X-Ray 读时间窗、按 last_seen upsert 到图谱 —— 幂等已核实。
    "neptune-etl-from-xray": "xray",
    # 同一形态（读 AgentCore 控制面 + 运行时日志，upsert）。
    "neptune-etl-from-agentcore": "agentcore-etl",
}


def output_source_for(service: str) -> str | None:
    """源服务名 -> 它的产出物 source 值。未登记返回 None。

    返回 None 的含义是"**还没核实过它幂等**"，
    不是"它没有产出物" —— 调用方不得据此认为这类边不可验。
    """
    return PIPELINE_OUTPUTS.get(service)

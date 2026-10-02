"""PetSite 的两个「没人看着」的缺口，做成定时检查。

两件事都来自 2026-09/10 的实测教训，不是假想的风险：

1. **alarm_watchdog** —— `petadoptions-adoption-success-ratio-low` 从
   2026-09-27T03:53 起连续 ALARM **5 天**没人处理。它接的是既有 Slack 通道，
   所以"告警发出去了"；缺的是**有人回来追**。CloudWatch 本身没有
   「已持续 N 小时」这种条件，所以只能定时查。

2. **bypass_survey** —— `Servic-PetSi` 443 上的 `prio 3` 规则凭一个静态头
   绕过 Cognito，且**没有路径条件**（整站可绕）。收窄它必须先枚举消费者，
   而 ALB 访问日志 2026-10-02 之前是关的。现已开启（前缀 petsite-443）。

   ⚠️ ALB 访问日志**不记请求头**，所以不能直接搜那个头。判据是
   `matched_rule_priority`：匹配到 1/2/3 的请求就是走了旁路规则。
   配合 `actions_executed` 交叉验证 —— 走旁路时不会出现 `authenticate`。
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import io
import re

import boto3

REGION = "ap-northeast-1"
LOG_BUCKET = "openclaw-alb-logs-1770913299"
LOG_PREFIX = "petsite-443/"

# 这三条是旁路规则（见 docs/audits/alb-443-rule-inventory.md）
BYPASS_PRIORITIES = {"1", "2", "3"}

WATCHED_ALARMS = [
    "petadoptions-adoption-success-ratio-low",
    "petsite-frontdoor-no-healthy-target",
    "petadoptions-history-queue-not-consumed",
]

# 超过这个小时数仍在 ALARM，就当成「没人在处理」而再提醒一次。
STALE_HOURS = 4


def alarm_watchdog(ctx):
    """持续 ALARM 超过 STALE_HOURS 的告警，重新提醒一次。"""
    cw = boto3.client("cloudwatch", region_name=REGION)
    res = cw.describe_alarms(AlarmNames=WATCHED_ALARMS)
    now = dt.datetime.now(dt.timezone.utc)

    stale = []
    for a in res.get("MetricAlarms", []):
        if a["StateValue"] != "ALARM":
            continue
        hours = (now - a["StateUpdatedTimestamp"]).total_seconds() / 3600
        if hours >= STALE_HOURS:
            stale.append((a["AlarmName"], hours, a.get("StateReason", "")[:200]))

    if not stale:
        # 不发无事通知 —— 无事通知会让人学会忽略这个频道。
        from kiro_crew.cron_script import Skip

        raise Skip("没有持续超时的告警")

    lines = ["⚠️ 以下告警已持续 ALARM 且无人处理："]
    for name, hours, reason in stale:
        lines.append(f"  • {name} —— 已 {hours:.1f} 小时")
        lines.append(f"    {reason}")
    lines.append("")
    lines.append("参考：adoption-success-ratio 那条在 2026-09-27 响了 5 天没人看，")
    lines.append("根因是 traffic-generator 调 /housekeeping/ 时少了 userId（302 被当成成功）。")

    from kiro_crew.cron_script import Report

    raise Report("\n".join(lines))


def _iter_log_lines(hours_back: int):
    """拉取最近 N 小时的 ALB 访问日志行。"""
    s3 = boto3.client("s3", region_name=REGION)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_back)

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=LOG_BUCKET, Prefix=LOG_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # ⚠️ ALB 在开启日志时会写一个**明文**的 ELBAccessLogTestFile，
            #    不过滤它 gzip 会抛 BadGzipFile（2026-10-02 预演时实际踩到）。
            if not key.endswith(".log.gz"):
                continue
            if obj["LastModified"] < cutoff:
                continue
            body = s3.get_object(Bucket=LOG_BUCKET, Key=key)["Body"].read()
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
                for raw in fh:
                    yield raw.decode("utf-8", "replace")


def bypass_survey(ctx):
    """枚举走了旁路规则的请求，为「收窄 prio 3」提供判据。"""
    hours = 24
    by_prio = collections.Counter()
    detail = collections.defaultdict(collections.Counter)
    total = 0

    # matched_rule_priority 紧跟在 chosen_cert_arn 之后。
    # 用正则锚定 ACM ARN，比按空格数数稳 —— user_agent 里可能含空格。
    pat = re.compile(r'"arn:aws:acm:[^"]+"\s+(\S+)\s')
    req = re.compile(r'"(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|-) (\S+) [^"]*"')
    acts = re.compile(r'"((?:waf|authenticate|forward|redirect|fixed-response|,)+)"')

    for line in _iter_log_lines(hours):
        total += 1
        m = pat.search(line)
        if not m:
            continue
        prio = m.group(1)
        by_prio[prio] += 1
        if prio in BYPASS_PRIORITIES:
            u = req.search(line)
            path = (u.group(1).split("?")[0] if u else "?")[:80]
            src = line.split(" ")[3].split(":")[0] if len(line.split(" ")) > 3 else "?"
            a = acts.search(line)
            detail[prio][f"{path} | src={src} | actions={a.group(1) if a else '?'}"] += 1

    bypass_total = sum(n for p, n in by_prio.items() if p in BYPASS_PRIORITIES)

    from kiro_crew.cron_script import Report, Skip

    if total == 0:
        raise Skip("近 24 小时没有访问日志（ALB 可能刚开启日志或无流量）")

    lines = [
        f"PetSite 443 旁路使用面调查（近 {hours} 小时，共 {total} 条请求）",
        "",
        f"走旁路规则（prio 1/2/3）的请求：**{bypass_total}** 条",
        "",
        "按规则优先级分布：",
    ]
    for p, n in sorted(by_prio.items(), key=lambda x: -x[1])[:8]:
        tag = "  ← 旁路" if p in BYPASS_PRIORITIES else ""
        lines.append(f"  prio={p:>4}  {n:6} 条{tag}")

    if bypass_total:
        lines += ["", "旁路请求明细（收窄 prio 3 的判据）："]
        for p in sorted(detail):
            lines.append(f"  prio {p}:")
            for k, n in detail[p].most_common(10):
                lines.append(f"    {n:5}x  {k}")
        lines += [
            "",
            "→ 若 prio 3 的路径集中在少数几个，就可以给它补上路径条件，",
            "  把「整站可绕」降为「这几个路径可绕」。",
        ]
    else:
        lines += [
            "",
            "→ 近 24 小时**没有任何请求走旁路规则**。",
            "  若连续几天都是 0，prio 3 可以直接删除而不是收窄 ——",
            "  但删除前要确认不是因为消费者恰好停了。",
        ]

    raise Report("\n".join(lines))

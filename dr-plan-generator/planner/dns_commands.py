"""
planner/dns_commands.py — Route 53 命令构造

为什么单独一个模块
------------------
原实现在 ``plan_generator``（降 TTL）与 ``step_builder``（切流量）两处各写一份
Route 53 命令，且**两处都不可执行**：

1. change-batch 只给了 ``{"Action":"UPSERT","ResourceRecordSet":{"TTL":60}}``，
   缺 ``Name`` / ``Type`` / ``ResourceRecords`` 三个必填字段。直接跑会被
   Route 53 以 ``InvalidChangeBatch`` 拒掉——演练时这一步必然卡住。
2. ``--hosted-zone-id $ZONE_ID`` 是个**未绑定的 shell 变量**。未设置时展开成空串，
   命令变成 ``--hosted-zone-id`` 后面直接跟 ``--change-batch``，参数解析错位。

放在一处的好处是这两个坑只需要修一次，而且改 DNS 是切换里最危险的一步
（TTL 缓存意味着改错要等 TTL 过期才能纠正），值得有单独的测试覆盖。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: hosted zone 未配置时输出的占位符。刻意用尖括号包起来：
#: 它在 shell 里**不会**被静默展开成空串，执行时会立刻报错，
#: 而不是像 ``$ZONE_ID`` 那样悄悄错位。
ZONE_ID_PLACEHOLDER = "<ROUTE53_HOSTED_ZONE_ID:未配置>"


def resolve_zone_id(profile: Optional[Any]) -> Tuple[str, bool]:
    """解析 hosted zone id。

    Args:
        profile: DRProfile 或 None。

    Returns:
        ``(zone_id, is_configured)``。未配置时返回可辨识的占位符。
    """
    if profile is not None:
        try:
            zone = profile.dns_hosted_zone_id
            if zone:
                return zone, True
        except Exception:  # noqa: BLE001
            pass
    return ZONE_ID_PLACEHOLDER, False


def _change_batch(
    record_name: str,
    record_type: str,
    ttl: int,
    values: List[str],
) -> str:
    """构造**完整**的 change-batch JSON。

    `Name` / `Type` / `TTL` / `ResourceRecords` 四者缺一，Route 53 就会以
    ``InvalidChangeBatch`` 拒绝整个请求。原实现只给了 TTL。

    Args:
        record_name: 记录名（会补尾点，Route 53 的规范形式）。
        record_type: 记录类型（A / CNAME…）。
        ttl: TTL 秒数。
        values: 记录值列表。

    Returns:
        单行 JSON 字符串，可直接放进 ``--change-batch '<json>'``。
    """
    name = record_name if record_name.endswith(".") else f"{record_name}."
    batch = {
        "Comment": "DR switchover",
        "Changes": [{
            "Action": "UPSERT",
            "ResourceRecordSet": {
                "Name": name,
                "Type": record_type,
                "TTL": ttl,
                "ResourceRecords": [{"Value": v} for v in values],
            },
        }],
    }
    return json.dumps(batch, separators=(",", ":"))


def build_ttl_change(
    profile: Optional[Any],
    ttl: int,
    record_values: Optional[List[str]] = None,
) -> Dict[str, str]:
    """构造「只改 TTL」的命令。

    ⚠️ Route 53 的 UPSERT 是**整条记录替换**，不是字段级更新——只想改 TTL 也必须
    连 ``ResourceRecords`` 一起提交，否则记录值会被清掉。因此这里要求先读出当前值。
    命令里给出 `get_current` 让操作者先取值，避免「改 TTL 顺手把解析目标删了」。

    Args:
        profile: DRProfile 或 None。
        ttl: 目标 TTL。
        record_values: 当前记录值；未知时命令里用变量占位并附取值命令。

    Returns:
        ``{"command": …, "validation": …, "expected": …, "note": …}``。
    """
    zone, configured = resolve_zone_id(profile)
    record_name = "example.com"
    record_type = "A"
    if profile is not None:
        try:
            record_name = profile.dns_primary_record or profile.domain or record_name
            record_type = profile.get("dns.record_type", "A") or "A"
        except Exception:  # noqa: BLE001
            pass

    get_current = (
        f"aws route53 list-resource-record-sets --hosted-zone-id {zone} "
        f"--query \"ResourceRecordSets[?Name=='{record_name}.' && Type=='{record_type}']\" "
        f"--output json"
    )

    if record_values:
        batch = _change_batch(record_name, record_type, ttl, record_values)
        command = (
            "# UPSERT 是整条记录替换，不是字段级更新：只改 TTL 也必须带上\n"
            "#   ResourceRecords，否则解析目标会被清掉。\n"
            f"aws route53 change-resource-record-sets --hosted-zone-id {zone} "
            f"--change-batch '{batch}'"
        )
    else:
        command = (
            "# UPSERT 是整条记录替换：必须先读出当前 ResourceRecords 再提交，\n"
            "#   否则只写 TTL 会把解析目标清掉。\n"
            f"CURRENT=$({get_current})\n"
            "# 用上面的输出填入下面的 ResourceRecords 后再执行：\n"
            f"aws route53 change-resource-record-sets --hosted-zone-id {zone} "
            f"--change-batch '"
            + _change_batch(record_name, record_type, ttl, ["<当前记录值>"])
            + "'"
        )

    note = "" if configured else (
        "\n# ⚠️ hosted zone id 未配置（profile 的 dns.hosted_zone_id）。"
        "命令中的占位符必须先替换为真实 zone id。"
    )
    return {
        "command": command + note,
        "validation": (
            f"aws route53 list-resource-record-sets --hosted-zone-id {zone} "
            f"--query \"ResourceRecordSets[?Name=='{record_name}.'].TTL\" --output text"
        ),
        "expected": str(ttl),
        "zone_configured": "yes" if configured else "no",
        "record_name": record_name,
    }


def build_failover_change(
    profile: Optional[Any],
    target_dns: str,
    target_region: str,
) -> Dict[str, str]:
    """构造「把主记录切到恢复区」的命令。

    Args:
        profile: DRProfile 或 None。
        target_dns: 恢复区入口的 DNS 名（ALB / NLB / CloudFront）。
        target_region: 恢复区 region（仅用于注释说明）。

    Returns:
        ``{"command": …, "validation": …, "expected": …}``。
    """
    zone, configured = resolve_zone_id(profile)
    record_name = "example.com"
    record_type = "CNAME"
    ttl = 60
    if profile is not None:
        try:
            record_name = profile.dns_primary_record or profile.domain or record_name
            record_type = profile.get("dns.record_type", "CNAME") or "CNAME"
            ttl = profile.dns_ttl_pre_switchover
        except Exception:  # noqa: BLE001
            pass

    batch = _change_batch(record_name, record_type, ttl, [target_dns])
    note = "" if configured else (
        "\n# ⚠️ hosted zone id 未配置（profile 的 dns.hosted_zone_id）。"
        "占位符必须先替换为真实 zone id。"
    )
    return {
        "command": (
            f"# 把 {record_name} 指向 {target_region} 的入口。\n"
            f"aws route53 change-resource-record-sets --hosted-zone-id {zone} "
            f"--change-batch '{batch}'" + note
        ),
        "validation": f"dig +short {record_name}",
        "expected": target_dns,
        "zone_configured": "yes" if configured else "no",
        "record_name": record_name,
    }

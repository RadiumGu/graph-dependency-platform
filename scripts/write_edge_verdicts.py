#!/usr/bin/env python3
"""把阶段 C 的注入验证判定写回 Neptune 的边属性。

契约（graph_contract_data.EDGE_VERIFICATION）规定：
  attrs      verify_status / verify_confidence / verify_last /
             verify_by / verify_experiment / verify_degradation
  authority  只有 'chaos-runner' 有权写这些属性 —— 所以 verify_by 固定写它，
             不是随便填一个来源名，否则 ETL 侧的门禁会把这批数据当成越权写入。
  statuses   untested / confirmed / refuted / inconclusive
             ⚠️ 注意契约用的是 **inconclusive**，不是我脚本里的 'unverifiable'，
                写回时必须映射，否则写进去的是契约不认的状态值。
  thresholds confirm_degradation_pct=20.0  refute_degradation_pct=5.0
             min_observation_requests=20   stale_verification_seconds=2592000

置信度按契约的 evidence_weights 折算：
  intervention_confirmed=+4.0 / intervention_refuted=-4.0
主动注入是最强证据（权重 4.0，远高于 observed_per_source 的 0.5），
因为它建立的是因果而非相关 —— 这正是阶段 C 相对被动观测的价值。

本脚本**只更新已存在的边属性**，不新建也不删除任何边或节点，
符合「Neptune 不能删、ETL 可改不可删」的硬约束。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path[:0] = os.environ.get("PYTHONPATH", "").split(":")

from neptune_client_base import neptune_query, extract_value  # noqa: E402

try:
    from graph_contract_data import EDGE_VERIFICATION  # noqa: E402
except Exception:  # pragma: no cover
    EDGE_VERIFICATION = {
        "statuses": ["untested", "confirmed", "refuted", "inconclusive"],
        "authority": ["chaos-runner"],
        "evidence_weights": {"intervention_confirmed": 4.0, "intervention_refuted": -4.0},
        "thresholds": {"min_observation_requests": 20},
    }

WRITER = EDGE_VERIFICATION["authority"][0]
VALID = set(EDGE_VERIFICATION["statuses"])
MIN_OBS = EDGE_VERIFICATION["thresholds"].get("min_observation_requests", 20)
W = EDGE_VERIFICATION["evidence_weights"]

# 脚本内部的三态 → 契约状态。'unverifiable' 在契约里叫 'inconclusive'。
STATUS_MAP = {
    "confirmed": "confirmed",
    "refuted": "refuted",
    "unverifiable": "inconclusive",
    "inconclusive": "inconclusive",
}


def esc(s: str) -> str:
    """Gremlin 字符串字面量转义。"""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")


def write_verdict(src: str, label: str, dst: str, v: dict) -> tuple[bool, str]:
    status = STATUS_MAP.get(v["verdict"])
    if status not in VALID:
        return False, f"状态 {v['verdict']!r} 不在契约允许值内"

    base = v.get("baseline")
    during = v.get("during")
    samples = v.get("samples") or base or 0

    # 退化百分比：基线成功率 → 注入期成功率的相对下降
    if base and base > 0 and during is not None:
        degradation = round((base - during) / base * 100.0, 1)
    else:
        degradation = 0.0

    if status == "confirmed":
        conf = W["intervention_confirmed"]
    elif status == "refuted":
        conf = W["intervention_refuted"]
    else:
        conf = 0.0

    # 样本量不足则降级为 inconclusive —— 判定方向可能对，但证据不够硬，
    # 写成 confirmed 会让下游误以为有充分依据。
    note = ""
    if samples and samples < MIN_OBS and status in ("confirmed", "refuted"):
        note = f" (样本 {samples} < 契约要求 {MIN_OBS}，降级为 inconclusive)"
        status, conf = "inconclusive", 0.0

    experiment = esc(v.get("experiment") or f"networkchaos-partition:{src}->{dst}")
    q = (
        f"g.V().has('name','{esc(src)}').outE('{esc(label)}')"
        f".where(inV().has('name','{esc(dst)}'))"
        f".property('verify_status','{status}')"
        f".property('verify_confidence',{conf})"
        f".property('verify_last',{int(time.time())})"
        f".property('verify_by','{WRITER}')"
        f".property('verify_experiment','{experiment}')"
        f".property('verify_degradation',{degradation})"
        f".count()"
    )
    res = neptune_query(q)
    data = res.get("result", {}).get("data", {}).get("@value", []) if isinstance(res, dict) else []
    n = extract_value(data[0]) if data else 0
    if not n:
        return False, "图里没有匹配到这条边（名字或 label 不符）"
    return True, f"{status} conf={conf} degradation={degradation}%{note}"


def parse_edge(edge: str) -> tuple[str, str, str] | None:
    """把 "src-Label->dst" 拆成三段。

    ⚠️ 不能用 split("-", 1)：服务名本身可能含连字符。
       "gateway-service-Calls->petsite" 会被切成
         src='gateway'  label='service-Calls'  dst='petsite'
       —— 结果 Gremlin 查一个不存在的 label，报「图里没有匹配到这条边」，
       而边其实好好地在图里（实测各 1 条）。这类错误会伪装成数据缺失。

    正确做法：先按 "->" 切出 dst，再从**右**边最后一个 "-" 切出 label。
    label 是 Gremlin 边标签（Calls / Delegates / Retrieves…），本身不含连字符，
    所以从右侧定位是安全的；而 src 可以含任意多个连字符。
    """
    if "->" not in edge:
        return None
    left, dst = edge.rsplit("->", 1)
    if "-" not in left:
        return None
    src, label = left.rsplit("-", 1)
    if not src or not label or not dst:
        return None
    return src, label, dst


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/edge_verdicts_final.json"
    rows = json.load(open(path))
    print(f"  写回 {len(rows)} 条判定（writer={WRITER}）\n")
    ok = fail = 0
    for r in rows:
        edge = r["edge"]
        parsed = parse_edge(edge)
        if parsed is None:
            print(f"  ✗ {edge}: 边名无法解析")
            fail += 1
            continue
        src, label, dst = parsed
        good, msg = write_verdict(src, label, dst, r)
        print(f"  {'✓' if good else '✗'} {edge:52s} {msg}")
        ok += good
        fail += not good
    print(f"\n  成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

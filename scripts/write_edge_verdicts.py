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

# ── 判定必须走带门禁的共享函数（2026-09-05 修）─────────────────────────────
#
# 本脚本原先**自己决定** status 与 confidence：调用方传进来一个 verdict 字符串
# 就照写，confidence 直接取契约里的**证据权重**（±4.0）。两个后果都实测到了：
#
#   1. 三道门禁被绕过 —— 注入生效门禁、证据通道分级、独立证据门禁。
#      实测因此产生 3 条错误的 refuted：DeepFlow calls 分别 60 / 2404 / 2796
#      （即有独立观测源看到过这条边），其中一条的 verify_reason 自述
#      「流量不足不能据此证伪」而 status 却写 refuted，状态与理由自相矛盾。
#   2. **11 条边的 verify_confidence 越界**（±4.0，而声明值域是 [0,1]）——
#      权限门禁（authority=chaos-runner）只校验**谁在写**，
#      不校验**写的值是否合法**。
#
# 现改为：状态与置信度一律由 graph_confidence 的共享函数算，调用方传入的
# verdict 降级为**交叉校验**用 —— 两者不一致时大声 log 出来（那正是上面
# 那批错误判定的形态），而不是静默采信任何一方。
_GATED = True
try:
    from graph_confidence import (  # noqa: E402
        classify_intervention,
        classify_dependency_strength,
        confidence as _confidence,
    )
except Exception as _e:  # pragma: no cover
    _GATED = False
    print(f"⚠️ 无法导入 graph_confidence（{_e!r}）—— 拒绝退回无门禁的旧逻辑。"
          f"请设置 PYTHONPATH 指向 infra/lambda/shared/python。", file=sys.stderr)

# 观测源标记表：与 chaos/code/runner/edge_verification.py 的 _OBSERVER_MARKERS
# 同源。此处内联一份是因为 scripts/ 不应依赖 chaos/code 的包结构；
# 由 tests/test_46_verdict_gating.py 强制两份保持一致。
_OBSERVER_MARKERS = {
    'xray': ('xray_call_count', 'xray_last_seen'),
    'nfm': ('nfm_flow_count', 'nfm_last_seen'),
    'deepflow': ('calls', 'error_rate'),
    # 2026-09-05（T-307）：K8s Pod spec 派生的启动依赖（微服务 → ECRRepository）。
    # 单列一档而不是塞进 deepflow：deepflow 的 ('calls','error_rate') 语义是
    # 「eBPF 观测到的流量计数」，而这类边来自镜像引用、不存在流量计数。
    # 写假的 calls 等于伪造观测证据，所以让写入方记录它实际看到的东西（镜像仓库名），
    # 这里按那个字段计数。
    'k8s-image-spec': ('image_ref',),
}
_STATIC_SOURCES = ('aws-etl', 'cfn-etl')


def _edge_evidence(src: str, label: str, dst: str) -> tuple[int, int, int, int]:
    """现场读这条边的属性，算出 (静态源, 观测源, 已确证次数, 已证伪次数)。

    独立观测源数是「有独立证据的边永不判 refuted」这道门禁的输入，
    必须现场读 —— 调用方不会传，而缺省 0 会让门禁失效。
    """
    q = (f"g.V().has('name','{esc(src)}').outE('{esc(label)}')"
         f".where(inV().has('name','{esc(dst)}')).valueMap().fold()")
    try:
        rows = neptune_query(q)['result']['data']['@value']
        raw = rows[0]['@value'] if rows else []
    except Exception:
        return (0, 0, 0, 0)
    if not raw:
        return (0, 0, 0, 0)
    it = iter(raw[0]['@value'])
    d = dict(zip(it, it))
    props = {}
    for k, v in d.items():
        val = v['@value'] if isinstance(v, dict) else v
        props[k] = val[0] if isinstance(val, list) and val else val
    static = 0
    if props.get('dependency_kind') == 'static':
        static += 1
    if props.get('source') in _STATIC_SOURCES:
        static += 1
    obs = sum(1 for markers in _OBSERVER_MARKERS.values()
              if any(m in props for m in markers))
    return (min(static, 2), obs,
            int(props.get('verify_confirm_count') or 0),
            int(props.get('verify_refute_count') or 0))


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
    if not _GATED:
        return False, "graph_confidence 不可用 —— 拒绝在无门禁的情况下写判定"

    claimed = STATUS_MAP.get(v["verdict"])
    if claimed not in VALID:
        return False, f"状态 {v['verdict']!r} 不在契约允许值内"

    base = v.get("baseline")
    during = v.get("during")
    samples = v.get("samples") or base or 0

    # 退化百分比：基线成功率 → 注入期成功率的相对下降
    if base and base > 0 and during is not None:
        degradation = round((base - during) / base * 100.0, 1)
    else:
        degradation = 0.0

    # ── 走共享判据，不自己决定状态 ─────────────────────────────────────────
    static_n, obs_n, conf_n, ref_n = _edge_evidence(src, label, dst)
    status, reason = classify_intervention(
        observer_baseline_requests=int(samples or 0),
        observer_injected_requests=int(v.get("injected_samples") or samples or 0),
        observer_degradation_pct=float(degradation),
        evidence_channel=v.get("evidence_channel", "both"),
        # 调用方没测注入生效性时传 None —— 门禁据此拒绝产生 refuted。
        # 这是刻意的：证伪需要先证明打断确实发生。
        injection_confirmed=v.get("injection_confirmed"),
        independent_observing_sources=obs_n,
        edge_baseline_calls=v.get("edge_baseline_calls"),
    )
    dep_class, dep_reason = classify_dependency_strength(
        float(degradation),
        evidence_channel=v.get("evidence_channel", "both"),
        injection_confirmed=v.get("injection_confirmed"),
        independent_observing_sources=obs_n,
        observer_baseline_requests=int(samples or 0),
        observer_injected_requests=int(v.get("injected_samples") or samples or 0),
    )
    if status == "confirmed":
        conf_n += 1
    elif status == "refuted":
        ref_n += 1
    conf = _confidence(static_n, obs_n, conf_n, ref_n)   # 归一化到 [0,1]

    # 调用方的主张与门禁结论不一致时大声报出来 —— 不静默采信任何一方。
    # 实测这正是 3 条错误 refuted 的形态（有独立观测源却判边不存在）。
    note = ""
    if claimed != status:
        note = (f" ⚠️ 调用方主张 {claimed!r}，带门禁的判据得出 {status!r}"
                f"（独立观测源={obs_n}）—— 采用后者")
        print(f"  ⚠️ {src} -[{label}]-> {dst}: {note.strip()}\n     理由: {reason}",
              file=sys.stderr)

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
        f".property('verify_reason','{esc(reason)[:300]}')"
        f".property('verify_confirm_count',{conf_n})"
        f".property('verify_refute_count',{ref_n})"
        f".property('verify_observing_sources',{obs_n})"
        f".property('verify_dependency_class','{dep_class or 'unclassified'}')"
        f".property('verify_dependency_class_reason','{esc(dep_reason)[:300]}')"
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

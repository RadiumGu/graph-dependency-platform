#!/usr/bin/env python3
"""回填被旧代码路径写坏的 verify_confidence（2026-09-05）。

## 修的是什么

旧版 `scripts/write_edge_verdicts.py` 自己决定置信度，直接写契约里的**证据权重**：

    confirmed → +4.0     refuted → -4.0     unverifiable → 0.0

2026-09-05 的修复把它改成一律调 `graph_confidence.confidence()`，并改掉了越界的
±4.0 那 11 条。但 **0.0 那批全部漏网** —— 0.0 落在声明值域 [0,1] 内，值域守门测试
（tests/test_45_target_selection.py）放行。活图谱复查：33 条。

## 为什么 0.0 一定是错的

`confidence()` 是 `round(sigmoid(log-odds), 4)`，没有 clamp。要得到 0.0 需要
log-odds ≤ -9.9，即**至少 3 次证伪**（3 × -4.0）。而零证据是 `sigmoid(0) = 0.5`。
这 33 条的 `verify_status` 全是 `inconclusive`（按契约 inconclusive 不递增
refute_count），且 `verify_confirm_count` / `verify_refute_count` 都是 **null** ——
而现行两条写入路径都无条件写这两个计数器，所以它们确实出自旧路径。

0.0 的语义是「几乎确定这条边不存在」，与实际情况（有证据但无法用干预检验）相反。
连带影响比数值本身严重：靶点选择按 `verify_confidence` 升序排（越低越不确定 =>
信息增益越大，chaos/code/runner/edge_verification.py:453），所以这批边会永久霸占
队列头部 —— 而它们恰恰是**已判明无法用现有手段注入**的边。

## 判据严格复用，不新增第三条路径

本脚本**不自己定义**证据规则：`_OBSERVER_MARKERS` / `_STATIC_SOURCES` /
`confidence()` 全部从既有模块 import。这个 bug 的根源正是「同一个判据存在两份
互相分歧的实现」，回填脚本再复制一份会重犯。

## 刻意不做的事

* **不动 `verify_status`** —— inconclusive 是正确判定（本次干预无法施加有效检验），
  只有置信度写错了。
* **不处理 `Invokes`** —— 那 16 条边所在的边类型在契约里是 `dependency: false`，
  它们该回填还是该整批清掉 `verify_*`，取决于「工具链/部署脚手架资源算不算被观测
  系统的一部分」这个尚未回答的设计问题。在那之前回填等于给一个可能不该存在的
  状态背书。用 `--include-invokes` 可以显式覆盖，默认拒绝。

用法：
    export NEPTUNE_ENDPOINT=<cluster endpoint>
    export PYTHONPATH=infra/lambda/shared/python
    python3 scripts/backfill_verify_confidence.py            # dry-run，只打印
    python3 scripts/backfill_verify_confidence.py --apply    # 实写
"""
from __future__ import annotations

import os
import sys

sys.path[:0] = os.environ.get("PYTHONPATH", "").split(":")
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from neptune_client_base import neptune_query  # noqa: E402

from graph_confidence import confidence as _confidence  # noqa: E402
from graph_contract import dependency_edge_labels, is_dependency_edge  # noqa: E402

# 判据同源：从既有脚本 import 而非复制。tests/test_46_verdict_gating.py 已强制
# write_edge_verdicts 的这两张表与 chaos/code/runner/edge_verification.py 一致，
# 所以 import 让本脚本自动继承那道一致性门禁。
from write_edge_verdicts import _OBSERVER_MARKERS, _STATIC_SOURCES  # noqa: E402

# 端点值 0.0 需要 log-odds ≤ -9.9，即至少 3 次证伪。低于这个证伪次数而
# confidence 为 0.0 的边，其置信度不可能由 confidence() 算出。
MIN_REFUTES_FOR_ZERO = 3


def _flatten(raw_props: list) -> dict:
    """Gremlin valueMap() 的 @value 交替列表 → 普通 dict。"""
    it = iter(raw_props)
    d = dict(zip(it, it))
    props = {}
    for k, v in d.items():
        val = v["@value"] if isinstance(v, dict) else v
        props[k] = val[0] if isinstance(val, list) and val else val
    return props


def _evidence(props: dict) -> tuple[int, int, int, int]:
    """(静态源, 观测源, 已确证, 已证伪) —— 规则与 write_edge_verdicts 完全一致。"""
    static = 0
    if props.get("dependency_kind") == "static":
        static += 1
    if props.get("source") in _STATIC_SOURCES:
        static += 1
    obs = sum(1 for markers in _OBSERVER_MARKERS.values()
              if any(m in props for m in markers))
    return (min(static, 2), obs,
            int(props.get("verify_confirm_count") or 0),
            int(props.get("verify_refute_count") or 0))


def find_candidates(include_invokes: bool = False) -> list[dict]:
    """找出 verify_confidence 恰为 0.0 且证伪次数不足以解释它的边。"""
    labels = ",".join(f"'{lb}'" for lb in sorted(dependency_edge_labels()))
    if include_invokes:
        labels += ",'Invokes'"
    q = (f"g.E().hasLabel({labels}).has('verify_confidence', 0.0)"
         f".project('eid','elabel','props')"
         f".by(__.id()).by(__.label()).by(__.valueMap()).fold()")
    rows = neptune_query(q)["result"]["data"]["@value"]
    if not rows:
        return []
    out = []
    for entry in rows[0]["@value"]:
        it = iter(entry["@value"])
        rec = dict(zip(it, it))
        eid = rec["eid"]
        eid = eid.get("@value") if isinstance(eid, dict) else eid
        props = _flatten(rec["props"]["@value"])
        static_n, obs_n, conf_n, ref_n = _evidence(props)
        if ref_n >= MIN_REFUTES_FOR_ZERO:
            continue  # 0.0 由真实证伪证据解释，合法，跳过
        out.append({
            "eid": eid,
            "label": rec["elabel"],
            "status": props.get("verify_status"),
            "source": props.get("source"),
            "dependency_kind": props.get("dependency_kind"),
            "evidence": (static_n, obs_n, conf_n, ref_n),
            "new_confidence": _confidence(static_n, obs_n, conf_n, ref_n),
            "counters_missing": props.get("verify_confirm_count") is None
            or props.get("verify_refute_count") is None,
        })
    return out


def backfill(rec: dict) -> bool:
    """写回置信度，并把缺失的两个计数器补成 0。

    计数器必须一并写：它们是「0.0 不可能由 confidence() 算出」这条判据的输入，
    留 null 的话下次读回来仍然走同一条路，回填会被重新覆盖成错值。
    """
    static_n, obs_n, conf_n, ref_n = rec["evidence"]
    q = (f"g.E('{rec['eid']}')"
         f".property('verify_confidence', {rec['new_confidence']})"
         f".property('verify_confirm_count', {conf_n})"
         f".property('verify_refute_count', {ref_n})")
    try:
        neptune_query(q)
        return True
    except Exception as e:  # pragma: no cover
        print(f"  ✗ {rec['eid']}: {e!r}", file=sys.stderr)
        return False


def main() -> int:
    apply = "--apply" in sys.argv
    include_invokes = "--include-invokes" in sys.argv

    if not is_dependency_edge("Calls"):
        print("契约自检失败：Calls 应为 dependency 边，检查 graph_contract 是否可用",
              file=sys.stderr)
        return 2

    cands = find_candidates(include_invokes)
    if not cands:
        print("没有需要回填的边 —— 全图 verify_confidence 无非法 0.0")
        return 0

    print(f"{'[实写]' if apply else '[dry-run]'} 待回填 {len(cands)} 条\n")
    print(f"{'边类型':<14}{'状态':<14}{'source':<16}"
          f"{'证据(静/观/确/伪)':<20}{'0.0 →':<10}{'补计数器'}")
    print("-" * 92)
    ok = 0
    for r in sorted(cands, key=lambda x: (x["label"], str(x["source"]))):
        line = (f"{r['label']:<14}{str(r['status']):<14}{str(r['source']):<16}"
                f"{str(r['evidence']):<20}{r['new_confidence']:<10}"
                f"{'是' if r['counters_missing'] else '否'}")
        print(line)
        if apply and backfill(r):
            ok += 1
    print("-" * 92)
    if apply:
        print(f"写入成功 {ok}/{len(cands)}")
        return 0 if ok == len(cands) else 1
    print("这是 dry-run。加 --apply 实写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

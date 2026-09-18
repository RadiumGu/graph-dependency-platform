#!/usr/bin/env python3
"""清除非依赖边上被误写的 verify_* 属性（2026-09-05）。

## 为什么要清

`verify_*` 按契约只适用于 `dependency: true` 的边类型（`graph_contract.is_dependency_edge`）。
活图谱实测：**全部 16 条 `Invokes` 边都带着完整的 `verify_*`**，而 `Invokes` 在契约里是
`dependency: false`。16/16 说明这不是偶发泄漏，是靶点选择器系统性地把 `Invokes` 当成
依赖边选中了 —— 两个组件对「什么算依赖」给出了相反答案。

## 为什么清而不是回填

这 16 条的 `verify_confidence` 也是非法的 0.0（与
`scripts/backfill_verify_confidence.py` 修的是同一个根因），但**不能回填**：
回填等于给「非依赖边进入验证体系」这个状态背书。正确处置是移除，让它们回到
「从未被验证过」的状态。

## 删之前先留存

16 条的 `verify_experiment` 文本完全相同、时间戳也相同（1788561628）：

    chaos-mesh-cannot-target-lambda: Chaos Mesh operates on Pod netns;
    Lambda/StepFunctions run outside the cluster

也就是说这 16 条记录的信息量是**一条规则**，不是 16 份证据：
「注入手段的能力边界由目标的运行平台决定（Pod netns vs Lambda/StepFunctions）」。
这条规则该由靶点选择器**在选靶前**判断并跳过，而不是选中、注入失败、再往 16 条边上
各写一份同样的字符串。留存文件是这个结论的证据，见 T-305。

## 一个仍然开着的建模问题（清理不解决它，刻意不掩盖）

`StepFunction → LambdaFunction`（3 条，指向 profile 声明的 stepread/stepprice）与
`SNSTopic → LambdaFunction`（1 条，告警投递链）**按任何定义都是依赖关系**。
把 `Invokes` 标成 `dependency: false` 才是可疑的那一侧。清理只是把错写的属性移除，
不是认定这些边不是依赖。该问题记为 T-304，需要拆标签或改标记，属破坏性变更。

用法：
    export NEPTUNE_ENDPOINT=<cluster endpoint> REGION=<region>
    export PYTHONPATH=infra/lambda/shared/python
    python3 scripts/clean_nondependency_verify_attrs.py           # dry-run
    python3 scripts/clean_nondependency_verify_attrs.py --apply   # 实写
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path[:0] = os.environ.get("PYTHONPATH", "").split(":")

from neptune_client_base import neptune_query  # noqa: E402

from graph_contract import dependency_edge_labels  # noqa: E402
from graph_contract_data import EDGE_TYPES, EDGE_VERIFICATION  # noqa: E402

# 契约声明的 6 个 + 写入方实际会写的其余 verify_* （edge_verification.write_verdict）
VERIFY_ATTRS = sorted(set(EDGE_VERIFICATION["attrs"]) | {
    "verify_reason", "verify_confirm_count", "verify_refute_count",
    "verify_evidence_channel", "verify_dependency_class",
    "verify_dependency_class_reason", "verify_observing_sources",
})

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "todo")


def _flatten(raw: list) -> dict:
    it = iter(raw)
    d = dict(zip(it, it))
    out = {}
    for k, v in d.items():
        val = v["@value"] if isinstance(v, dict) else v
        out[k] = val[0] if isinstance(val, list) and val else val
    return out


def find_violations() -> list[dict]:
    """所有带任一 verify_* 属性、但边类型不是 dependency 的边。"""
    dep = dependency_edge_labels()
    nondep = sorted(lb for lb in EDGE_TYPES if lb not in dep)
    labels = ",".join(f"'{lb}'" for lb in nondep)
    ors = ",".join(f"__.has('{a}')" for a in VERIFY_ATTRS)
    q = (f"g.E().hasLabel({labels}).or({ors})"
         f".project('eid','elabel','src','dst','props')"
         f".by(__.id()).by(__.label())"
         f".by(__.outV().values('name')).by(__.inV().values('name'))"
         f".by(__.valueMap()).fold()")
    rows = neptune_query(q)["result"]["data"]["@value"]
    if not rows:
        return []
    out = []
    for entry in rows[0]["@value"]:
        it = iter(entry["@value"])
        rec = dict(zip(it, it))
        eid = rec["eid"]
        out.append({
            "eid": eid.get("@value") if isinstance(eid, dict) else eid,
            "label": rec["elabel"],
            "src": rec["src"],
            "dst": rec["dst"],
            "props": _flatten(rec["props"]["@value"]),
        })
    return out


def archive(violations: list[dict]) -> str:
    """删前留存：完整属性快照落盘，Neptune 侧删除不可逆。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(
        ARCHIVE_DIR,
        f"removed-verify-attrs-nondependency-edges_{time.strftime('%Y%m%d-%H%M')}.json")
    payload = {
        "removed_at": int(time.time()),
        "reason": "verify_* 只适用于 dependency:true 的边类型；这些边类型在契约里是 "
                  "dependency:false，属误写。见 scripts/clean_nondependency_verify_attrs.py",
        "attrs_removed": VERIFY_ATTRS,
        "edges": [{k: v for k, v in e.items()} for e in violations],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def clean(rec: dict) -> bool:
    present = [a for a in VERIFY_ATTRS if a in rec["props"]]
    if not present:
        return True
    args = ",".join(f"'{a}'" for a in present)
    q = f"g.E('{rec['eid']}').properties({args}).drop()"
    try:
        neptune_query(q)
        return True
    except Exception as e:  # pragma: no cover
        print(f"  ✗ {rec['eid']}: {e!r}", file=sys.stderr)
        return False


def main() -> int:
    apply = "--apply" in sys.argv
    v = find_violations()
    if not v:
        print("没有违约边 —— 非依赖边上不存在 verify_* 属性")
        return 0

    from collections import Counter
    print(f"{'[实写]' if apply else '[dry-run]'} 违约边 {len(v)} 条，"
          f"按边类型：{dict(Counter(e['label'] for e in v))}\n")
    for e in sorted(v, key=lambda x: (x["label"], x["src"], x["dst"])):
        present = [a for a in VERIFY_ATTRS if a in e["props"]]
        print(f"  {e['label']:<10} {e['src'][:44]:<46}-> {e['dst'][:44]:<46}"
              f" 属性 {len(present)}")

    if not apply:
        print("\n这是 dry-run。加 --apply 实写（会先落盘留存）。")
        return 0

    path = archive(v)
    print(f"\n已留存：{path}")
    ok = sum(1 for e in v if clean(e))
    print(f"清除成功 {ok}/{len(v)}")
    return 0 if ok == len(v) else 1


if __name__ == "__main__":
    sys.exit(main())

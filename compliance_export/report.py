"""渲染层 —— Markdown（给人看）+ CSV（给机器/审计取证）。

## 页首三件套是强制的

每份产出都必须带：**快照时刻**、**依赖边清单来源**、**局限披露**。

前两者是可交叉校验的前提（DORA 用 12/31 统一基准日，ESAs 点名过「跨模板基准日
不一致」）。第三者是这份报告能不能被采信的关键 —— 一份不披露 21% 确证率的映射，
被审计发现后会连带质疑其余所有数字。

## 为什么局限披露由代码生成而不是手写

手写的披露会过期。这里的每一条都从快照数据算出来：确证率现算、缺失维度现查、
impact tolerance 是否为空现判。数字变了，披露跟着变。
"""
from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, List, Optional

from .breakdown import MISSING_LABEL, Breakdown

#: SYSC 15A.4.1R 要求的六要素，及本平台的覆盖状态。
#: 覆盖状态是**结构性事实**（有没有这类节点），不随快照变化，所以在这里写死；
#: 但每一项都注明依据，便于将来补齐时同步更新。
SYSC_15A_ELEMENTS = (
    ("technology", "✅ 覆盖", "Microservice / Pod / EC2 / Lambda / 各类托管服务"),
    ("facilities", "✅ 覆盖", "AvailabilityZone / Region / VPC / Subnet"),
    ("people", "❌ 缺失", "契约与活图谱均无责任人/团队实体"),
    ("processes", "❌ 缺失", "同上，无流程实体"),
    ("information", "🟡 部分", "数据基础设施有（AccessesData），表/字段级血缘无"),
)


def _fmt(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return "%.3f" % v
    return str(v)


def _md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_fmt(c) for c in r) + " |")
    return "\n".join(out)


def _header(snap) -> str:
    """页首三件套的前两件。"""
    return "\n".join([
        "# 合规依赖关系报告",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| **快照时刻（基准日）** | `%s` |" % snap.taken_at,
        "| 数据源 | `%s` |" % snap.endpoint,
        "| 业务能力数 | %d |" % snap.capability_count,
        "| 一跳依赖边数 | %d |" % snap.dependency_count,
        "| 依赖边类型（%d 种） | `%s` |" % (
            len(snap.dependency_edge_labels), " / ".join(snap.dependency_edge_labels)),
        "",
        "> **依赖边清单来源**：`graph_contract.dependency_edge_labels()`（契约，单一来源）。",
        "> 本报告全部表格取自**同一次会话、同一快照时刻** —— 跨表数字可交叉校验。",
        "> 活图谱正被 ETL 持续改写，不同时刻导出的数字会不同，这是正常的；",
        "> 但同一份报告内部若出现矛盾，则是缺陷。",
    ])


def _limitations(snap, bd: Breakdown) -> str:
    """局限披露 —— 全部由快照数据算出，不手写。"""
    lines = ["## 必须随报告一同披露的局限", ""]

    confirmed = bd.bucket("verify_status", "confirmed")
    n_conf = confirmed.count if confirmed else 0
    pct = confirmed.share_pct if confirmed else "0%"
    missing_verify = bd.bucket("verify_status", MISSING_LABEL)
    n_missing = missing_verify.count if missing_verify else 0
    lines += [
        "**1. 证据覆盖率 %s。** %d 条依赖里 `confirmed` 仅 %d 条，%d 条从未验证。"
        % (pct, bd.total, n_conf, n_missing),
        "`inconclusive`（注入了故障但退化不足以判定）与「未验证」刻意区分 —— ",
        "零流量与健康在指标上无法区分，不可混为「依赖不成立」。",
        "",
    ]

    missing_kind = bd.bucket("dependency_kind", MISSING_LABEL)
    if missing_kind:
        lines += [
            "**2. %d 条边的 `dependency_kind` 为空。** 这些是边类型分类新近变更、"
            "尚待下轮 ETL 补标的边。空值表示「尚未打标」，不表示「不适用」。"
            % missing_kind.count,
            "",
        ]

    lines += [
        "**3. SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and document ",
        "the **people, processes, technology, facilities and information** necessary ",
        "to deliver each important business service：",
        "",
        _md_table(["要素", "状态", "依据"],
                  [[e, s, w] for e, s, w in SYSC_15A_ELEMENTS]),
        "",
    ]

    no_tolerance = [r for r in snap.capability_meta
                    if r.get("impact_tolerance_seconds") is None]
    if no_tolerance:
        lines += [
            "**4. impact tolerance 未设定（%d/%d 个业务能力为空）。** SYSC 15A.2.5R 要求 "
            "firm *must* set an impact tolerance for each important business service。"
            % (len(no_tolerance), len(snap.capability_meta)),
            "**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有阈值，",
            "不能用「没检测到越界」掩盖「压根没有阈值可比」。",
            "",
        ]

    lines += [
        "**5. 承载层不在依赖表内。** `LocatedIn` / `RunsOn` 等承载关系普遍为真、",
        "不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS 集群与",
        "负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT 服务 ——",
        "故单列于「承载层」一节，并注明性质。",
        "",
        "**6. 集中度是技术集中度，不是供应商集中度。** DORA Art. 29/31 要的是",
        "供应商层面的集中度与分包链，需要 `Vendor` 法人实体节点才能回答；",
        "本平台当前只有技术对象。第四方分包链在埋点边界外，**结构上做不到**。",
        "",
        "**7. 本报告不是 DORA Art. 28 信息登记册。** 那份由 ITS (EU) 2024/2956 规定",
        "15 张互联模板、以 xBRL-CSV 年度报送，字段是合同编号、通知期、适用法律、",
        "20 位 LEI、退出策略 —— 本质是合同清单不是图。本报告对应的是 DORA Art. 8(4)",
        "「map the links and interdependencies」、BCBS POR 原则四、SYSC 15A.4.1R",
        "与关基条例第九条那一类**穿透式依赖映射**要求。",
    ]
    return "\n".join(lines)


def render_markdown(snap, bd: Breakdown) -> str:
    parts = [_header(snap), ""]

    # ── 三栏分列 ──
    parts += ["## 三栏分列统计", "",
              "> 三个维度回答三个不同问题，**刻意不合并成单一覆盖率**：",
              "> 平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消。", ""]
    for _field, title, why, buckets in bd.dimensions():
        parts += ["### %s" % title, "", "> %s" % why, "",
                  _md_table(["取值", "条数", "占比"],
                            [[b.value, b.count, b.share_pct] for b in buckets]),
                  ""]

    # ── 功能映射表 ──
    parts += ["## 功能映射表（按业务能力）", ""]
    by_cap: Dict[str, List[Dict[str, Any]]] = {}
    for r in snap.function_mapping:
        by_cap.setdefault(r["capability"], []).append(r)
    reach = {r["capability"]: r for r in getattr(snap, "reachability", [])}
    for cap in sorted(by_cap):
        rows = by_cap[cap]
        tier = rows[0].get("tier") or "–"
        rc = reach.get(cap, {})
        parts += [
            "### %s（%s）— 一跳依赖 %d 条，多跳可达 %s 个对象"
            % (cap, tier, len(rows), rc.get("reachable_objects", "?")),
            "",
            _md_table(
                ["服务", "边类型", "被依赖对象", "对象类型", "scope",
                 "kind", "verify", "conf", "source", "drift"],
                [[r["service"], r["edge_type"], r["target"], r["target_label"],
                  r.get("target_scope"), r.get("dependency_kind"),
                  r.get("verify_status"), r.get("confidence"),
                  r.get("source"), r.get("drift_status")] for r in rows]),
            "",
        ]

    # ── 集中度 ──
    parts += ["## 技术集中度（SYSC 15A.2.7G(10) / DORA Art. 29-31 的技术输入）", "",
              "> 原文要求评估 *the potential aggregate impact of disruptions to multiple",
              "> important business services, in particular where such services rely on",
              "> **common operational resources** as identified by the firm's mapping exercise*。",
              ""]
    total_caps = snap.capability_count
    parts += [_md_table(
        ["被依赖对象", "类型", "scope", "支撑业务功能数", "边数"],
        [[r["target"], r["target_label"], r.get("target_scope"),
          "%s / %s" % (r["capability_count"], total_caps), r["edge_count"]]
         for r in snap.concentration]), ""]

    # ── 承载层 ──
    parts += ["## 基础设施承载层（单列，非服务消费关系）", "",
              "> 这些边**不是**依赖。它们普遍为真、不携带判别信息，故不进依赖表；",
              "> 但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，故在此列出。",
              "",
              _md_table(["边类型", "条数", "性质"],
                        [[r["edge_type"], r["count"], r["nature"]]
                         for r in snap.hosting_layer]), ""]

    # ── 业务能力元数据 ──
    parts += ["## 业务能力与容忍度阈值", "",
              _md_table(["业务能力", "tier", "impact_tolerance_seconds", "rto_target_seconds"],
                        [[r["capability"], r.get("tier"),
                          r.get("impact_tolerance_seconds"), r.get("rto_target_seconds")]
                         for r in snap.capability_meta]),
              "",
              "> `impact_tolerance` 与 `rto_target` **必须分列**：RTO 是恢复某个流程的",
              "> 目标时间（内部视角），impact tolerance 是 IBS 的最大可容忍中断",
              "> （外部危害视角），两者可以差数倍。混用是监管审查重点。", ""]

    parts += [_limitations(snap, bd)]
    return "\n".join(parts) + "\n"


#: CSV 产出的列顺序。`verify_status` 与 `dependency_kind` 是强制列，
#: 由 tests/test_54_compliance_export.py::m02 锁定。
CSV_COLUMNS = (
    "capability", "tier", "service", "service_label", "edge_type",
    "target", "target_label", "target_scope",
    "dependency_kind", "verify_status", "confidence",
    "source", "drift_status", "last_seen", "verify_experiment",
)


def render_csv(snap) -> str:
    """功能映射表的 CSV。页首以注释行写入快照时刻。

    RFC 4180 没有注释语法，但审计取证需要基准日与数据不可分离；用 `#` 前缀行，
    多数工具（pandas `comment='#'`、Excel 导入）都能跳过。
    """
    buf = io.StringIO()
    buf.write("# compliance dependency mapping\n")
    buf.write("# snapshot_taken_at=%s\n" % snap.taken_at)
    buf.write("# endpoint=%s\n" % snap.endpoint)
    buf.write("# dependency_edge_labels=%s\n" % ",".join(snap.dependency_edge_labels))
    buf.write("# source=graph_contract.dependency_edge_labels()\n")
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    w.writeheader()
    for r in snap.function_mapping:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})
    return buf.getvalue()


def write_bundle(snap, bd: Breakdown, out_dir: str,
                 basename: Optional[str] = None) -> Dict[str, str]:
    """写出一份完整报告包。返回 {kind: path}。

    默认文件名带快照时刻 —— SYSC 15A.6.2R 要求保存 **each version** 的记录 6 年，
    同名覆盖会让历史版本消失。

    `basename` 用于**样例**：样例要能被 diff、被覆盖更新，所以用固定名。
    正式导出**不要**传它。快照时刻仍写在文件内容里（页首 + CSV 注释行），
    所以即使文件名固定，基准日也不会丢。
    """
    os.makedirs(out_dir, exist_ok=True)
    if basename:
        md_name = "%s.md" % basename
        csv_name = "%s.csv" % basename
    else:
        stamp = snap.taken_at.replace(":", "").replace("-", "")
        md_name = "compliance-dependency-report_%s.md" % stamp
        csv_name = "compliance-dependency-mapping_%s.csv" % stamp

    paths = {}
    md_path = os.path.join(out_dir, md_name)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(snap, bd))
    paths["markdown"] = md_path
    csv_path = os.path.join(out_dir, csv_name)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(render_csv(snap))
    paths["csv"] = csv_path
    return paths

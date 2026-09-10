"""渲染层 —— Markdown（给人看）+ CSV（给机器/审计取证）。

## 文档形制的依据，以及一条不能越的线

本模块的章节骨架取自 **SYSC 15A.6.1R(1)-(9)** —— 那一条列举了 firm 自己的书面
记录必须包含什么（IBS 识别与理由、impact tolerance 与理由、mapping 方法、测试
计划、场景测试细节、lessons learned、vulnerabilities 与整改时限理由、沟通策略、
所用方法论）。文档控制要素（标准识别、固有局限性、所执行工作摘要、日期、批准）
借自 ISAE 3000 (Revised) 第 69 段的要素纪律。

**但本报告刻意不声称是鉴证报告。** ISAE 3000 §69(h)(i)(j) 要求声明「本业务按
本 ISAE 执行」、「适用 ISQC 1」、「遵守 IESBA Code 独立性要求」—— 自动生成的
管理层记录做不出这三条声明。照抄整套要素会让读者以为存在独立鉴证，那是虚假
陈述，比形制粗糙严重得多。所以 §1 一上来就声明性质。

## 取值词汇对齐外部标准，不用内部实现词

- 缺失值 → `未评估（Assessment not performed）`，对齐 ITS (EU) 2024/2956
  B_06.01.0050 的枚举码 3。DORA 的法定模版把「没评估」当成一个**显式枚举码**，
  不是空值 —— 数据库的 null 不该出现在正式文档里。
- 取证方法 → `TEST` / `EXAMINE`，取自 NIST OSCAL `observation.method` 的受控
  词汇（源自 SP 800-53A，强度 TEST > EXAMINE > INTERVIEW）。
- 结论措辞 → `Confirmed — no exceptions noted` / `Not tested`，对齐 SOC 2
  Section IV「tests of controls and results of tests」的惯例。

## 页首三件套仍然是强制的

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
from typing import Any, Dict, List, Optional, Tuple

from .breakdown import MISSING_LABEL, Breakdown

#: SYSC 15A.4.1R 要求的六要素，及本平台的覆盖状态。
#: 覆盖状态是**结构性事实**（有没有这类节点），不随快照变化，所以在这里写死；
#: 但每一项都注明依据，便于将来补齐时同步更新。
SYSC_15A_ELEMENTS = (
    ("technology", "覆盖", "Microservice / Pod / EC2 / Lambda / 各类托管服务"),
    ("facilities", "覆盖", "AvailabilityZone / Region / VPC / Subnet"),
    ("people", "缺失", "契约与活图谱均无责任人/团队实体"),
    ("processes", "缺失", "同上，无流程实体"),
    ("information", "部分", "数据基础设施有（数据访问类边），表/字段级血缘无"),
)

#: 适用标准（applicable criteria）与本报告对应节。
#: ISAE 3000 §69(d) 要求识别适用标准；把条文依据集中成一张交叉引用表，
#: 而不是散在各节的括号里 —— 散落的引用无法被审计逐条核对。
APPLICABLE_CRITERIA: Tuple[Tuple[str, str, str], ...] = (
    ("DORA (EU) 2022/2554 Art. 8(4)",
     "map the links and interdependencies", "§5 依赖关系映射"),
    ("DORA (EU) 2022/2554 Art. 8(5)",
     "识别与第三方的 interconnections", "§6 第三方范围分列"),
    ("FCA SYSC 15A.4.1R",
     "identify and document people/processes/technology/facilities/information",
     "§5、§10"),
    ("FCA SYSC 15A.5.3R",
     "carry out scenario testing 的测试证据", "§5 取证方法与结论"),
    ("FCA SYSC 15A.2.5R",
     "must set an impact tolerance for each IBS", "§9（本报告披露为未设定）"),
    ("FCA SYSC 15A.2.7G(10)",
     "common operational resources 的聚合影响", "§7 技术集中度"),
    ("FCA SYSC 15A.6.1R(3)",
     "mapping 方法与如何支撑测试的书面记录", "附录 A"),
    ("FCA SYSC 15A.6.1R(7)",
     "vulnerabilities 及整改行动与时限理由", "§10"),
    ("FCA SYSC 15A.6.2R",
     "retain each version …… at least 6 years", "文档控制（文件名带快照时刻）"),
    ("FCA SYSC 15A.7.1R",
     "governing body 批准并定期审查该书面记录", "文档控制（待签署）"),
    ("BCBS Principles for Operational Resilience 原则四",
     "mapping interconnections and interdependencies", "§5"),
    ("《关键信息基础设施安全保护条例》第九条",
     "识别关键业务及其依赖的网络设施与信息系统", "§5"),
    ("ITS (EU) 2024/2956 B_06.01",
     "functions identification 的字段与枚举词汇（借用词汇，非报送）", "§4、§9"),
    ("NIST OSCAL observation.method",
     "TEST / EXAMINE / INTERVIEW 取证方法受控词汇", "§4、§5"),
    ("ISAE 3000 (Revised) §69(e)",
     "重大固有局限性的描述", "§10"),
)

#: 术语与取值定义。正式文档必须定义自己用的每个取值 —— 否则读者只能猜。
GLOSSARY: Tuple[Tuple[str, str], ...] = (
    ("依赖对象标识符",
     "AWS 物理资源 ID、Kubernetes 服务名、AWS 服务端点名或 agent 工具键，"
     "按对象类型而定。本平台**不生成**另一套展示名 —— 图谱里没有的名称不会被"
     "编造出来，标识符即该对象在其所属系统中的真实身份键。"),
    ("依赖类型",
     "契约定义的依赖边类型，来源为 `graph_contract.dependency_edge_labels()`。"
     "承载/放置类关系不在此列，单列于 §8。"),
    ("取证方法 TEST",
     "主动故障注入实测。对应 NIST OSCAL `observation.method=TEST`，"
     "也对应 SYSC 15A.5.3R 的 scenario testing。证据强度最高。"),
    ("取证方法 EXAMINE",
     "审阅运行时遥测与配置（来源见随附 CSV 的 `source` 列），未做主动注入。"
     "对应 OSCAL `observation.method=EXAMINE`。可证明「观测到过」，"
     "不能证明「切断后业务确实受损」。"),
    ("Confirmed — no exceptions noted",
     "已做故障注入，且观测到预期的业务侧退化。该依赖成立且被实测确证。"),
    ("Inconclusive",
     "已做故障注入，但观测退化不足以判定。**刻意不与「未测试」合并** —— "
     "零流量与健康在指标上无法区分，不可据此判「依赖不成立」。"),
    ("Not tested — assessment not performed",
     "未做故障注入。措辞与取值对齐 ITS (EU) 2024/2956 B_06.01.0050 枚举码 3 "
     "(Assessment not performed)：法定模版把「未评估」作为显式取值，不是空值。"),
    ("未评估（Assessment not performed）",
     "该字段在图谱中无值。对 `verify_status` 表示未做场景测试；"
     "对 `dependency_kind` 表示 ETL 尚未打标（如边类型分类新近变更）。"
     "**不折叠为 0 或空档** —— 「我们知道自己不知道」是审计要看的诚实度。"),
    ("未设定（Not defined）",
     "该阈值尚未设定。ITS B_06.01.0080/0090 对未定义的 RTO/RPO 规定填 `0`，"
     "本报告在人类可读产出中写作「未设定」以免与真实的 0 秒混淆；"
     "机器可读产出（CSV）保留空值。"),
    ("范围 observed / external / scaffolding",
     "observed = 本组织自有并被观测到；external = 组织外部服务端点（DORA "
     "Art. 8(5) 的 interconnections）；scaffolding = 构建/部署期基础设施，"
     "非运行时业务依赖。"),
    ("drift 漂移状态",
     "declared_not_observed = 配置声明存在但当轮未观测到；"
     "observed_then_silent = 曾观测到、当轮静默。两者都不等于「依赖已消失」。"),
)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return "%.3f" % v
    return str(v)


class _TableNumberer:
    """表编号器。正式文档的表要连续编号并带题注（`Table N: caption`）。

    做成对象而不是全局计数器，是为了让一次渲染的编号自成一体 —— 全局计数器在
    连续导出两份报告时会从上一份接着数。
    """

    def __init__(self) -> None:
        self._n = 0

    def caption(self, text: str) -> str:
        self._n += 1
        return "**表 %d：%s**" % (self._n, text)


def _md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_fmt(c) for c in r) + " |")
    return "\n".join(out)


#: verify_status 取值 → (取证方法, 结论措辞)。
#: 取证方法用 NIST OSCAL `observation.method` 的受控词汇；结论措辞用 SOC 2
#: Section IV 的惯例。未测试的边若有观测来源，其取证方法是 EXAMINE 而非「无」——
#: ETL 从遥测里看到过这条边，那是证据，只是强度低于实测。
def _evidence(row: Dict[str, Any]) -> Tuple[str, str]:
    status = row.get("verify_status")
    if status == "confirmed":
        return "TEST", "Confirmed — no exceptions noted"
    if status == "inconclusive":
        return "TEST", "Inconclusive — 已注入故障，观测退化不足以判定"
    if status:
        return "TEST", str(status)
    if row.get("source"):
        return "EXAMINE", "Not tested — assessment not performed"
    return "未取证", "Not tested — assessment not performed"


def _note(row: Dict[str, Any]) -> str:
    """例外与备注列。只在确有内容时写，避免产出一整列空值。

    一整列 `—` 比没有这一列更糟 —— 读者会以为渲染坏了，或以为该字段永远无值。
    """
    bits = []
    drift = row.get("drift_status")
    if drift and drift != "ok":
        bits.append("漂移 `%s`" % drift)
    exp = row.get("verify_experiment")
    if exp:
        bits.append("实验 `%s`" % exp)
    return "；".join(bits) if bits else ""


def _doc_control(snap, doc_control: Optional[Dict[str, str]] = None) -> str:
    """文档控制块。

    报告编号由快照时刻确定 —— 它天然唯一且可追溯，不需要另设一套流水号。
    密级与批准人**刻意留作待填写**：自动生成的报告不该替组织决定密级，
    也不该伪造签署。SYSC 15A.7.1R 要求 governing body 批准该记录，
    所以那一行的存在本身是在提示这件事还没做。
    """
    dc = dict(doc_control or {})
    ref = dc.get("reference") or ("DEP-MAP-%s" % snap.taken_at.replace(":", "")
                                  .replace("-", ""))
    rows = [
        ["报告编号", "`%s`（由快照时刻确定，全局唯一）" % ref],
        ["报告名称", "依赖关系映射记录 —— 管理层自评估"],
        ["基准时点（as-at）", "`%s`" % snap.taken_at],
        ["版本纪律", "每次导出为独立版本，文件名带快照时刻，不覆盖历史"
                     "（SYSC 15A.6.2R 要求保存 each version 满 6 年）"],
        ["密级", dc.get("classification") or "〈待指定〉"],
        ["编制", dc.get("prepared_by")
                 or "`compliance_export` 自动生成，无人工编辑"],
        ["审核", dc.get("reviewed_by") or "〈待签署〉"],
        ["批准", dc.get("approved_by")
                 or "〈待签署〉—— SYSC 15A.7.1R 要求 governing body 批准并定期审查"],
        ["分发范围", dc.get("distribution") or "〈待指定〉"],
    ]
    return "\n".join([
        "# 依赖关系映射记录 —— 管理层自评估",
        "",
        "## 文档控制",
        "",
        _md_table(["项", "值"], rows),
    ])


def _nature_statement() -> str:
    """§1 报告性质。

    这一节的作用是**防止读者误以为存在独立鉴证**。ISAE 3000 §69(f) 要求在标准
    为特定目的设计时提醒读者该信息可能不适用于其他目的；§69(h)(i)(j) 要求的三条
    声明（依准则执行、ISQC 1 质控、IESBA 独立性）本报告一条都做不出，所以必须
    明说不是鉴证报告。少了这一节，形制上的「正规」反而变成误导。
    """
    return "\n".join([
        "## 1 报告性质与适用范围",
        "",
        "**1.1 本报告是管理层自行编制的书面记录**，用于满足 FCA SYSC 15A.6.1R "
        "对依赖关系 mapping 的记录要求，以及 DORA Art. 8(4)、BCBS 运营韧性原则四、"
        "《关基条例》第九条一类的穿透式依赖映射要求。",
        "",
        "**1.2 本报告不是鉴证报告，未按 ISAE 3000 (Revised) 执行。** 报告由本组织"
        "自有工具从可观测性数据自动生成，未经独立执业者鉴证，不含 ISAE 3000 "
        "§69(h)(i)(j) 所要求的「依准则执行」、「适用 ISQC 1 质量控制」与"
        "「遵守 IESBA Code 独立性要求」声明。任何将本报告视为独立鉴证结论的引用"
        "都是误用。",
        "",
        "**1.3 本报告不是 DORA Art. 28 信息登记册（Register of Information）。** "
        "那份登记册由 ITS (EU) 2024/2956 规定 16 张互联模板、以 xBRL-CSV 报送，"
        "其主连接键是 `contractual arrangement reference number`（合同编号），"
        "字段为通知期、适用法律、20 位 LEI、退出计划。本组织的图谱不含合同数据，"
        "**结构上无法产出该登记册**；强行套用只会得到一份强制字段大面积为空的"
        "失败报送件。本报告借用其 B_06.01 的取值词汇（见 §4），仅此而已。",
        "",
        "**1.4 本报告的结论仅在 §2 所载基准时点成立**，不构成对该时点之后系统"
        "状态的陈述。",
    ])


def _basis(snap) -> str:
    """§2 报告基准与数据来源 —— 原「页首三件套」的前两件。"""
    return "\n".join([
        "## 2 报告基准与数据来源",
        "",
        _md_table(["项", "值"], [
            ["快照时刻（基准日）", "`%s`" % snap.taken_at],
            ["数据源", "`%s`" % snap.endpoint],
            ["业务功能数", snap.capability_count],
            ["一跳依赖边数", snap.dependency_count],
            ["依赖边类型（%d 种）" % len(snap.dependency_edge_labels),
             "`%s`" % " / ".join(snap.dependency_edge_labels)],
        ]),
        "",
        "**2.1 依赖边清单来源**：`graph_contract.dependency_edge_labels()`"
        "（契约，单一来源）。本仓库曾因四处各抄一份清单而产生分歧，"
        "其中两处漂移到实际错误，故清单只有一个出处。",
        "",
        "**2.2 跨表可交叉校验**：本报告全部表格取自同一次会话、同一快照时刻。"
        "活图谱正被 ETL 持续改写，不同时刻导出的数字会不同，这是正常的；"
        "但同一份报告内部若出现矛盾，则是缺陷。",
        "",
        "**2.3 完整字段见随附 CSV**。本 Markdown 呈现的是审阅所需的列；"
        "机器可读的完整字段集（含 `confidence`、`source`、`last_seen`、"
        "`verify_experiment`）在同名 CSV 中，两者取自同一快照。",
    ])


def _criteria_section(tn: _TableNumberer) -> str:
    return "\n".join([
        "## 3 适用标准（Applicable Criteria）",
        "",
        "ISAE 3000 §69(d) 要求识别适用标准。下表把条文依据集中成交叉引用，"
        "而不是散在各节的括号里 —— 散落的引用无法被逐条核对。",
        "",
        tn.caption("适用标准与本报告对应节"),
        "",
        _md_table(["条文依据", "该条要求什么", "本报告对应节"],
                  [[a, b, c] for a, b, c in APPLICABLE_CRITERIA]),
    ])


def _glossary_section(tn: _TableNumberer) -> str:
    return "\n".join([
        "## 4 术语与取值定义",
        "",
        "正式文档必须定义自己用的每个取值。本节的取值词汇刻意对齐外部标准"
        "（ITS (EU) 2024/2956 的枚举措辞、NIST OSCAL 的取证方法、"
        "SOC 2 的结论措辞），而不使用内部实现词。",
        "",
        tn.caption("术语与取值定义"),
        "",
        _md_table(["术语 / 取值", "定义"], [[a, b] for a, b in GLOSSARY]),
    ])


def _mapping_section(snap, tn: _TableNumberer) -> str:
    """§5 依赖关系映射 —— 列设计对齐 SOC 2 Section IV。

    列的取舍：原版 10 列里 `conf`（confidence）实际全为空，一整列 `—` 让读者
    以为渲染坏了；`source` 与 `last_seen` 属取证细节，移入随附 CSV。留下的
    8 列是审阅一条依赖是否可信所必需的最小集。
    """
    parts = ["## 5 依赖关系映射（按业务功能）", "",
             "对应 DORA Art. 8(4)、SYSC 15A.4.1R 的 technology 维度。"
             "列设计对齐 SOC 2 Section IV「tests of controls and results of "
             "tests」的惯例：先声明取证方法，再给结论，例外单列。", ""]

    by_cap: Dict[str, List[Dict[str, Any]]] = {}
    for r in snap.function_mapping:
        by_cap.setdefault(r["capability"], []).append(r)
    reach = {r["capability"]: r for r in getattr(snap, "reachability", [])}

    for cap in sorted(by_cap):
        rows = by_cap[cap]
        tier = rows[0].get("tier") or "未分级"
        rc = reach.get(cap, {})
        hops = rc.get("reachable_objects")
        parts += [
            "### 5.%d %s" % (sorted(by_cap).index(cap) + 1, cap),
            "",
            "重要性分级 `%s`；一跳直接依赖 **%d** 条；多跳可达 **%s** 个对象。"
            % (tier, len(rows), hops if hops is not None else "未统计"),
            "",
            tn.caption("%s 的一跳直接依赖" % cap),
            "",
            _md_table(
                ["#", "依赖方", "依赖类型", "依赖对象标识符", "对象类型",
                 "范围", "取证方法", "结论", "例外与备注"],
                [[i, r["service"], r["edge_type"],
                  "`%s`" % r["target"], r["target_label"],
                  r.get("target_scope") or MISSING_LABEL,
                  _evidence(r)[0], _evidence(r)[1], _note(r)]
                 for i, r in enumerate(rows, 1)]),
            "",
        ]
    return "\n".join(parts)


def _breakdown_section(bd: Breakdown, tn: _TableNumberer) -> str:
    parts = ["## 6 证据状态分列统计", "",
             "三个维度回答三个不同问题，**刻意不合并成单一比率**："
             "平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消，"
             "而那是监管审查最容易挑的点。", ""]
    for i, (_field, title, why, buckets) in enumerate(bd.dimensions(), 1):
        parts += ["### 6.%d %s" % (i, title), "", "%s。" % why, "",
                  tn.caption(title),
                  "",
                  _md_table(["取值", "条数", "占比"],
                            [[b.value, b.count, b.share_pct] for b in buckets]),
                  ""]
    return "\n".join(parts)


#: 局限披露节的标题。**导出为常量**，因为 demo 页面要按它切片取出这一节。
#:
#: 让页面硬编码一份标题字符串，就是又抄了一份清单 —— 本仓库因此吃过亏（同一份
#: 依赖边清单四处各抄一份，其中两处漂移到实际错误）。这里的失效形状更隐蔽：
#: 标题改了而页面没改，页面会**静默显示「（未生成）」**，既不报错也不缺页。
LIMITATIONS_HEADING = "## 10 重大固有局限性与范围排除"


def _limitations(snap, bd: Breakdown, tn: _TableNumberer) -> str:
    """§10 重大固有局限性与范围排除 —— 全部由快照数据算出，不手写。

    对应 ISAE 3000 §69(e)（重大固有局限性的描述）与 SYSC 15A.6.1R(7)
    （vulnerabilities 及整改行动与时限理由）。
    """
    lines = [LIMITATIONS_HEADING, "",
             "本节对应 ISAE 3000 §69(e) 与 SYSC 15A.6.1R(7)。"
             "每一条的数字均由本次快照现算，不是手写的固定文本 —— "
             "手写的披露会过期。", ""]

    confirmed = bd.bucket("verify_status", "confirmed")
    n_conf = confirmed.count if confirmed else 0
    pct = confirmed.share_pct if confirmed else "0%"
    missing_verify = bd.bucket("verify_status", MISSING_LABEL)
    n_missing = missing_verify.count if missing_verify else 0
    lines += [
        "**10.1 证据覆盖率 %s。** %d 条依赖里经实测确证（TEST/Confirmed）仅 %d 条，"
        "%d 条未测试。整改方向为扩大故障注入覆盖，"
        "当前受限于注入手段对 agent 层与部分托管服务的可达性。"
        % (pct, bd.total, n_conf, n_missing),
        "",
    ]

    missing_kind = bd.bucket("dependency_kind", MISSING_LABEL)
    if missing_kind:
        lines += [
            "**10.2 %d 条边的 `dependency_kind` 为未评估。** 这些是边类型分类新近"
            "变更、尚待下轮 ETL 补标的边。该取值表示「尚未打标」，"
            "不表示「不适用」。" % missing_kind.count,
            "",
        ]

    lines += [
        "**10.3 SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and "
        "document the people, processes, technology, facilities and information "
        "necessary to deliver each important business service：",
        "",
        tn.caption("SYSC 15A.4.1R 六要素的覆盖状态"),
        "",
        _md_table(["要素", "状态", "依据"],
                  [[e, s, w] for e, s, w in SYSC_15A_ELEMENTS]),
        "",
    ]

    no_tolerance = [r for r in snap.capability_meta
                    if r.get("impact_tolerance_seconds") is None]
    if no_tolerance:
        lines += [
            "**10.4 impact tolerance 未设定（%d/%d 个业务功能）。** SYSC 15A.2.5R "
            "要求 firm *must* set an impact tolerance for each important business "
            "service。**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有"
            "阈值，不能用「没检测到越界」掩盖「压根没有阈值可比」。"
            % (len(no_tolerance), len(snap.capability_meta)),
            "",
        ]

    lines += [
        "**10.5 承载层不在依赖表内（范围排除）。** 承载/放置类关系普遍为真、"
        "不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS "
        "集群与负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT "
        "服务 —— 故单列于 §8 并注明性质。",
        "",
        "**10.6 集中度是技术集中度，不是供应商集中度（范围排除）。** DORA "
        "Art. 29/31 要的是供应商层面的集中度与分包链，需要法人实体节点才能回答；"
        "本平台当前只有技术对象。第四方分包链在埋点边界外，结构上做不到。",
        "",
        "**10.7 未采用 ITS B_05.02 的 `Rank` 分层（范围排除）。** 法定模版用 "
        "`Rank`（直接第三方=1，分包商逐级>1）表达供应链层级。本报告的多跳可达数"
        "是技术依赖深度，与 Rank 的法人分包语义不同，**刻意不混用该字段名**。",
        "",
        "**10.8 无历史版本查询能力。** SYSC 15A.6.2R 要求保存 each version 满 "
        "6 年。本报告通过「文件名带快照时刻、不覆盖历史」满足留存，"
        "但图谱本身无双时态，无法回答「三个月前这条依赖是什么状态」。",
    ]
    return "\n".join(lines)


def _appendix_method(snap, tn: _TableNumberer) -> str:
    """附录 A —— 所执行工作摘要，对应 ISAE 3000 §69(k) 与 SYSC 15A.6.1R(3)(9)。"""
    return "\n".join([
        "## 附录 A 编制方法与所执行工作摘要",
        "",
        "对应 ISAE 3000 §69(k)（所执行工作的信息性摘要）与 SYSC 15A.6.1R(3)(9)"
        "（mapping 方法与所用方法论的书面记录）。",
        "",
        "**A.1 数据获取。** 对 `%s` 执行只读 openCypher 查询。导出层不含任何写"
        "操作，由 `tests/test_54_compliance_export.py::m07` 静态扫描锁定 —— "
        "生成合规报告的过程若会改动被报告的对象，报告本身就不可采信。"
        % snap.endpoint,
        "",
        "**A.2 依赖边的判定口径。** 依赖边类型清单取自契约函数，共 %d 种；"
        "承载/放置类关系不计入，且经门禁校验两集合零重叠。"
        % len(snap.dependency_edge_labels),
        "",
        "**A.3 一跳与多跳分开呈现。** 报告主体用一跳直接依赖 —— 每条边有明确的"
        "源、目标、观测来源与证据等级，是可归责的单位。多跳可达数作为补充统计，"
        "回答「这项业务功能一共牵连多少资产」，两种口径刻意不合并。",
        "",
        "**A.4 取证方法的判定。** `TEST` 表示已执行故障注入实验；`EXAMINE` 表示"
        "仅审阅运行时遥测与配置。词汇取自 NIST OSCAL `observation.method`，"
        "其强度序列 TEST > EXAMINE > INTERVIEW 源自 NIST SP 800-53A。",
        "",
        "**A.5 快照一致性。** 基准时刻在快照构造时取一次，全部表格共用。"
        "由 `m05` 以 AST 扫描锁定「`take_snapshot` 内取当前时刻恰好一次」—— "
        "分次取时刻会让同一份报告的不同表落在不同基准日上。",
        "",
        "**A.6 本报告的自动化程度。** 全部数字与披露文本由代码从快照现算，"
        "无人工编辑环节。因此本报告不含人工判断，也不含对数字合理性的复核 —— "
        "该复核是 SYSC 15A.7.1R 所要求的 governing body 审批环节的内容。",
    ])


def render_markdown(snap, bd: Breakdown,
                    doc_control: Optional[Dict[str, str]] = None) -> str:
    tn = _TableNumberer()
    parts = [
        _doc_control(snap, doc_control), "",
        _nature_statement(), "",
        _basis(snap), "",
        _criteria_section(tn), "",
        _glossary_section(tn), "",
        _mapping_section(snap, tn), "",
        _breakdown_section(bd, tn), "",
    ]

    # ── §7 技术集中度 ──
    total_caps = snap.capability_count
    parts += [
        "## 7 技术集中度",
        "",
        "对应 SYSC 15A.2.7G(10) 的原文要求：评估 *the potential aggregate impact "
        "of disruptions to multiple important business services, in particular "
        "where such services rely on common operational resources as identified "
        "by the firm's mapping exercise*。",
        "",
        tn.caption("被多个业务功能共同依赖的对象"),
        "",
        _md_table(
            ["被依赖对象标识符", "对象类型", "范围", "支撑业务功能数", "边数"],
            [["`%s`" % r["target"], r["target_label"],
              r.get("target_scope") or MISSING_LABEL,
              "%s / %s" % (r["capability_count"], total_caps), r["edge_count"]]
             for r in snap.concentration]),
        "",
    ]

    # ── §8 承载层 ──
    parts += [
        "## 8 基础设施承载层（单列，非服务消费关系）",
        "",
        "本节所列边**不是**依赖。它们普遍为真、不携带判别信息，故不进依赖表；"
        "但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，故在此列出，"
        "以免读者误以为被遗漏。范围排除的理由见 §10.5。",
        "",
        tn.caption("承载/放置类关系"),
        "",
        _md_table(["边类型", "条数", "性质"],
                  [[r["edge_type"], r["count"], r["nature"]]
                   for r in snap.hosting_layer]),
        "",
    ]

    # ── §9 业务功能与容忍度阈值 ──
    def _thr(v: Any) -> str:
        return "未设定（Not defined）" if v is None else str(v)

    parts += [
        "## 9 业务功能与容忍度阈值",
        "",
        "阈值取值词汇对齐 ITS (EU) 2024/2956 B_06.01.0080/0090 的口径（见 §4）。",
        "",
        tn.caption("业务功能的重要性分级与容忍度阈值"),
        "",
        _md_table(
            ["业务功能", "重要性分级", "impact_tolerance_seconds",
             "rto_target_seconds"],
            [[r["capability"], r.get("tier") or "未分级",
              _thr(r.get("impact_tolerance_seconds")),
              _thr(r.get("rto_target_seconds"))]
             for r in snap.capability_meta]),
        "",
        "**9.1 两个阈值必须分列。** RTO 是恢复某个流程的目标时间（内部视角）；"
        "impact tolerance 是重要业务服务的最大可容忍中断（外部危害视角）。"
        "两者可以差数倍，混用是监管审查重点。",
        "",
    ]

    parts += [_limitations(snap, bd, tn), "", _appendix_method(snap, tn)]
    return "\n".join(parts) + "\n"


#: CSV 产出的列顺序。`verify_status` 与 `dependency_kind` 是强制列，
#: 由 tests/test_54_compliance_export.py::m02 锁定。
#:
#: CSV 刻意保留技术字段名而不改成中文列名 —— 它是机器可读的完整记录，
#: 消费方是脚本与审计取证工具，字段名稳定比可读性重要。人类可读的呈现在
#: Markdown 里（见 §2.3 的说明）。
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
                 basename: Optional[str] = None,
                 doc_control: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """写出一份完整报告包。返回 {kind: path}。

    默认文件名带快照时刻 —— SYSC 15A.6.2R 要求保存 **each version** 的记录 6 年，
    同名覆盖会让历史版本消失。

    `basename` 用于**样例**：样例要能被 diff、被覆盖更新，所以用固定名。
    正式导出**不要**传它。快照时刻仍写在文件内容里（文档控制块 + CSV 注释行），
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
        f.write(render_markdown(snap, bd, doc_control))
    paths["markdown"] = md_path
    csv_path = os.path.join(out_dir, csv_name)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(render_csv(snap))
    paths["csv"] = csv_path
    return paths

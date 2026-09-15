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
     "识别与第三方的 interconnections", "§7 第三方范围分列"),
    ("FCA SYSC 15A.4.1R",
     "identify and document people/processes/technology/facilities/information",
     "§5、§11"),
    ("FCA SYSC 15A.5.3R",
     "carry out scenario testing 的测试证据", "§5 取证方法与结论"),
    ("FCA SYSC 15A.2.5R",
     "must set an impact tolerance for each IBS", "§10（本报告披露为未设定）"),
    ("FCA SYSC 15A.2.7G(10)",
     "common operational resources 的聚合影响", "§8 技术集中度"),
    ("FCA SYSC 15A.6.1R(3)",
     "mapping 方法与如何支撑测试的书面记录", "附录 A"),
    ("FCA SYSC 15A.6.1R(7)",
     "vulnerabilities 及整改行动与时限理由", "§11"),
    ("FCA SYSC 15A.6.2R",
     "retain each version …… at least 6 years", "文档控制（文件名带快照时刻）"),
    ("FCA SYSC 15A.7.1R",
     "governing body 批准并定期审查该书面记录", "文档控制（待签署）"),
    ("BCBS Principles for Operational Resilience 原则四",
     "mapping interconnections and interdependencies", "§5"),
    ("《关键信息基础设施安全保护条例》第九条",
     "识别关键业务及其依赖的网络设施与信息系统", "§5"),
    ("ITS (EU) 2024/2956 B_06.01",
     "functions identification 的字段与枚举词汇（借用词汇，非报送）", "§4、§10"),
    ("NIST OSCAL observation.method",
     "TEST / EXAMINE / INTERVIEW 取证方法受控词汇", "§4、§5"),
    ("ISAE 3000 (Revised) §69(e)",
     "重大固有局限性的描述", "§11"),
)

#: 术语与取值定义。正式文档必须定义自己用的每个取值 —— 否则读者只能猜。
GLOSSARY: Tuple[Tuple[str, str], ...] = (
    ("切断手段（severance）",
     "得出 `Confirmed` 所使用的故障注入手段，决定该结论的**适用范围**。"
     "本轮出现两种：`iam-deny`（注入期间该依赖调用全程返回 AccessDenied）与 "
     "`rds-reboot`（数据库实例重启，实测中断仅约 16~18 秒）。"
     "**两者不等价** —— 后者不支撑「数据库长时间或彻底不可用」场景下的结论。"
     "逐手段的范围定义见 §6。未记录该字段的 `Confirmed` 视为范围不明。"),
    ("证据通道（evidence channel）",
     "判定所依据的观测来源。`xray-edge+business-probe` 为消费方侧调用统计加"
     "业务功能探针的双通道；`rds-event+business-probe` 表示**消费方侧没有调用"
     "遥测**，注入生效性改由资源自身事件（RDS `DB instance shutdown`/"
     "`restarted`）证明。后者是一个已披露的观测缺口，不是等价替代。"),
    ("依赖对象标识符",
     "AWS 物理资源 ID、Kubernetes 服务名、AWS 服务端点名或 agent 工具键，"
     "按对象类型而定。本平台**不生成**另一套展示名 —— 图谱里没有的名称不会被"
     "编造出来，标识符即该对象在其所属系统中的真实身份键。"),
    ("依赖类型",
     "契约定义的依赖边类型，来源为 `graph_contract.dependency_edge_labels()`。"
     "承载/放置类关系不在此列，单列于 §9。"),
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


#: 切断手段 → (中文名, 证据范围, 是否瞬时)。
#:
#: ## 为什么必须逐手段披露而不是一律写 confirmed
#:
#: 2026-09-15 之前，`_evidence()` 对每一条 confirmed 都返回同一句
#: `Confirmed — no exceptions noted`。而这 16 条 confirmed 的证据是**异质**的：
#:
#:   iam-deny    deny 期间调用**全程**失败 —— 覆盖「依赖不可用」
#:   rds-reboot  实例重启，实测中断仅约 **16~18 秒** —— **不覆盖**「数据库彻底不可用」
#:
#: 用 SOC 2 的「no exceptions noted」去描述一次 16 秒的重启测试，读者会以为
#: 这条依赖被完整验证过。那是过度声称 —— 存 `verify_severance` 的全部意义
#: 就是让报告披露这个差别，不披露就等于没存。
SEVERANCE_SCOPE: Dict[str, Tuple[str, str, bool]] = {
    "source-audit": (
        "源码/IaC 审计",
        "**未做故障注入**。结论是「这个调用在代码里不存在」——确定性证据，"
        "但性质与故障注入完全不同：它证明的是**不存在依赖**，"
        "不是「依赖存在且承重」。因此这类边被排除在可评估分母之外，"
        "而不是计入 confirmed。"
        "**不覆盖**以下情形：(a) 部署镜像与被审源码树不一致 —— "
        "本审计读的是仓库，不是运行中的镜像，若镜像来自另一个提交则结论可能失效；"
        "(b) 非源码路径发起的调用（sidecar、自动注入的 agent、运行期加载的插件）；"
        "(c) 反射或动态构造的调用；"
        "(d) IaC 层的连线（如由 EventBridge 触发而非被应用代码调用）。"
        "对具名 RDS 实例的判定还**无法判断**故障转移后的角色变化 —— "
        "结论只在所附角色快照下成立",
        False),
    "iam-deny": (
        "IAM 拒绝",
        "注入期间该依赖的调用**全程**返回 AccessDenied，覆盖「依赖不可用」场景；"
        "不覆盖延迟升高与部分失败",
        False),
    "rds-reboot": (
        "数据库实例重启",
        "实例重启造成的**瞬时**中断（实测约 16~18 秒），覆盖「短暂丢失」场景；"
        "**不覆盖**「数据库长时间或彻底不可用」，亦不覆盖延迟升高",
        True),
    "k8s-service-blackhole": (
        "服务选择器黑洞",
        "把 Kubernetes Service 的选择器改为不匹配任何 Pod，端点清空后调用方连接"
        "**立即失败**，覆盖「依赖不可用」场景；不覆盖延迟升高与部分失败。"
        "⚠️ 该手段切断的是**服务发现与路由**，被依赖服务本身仍在运行 —— "
        "因此不覆盖「被依赖服务崩溃或返回错误响应」这一类失效",
        False),
    "unspecified": (
        "未声明",
        "⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围",
        True),
}

#: 证据通道 → 说明。
#:
#: ## ⚠️ 这个字段里存着**两套不兼容的词汇**
#:
#: 2026-09-15 实测发现，`verify_evidence_channel` 同时承载两个不同问题的答案：
#:
#:   旧词汇（`chaos/code/runner/result.py`，Chaos Mesh 期）
#:       success_rate / throughput_only / both / none
#:       回答「**哪个指标**显示了退化」
#:   新词汇（IAM deny 与 RDS 故障实验器）
#:       xray-edge+business-probe / rds-event+business-probe
#:       回答「用了**哪个观测来源**」
#:
#: 这是一个应当上报的数据模型缺陷。**但不能靠改写历史值来消除** ——
#: 那些判定不是本轮采集的，重写它们等于伪造证据来源。
#: 正确做法是两套都登记、按其本义解读，并在 §11 披露该字段语义不统一。
EVIDENCE_CHANNEL_NOTE: Dict[str, str] = {
    # 新词汇：观测来源
    "xray-edge+business-probe":
        "【观测来源】消费方侧调用统计（X-Ray）＋ 源服务业务功能探针，双通道",
    "rds-event+business-probe":
        "【观测来源】⚠️ **消费方侧无调用遥测**；生效性由资源自身事件（RDS "
        "`DB instance shutdown`/`restarted`）证明，业务影响由探针证明",
    # 旧词汇：退化体现在哪个指标（语义与上面两项**不同轴**）
    "both":
        "【退化指标】成功率与吞吐**同时**塌陷。注意：该取值来自早期实验的另一套"
        "词汇，描述的是指标而非观测来源，与本节前两项不同轴",
    "throughput_only":
        "【退化指标】仅吞吐塌陷、成功率未变（`abort` 类故障不产生响应行）。"
        "同属早期词汇，描述指标而非观测来源",
    "success_rate":
        "【退化指标】仅成功率下降。同属早期词汇，描述指标而非观测来源",
    "none":
        "⚠️ 【退化指标】两个指标都未见退化 —— 该 `confirmed` 的依据需人工复核",
    "unknown": "⚠️ 未记录证据通道",
}


#: verify_status 取值 → (取证方法, 结论措辞)。
#: 取证方法用 NIST OSCAL `observation.method` 的受控词汇；结论措辞用 SOC 2
#: Section IV 的惯例。未测试的边若有观测来源，其取证方法是 EXAMINE 而非「无」——
#: ETL 从遥测里看到过这条边，那是证据，只是强度低于实测。
def _evidence(row: Dict[str, Any]) -> Tuple[str, str]:
    status = row.get("verify_status")
    if status == "confirmed":
        sev = (row.get("verify_severance") or "unspecified")
        name, _scope, transient = SEVERANCE_SCOPE.get(
            sev, (sev, "未登记的切断手段", True))
        # 瞬时手段不得用「no exceptions noted」—— 那是「已完整验证」的措辞。
        if transient:
            return "TEST", ("Confirmed（%s，范围受限）" % name)
        return "TEST", ("Confirmed（%s）— no exceptions noted" % name)
    if status == "inconclusive":
        return "TEST", "Inconclusive — 已注入故障，观测退化不足以判定"
    if status == "modeling_artifact":
        # 取证方法是 EXAMINE 而非 TEST：源码/IaC 审计是**检查**，不是测试。
        # 用 TEST 会让读者以为做过故障注入。
        #
        # 措辞刻意不写 "not a dependency" 而写「建模产物」：边仍在图里、
        # 仍在总数里，被排除的只是**可评估分母**。两者的区别必须能从
        # 措辞上看出来，否则读者会以为图谱被删过。
        return "EXAMINE", "建模产物 — 源码/IaC 审计证明该调用不存在"
    if status:
        return "TEST", str(status)
    if row.get("source"):
        return "EXAMINE", "Not tested — assessment not performed"
    return "未取证", "Not tested — assessment not performed"


def _note(row: Dict[str, Any]) -> str:
    """例外与备注列。只在确有内容时写，避免产出一整列空值。

    一整列 `—` 比没有这一列更糟 —— 读者会以为渲染坏了，或以为该字段永远无值。

    ## confirmed 的范围限制必须落在这一列

    SOC 2 Section IV 的「例外」列正是披露「结论成立但适用范围有限」的地方。
    一条用 16 秒实例重启验证出来的 confirmed，如果这一列是空的，
    读者只会看到结论、看不到边界 —— 那是过度声称。
    所以瞬时手段与缺失消费方遥测这两件事，都必须在这里写明。
    """
    bits = []
    drift = row.get("drift_status")
    if drift and drift != "ok":
        bits.append("漂移 `%s`" % drift)

    if row.get("verify_status") == "confirmed":
        sev = row.get("verify_severance") or "unspecified"
        name, scope, transient = SEVERANCE_SCOPE.get(
            sev, (sev, "未登记的切断手段，适用范围不明", True))
        if transient:
            bits.append("**范围限制**：%s" % scope)
        chan = row.get("verify_evidence_channel") or "unknown"
        if chan != "xray-edge+business-probe":
            bits.append(EVIDENCE_CHANNEL_NOTE.get(
                chan, "⚠️ 未登记的证据通道 `%s`" % chan))

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


def _severance_section(snap, tn: "_TableNumberer") -> str:
    """切断手段与证据范围 —— 让读者能查到每条 confirmed 到底覆盖了什么。

    ## 为什么这一节必须存在

    §5 里每条 confirmed 都注明了手段，但手段的**适用范围**需要一处集中定义，
    否则读者要靠猜。这一节做三件事：
    列出本次实际用过的手段、说明每种手段覆盖与**不覆盖**什么、
    统计各手段各验证了多少条边。

    ## 「不覆盖」比「覆盖」更重要

    监管审查问的是「你凭什么说这条依赖已验证」，而最容易被挑的正是
    「你的测试没覆盖到的场景被你算作已验证」。所以每一行都必须写出边界：
    `rds-reboot` 实测中断仅约 16~18 秒，它**不能**支撑「数据库彻底不可用」
    这一场景下的结论。
    """
    confirmed = [r for r in snap.function_mapping
                 if r.get("verify_status") == "confirmed"]
    if not confirmed:
        return ""

    counts: Dict[str, int] = {}
    chans: Dict[str, int] = {}
    for r in confirmed:
        counts[r.get("verify_severance") or "unspecified"] = counts.get(
            r.get("verify_severance") or "unspecified", 0) + 1
        chans[r.get("verify_evidence_channel") or "unknown"] = chans.get(
            r.get("verify_evidence_channel") or "unknown", 0) + 1

    unspec = counts.get("unspecified", 0)
    parts = [
        "## 6 切断手段与证据范围", "",
        "本节回答「凭什么说这条依赖已验证，以及该结论**不**适用于什么」。"
        "§5 每条 `Confirmed` 都注明了手段，手段的边界在此定义。", "",
        "**已验证的 %d 条边并非同质证据。**「已验证」不等于「已覆盖全部中断场景」——"
        "下表的「不覆盖」一列是本报告刻意突出的部分。" % len(confirmed), "",
    ]
    if unspec:
        parts += [
            "> **⚠️ %d / %d 条 `Confirmed` 未记录切断手段。** 这些判定由早期实验写入，"
            "边上没有 `verify_severance`，因此**无法判断其结论的适用范围** ——"
            "读者不应假定它们与本轮实验同等强度。本报告不为这些边补写手段："
            "那不是本轮采集的证据，追认手段等于伪造来源。整改方向见 §11。"
            % (unspec, len(confirmed)), "",
        ]
    parts += [
        tn.caption("切断手段的证据范围与覆盖边数"), "",
        _md_table(
            ["切断手段", "中文名", "验证边数", "该手段的证据范围与不覆盖之处"],
            [["`%s`" % k, SEVERANCE_SCOPE.get(k, (k, "", True))[0], n,
              SEVERANCE_SCOPE.get(k, (k, "未登记的切断手段，适用范围不明",
                                      True))[1]]
             for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]),
        "",
        tn.caption("证据通道分布"), "",
        _md_table(
            ["证据通道", "边数", "说明"],
            [["`%s`" % k, n, EVIDENCE_CHANNEL_NOTE.get(
                k, "⚠️ 未登记的证据通道")]
             for k, n in sorted(chans.items(), key=lambda kv: -kv[1])]),
        "",
        "**共同限制（适用于全部手段）**：本轮取证均为**可用性**维度的切断实验，"
        "不覆盖延迟升高、部分失败、数据正确性与容量耗尽等场景。"
        "因此本报告不对依赖做 hard／soft 分级 —— 分级需要延迟与部分失败场景的证据，"
        "而那些实验尚未进行。", "",
    ]
    return "\n".join(parts)


def _breakdown_section(bd: Breakdown, tn: _TableNumberer) -> str:
    parts = ["## 7 证据状态分列统计", "",
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
LIMITATIONS_HEADING = "## 11 重大固有局限性与范围排除"


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
        "**11.1 证据覆盖率 %s。** %d 条依赖里经实测确证（TEST/Confirmed）仅 %d 条，"
        "%d 条未测试。整改方向为扩大故障注入覆盖，"
        "当前受限于注入手段对 agent 层与部分托管服务的可达性。"
        % (pct, bd.total, n_conf, n_missing),
        "",
    ]

    # ── 证据来源缺口：比覆盖率数字更容易被审查挑到 ──
    #
    # 「16 条已验证」这个数字会被读成 16 条同等强度的证据。实际不是：
    # 一部分只做过约 16~18 秒的瞬时中断，一部分没有记录手段，
    # 还有一个字段承载了两套不同轴的词汇。这三件事都必须在这里说，
    # 而不是留给读者从 §5 的备注列里自己拼出来。
    conf_rows = [r for r in snap.function_mapping
                 if r.get("verify_status") == "confirmed"]
    unspec = [r for r in conf_rows if not r.get("verify_severance")]
    transient = [r for r in conf_rows
                 if SEVERANCE_SCOPE.get(r.get("verify_severance") or "", (None,
                                        None, False))[2]
                 and r.get("verify_severance")]
    legacy_chan = [r for r in conf_rows
                   if (r.get("verify_evidence_channel") or "") in
                   ("both", "throughput_only", "success_rate", "none")]
    if unspec or transient or legacy_chan:
        lines += [
            "**11.2 已验证的 %d 条边并非同等强度证据。** 「已验证」这一个计数掩盖了"
            "三个不同的缺口，逐项披露如下（详见 §6）：" % len(conf_rows),
            "",
        ]
        if unspec:
            lines += [
                "- **%d 条未记录切断手段**，因此其结论的适用范围不明。这些判定由早期"
                "实验写入。**本报告不为它们追认手段** —— 那不是本轮采集的证据。"
                "整改方向：重跑这些边的切断实验并记录手段，或将其降级回未评估。"
                % len(unspec), "",
            ]
        if transient:
            lines += [
                "- **%d 条仅做过瞬时中断测试**（数据库实例重启，实测中断约 16~18 秒）。"
                "该证据**不支撑**「数据库长时间或彻底不可用」场景下的结论。"
                "整改方向：补充长时中断场景的实验。" % len(transient), "",
            ]
        if legacy_chan:
            lines += [
                "- **`verify_evidence_channel` 字段语义不统一。** %d 条边的取值来自早期"
                "词汇（描述「哪个指标退化」），与本轮词汇（描述「哪个观测来源」）"
                "**不在同一语义轴上**。同一字段承载两套词汇会让筛选与统计失真。"
                "整改方向：拆成两个字段，或为历史值补记来源 —— 但不得靠推测回填。"
                % len(legacy_chan), "",
            ]

    missing_kind = bd.bucket("dependency_kind", MISSING_LABEL)
    if missing_kind:
        lines += [
            "**11.3 %d 条边的 `dependency_kind` 为未评估。** 这些是边类型分类新近"
            "变更、尚待下轮 ETL 补标的边。该取值表示「尚未打标」，"
            "不表示「不适用」。" % missing_kind.count,
            "",
        ]

    lines += [
        "**11.4 SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and "
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
            "**11.5 impact tolerance 未设定（%d/%d 个业务功能）。** SYSC 15A.2.5R "
            "要求 firm *must* set an impact tolerance for each important business "
            "service。**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有"
            "阈值，不能用「没检测到越界」掩盖「压根没有阈值可比」。"
            % (len(no_tolerance), len(snap.capability_meta)),
            "",
        ]

    lines += [
        "**11.6 承载层不在依赖表内（范围排除）。** 承载/放置类关系普遍为真、"
        "不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS "
        "集群与负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT "
        "服务 —— 故单列于 §8 并注明性质。",
        "",
        "**11.7 集中度是技术集中度，不是供应商集中度（范围排除）。** DORA "
        "Art. 29/31 要的是供应商层面的集中度与分包链，需要法人实体节点才能回答；"
        "本平台当前只有技术对象。第四方分包链在埋点边界外，结构上做不到。",
        "",
        "**11.8 未采用 ITS B_05.02 的 `Rank` 分层（范围排除）。** 法定模版用 "
        "`Rank`（直接第三方=1，分包商逐级>1）表达供应链层级。本报告的多跳可达数"
        "是技术依赖深度，与 Rank 的法人分包语义不同，**刻意不混用该字段名**。",
        "",
        "**11.9 无历史版本查询能力。** SYSC 15A.6.2R 要求保存 each version 满 "
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
        _severance_section(snap, tn), "",
        _breakdown_section(bd, tn), "",
    ]

    # ── §7 技术集中度 ──
    total_caps = snap.capability_count
    parts += [
        "## 8 技术集中度",
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
        "## 9 基础设施承载层（单列，非服务消费关系）",
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
        "## 10 业务功能与容忍度阈值",
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
    # 切断手段与证据通道必须进 CSV：审计取证要能按手段筛选与复核。
    # 只写进 Markdown 不够 —— 审阅者拿到的明细表若缺这两列，
    # 就无法回答「哪些 confirmed 只做过瞬时中断测试」。
    "verify_severance", "verify_evidence_channel",
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

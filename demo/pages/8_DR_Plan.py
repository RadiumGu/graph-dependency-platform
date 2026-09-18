"""
DR Plan — 灾备切换计划生成与查看

调用 dr-plan-generator 生成 AZ / Region / Service 级别的切换计划，以 Markdown 展示。
"""
import dataclasses
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

_PROJECT_ROOT = C.PROJECT_ROOT
_DR_ROOT = os.path.join(_PROJECT_ROOT, "dr-plan-generator")

C.page_setup("DR 计划", icon="🛡️")
C.sidebar()

# ── 预设场景 ──────────────────────────────────────────────────────────────────
# ── 预设场景 ──────────────────────────────────────────────────────────────────
#
# ## 默认必须落在**真的能出计划**的那个场景上（2026-09-06 修）
#
# 当时实测三种 scope 在真实图谱上的结果：
#
#     scope=az       source=apne1-az1（虚构名）      受影响服务 0
#     scope=az       source=ap-northeast-1a（真实）  受影响服务 0   ← 锚点匹配不上
#     scope=service  source=petsite                  受影响服务 6   ← 只有这个能用
#
# 两个独立问题：
#
# ① `apne1-az1` / `apne1-az2` / `apne1-az4` 是**虚构的 AZ 名**，图谱里是
#    `ap-northeast-1a` / `ap-northeast-1c` / `ap-northeast-1d`。这套虚构命名贯穿
#    整个 dr-plan-generator（examples / fixtures / tests / README / SKILL.md，
#    连 `graph/queries.py` 的 docstring 都写着「e.g. apne1-az1」），**没有别名
#    翻译层** —— 也就是说 AZ scope 从来只在合成 fixture 上验证过。
#
# ② 即使换成真实 AZ 名，AZ scope 的受影响服务仍是 0。
#
# ## ② 已在 2026-09-07 修好（上游 dr-plan-generator）
#
# 根因是**图模型与查询差一跳**：`Microservice` 从不直接 `LocatedIn` 一个 AZ
# （实测 1 跳可达服务数为 0）。挂在 AZ 上的是 `Pod`（808 条边）、`Subnet`、
# `LoadBalancer`、`EC2Instance`、`RDSInstance`。真实路径是两跳：
#
#     AZ <-[:LocatedIn]- Pod <-[:RunsOn]- Microservice
#
# 补上这一跳之后实测：
#
#     ap-northeast-1a  受影响服务 6
#     ap-northeast-1c  受影响服务 7   其中 trafficgenerator 是**单 AZ、全停**
#     ap-northeast-1d  受影响服务 0   （那个 AZ 真的只有 2 个资源）
#
# 而且上游现在会分开报「全停」与「降级」：petsite 有 96 个 pod 在 1a、160 个在
# 1c，掉一个 AZ 是降级；trafficgenerator 只有 1 个 pod 且只在 1c，1c 掉了它就
# 没了。计划产物里多了 `fully_lost_services` 与 `service_az_pods` 两个字段。
# 门禁 `tests/test_60_dr_az_scope_finds_services.py`。
#
# 所以本页的 AZ 场景现在是**可用**的。默认仍留在 `scope=service, source=petsite`
# —— 那是最容易一眼看懂的场景（6 个受影响服务、61 分钟 RTO）；
# AZ 场景用**真实** AZ 名，并在结果里把「全停 vs 降级」摆出来。
def _graph_azs() -> list:
    """AZ 选项从图谱取，不硬编码 —— 硬编码就是上面 ① 那个问题的来源。"""
    try:
        import _common as _c
        r = _c.gquery("MATCH (a:AvailabilityZone) RETURN a.name AS name ORDER BY name")
        rows = (r or {}).get("results", r) if isinstance(r, dict) else r
        azs = [x.get("name") for x in (rows or []) if isinstance(x, dict) and x.get("name")]
        if azs:
            return azs
    except Exception:  # noqa: BLE001
        pass
    return ["ap-northeast-1a", "ap-northeast-1c", "ap-northeast-1d"]


_AZS = _graph_azs()


def _az_with_most_impact(azs: list) -> str:
    """挑**承载服务最多、且最好含单 AZ 服务**的那个 AZ 作为预设源。

    原来是 `_AZS[0]` —— 按字母序取第一个。图谱返回的顺序是
    `ap-northeast-1a / 1c / 1d`，于是预设永远落在 1a。

    但 1a 上所有服务都跨 AZ，掉它只是**降级**；`trafficgenerator` 只有 1 个 pod
    且只在 **1c** —— 1c 才是唯一能看出「⛔ 全停 vs ⚠️ 降级」这个区分的场景。
    而 1d 实测只有 2 个资源、0 个服务，选它会得到一份空计划。

    展示站的默认值应该落在**能看出这个能力**的场景上。所以按图谱现算：
    优先选「有单 AZ 服务」的，其次选服务数最多的。不写死 —— 图谱变了它自动跟。
    """
    best, best_key = (azs[0] if azs else "ap-northeast-1a"), (-1, -1)
    try:
        import _common as _c
        r = _c.gquery(
            "MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:LocatedIn]->(z:AvailabilityZone) "
            "RETURN s.name AS svc, z.name AS az, count(DISTINCT p) AS pods")
        rows = r.get("results", []) if isinstance(r, dict) else (r or [])
        spread: dict = {}
        for x in rows:
            spread.setdefault(x.get("svc"), {})[x.get("az")] = x.get("pods") or 0
        for az in azs:
            here = [s for s, m in spread.items() if m.get(az)]
            single = [s for s in here
                      if len([a for a, n in spread[s].items() if n]) == 1]
            key = (len(single), len(here))     # 先看单 AZ 服务数，再看总数
            if key > best_key:
                best, best_key = az, key
    except Exception:  # noqa: BLE001
        pass                                   # 查不到就退回字母序第一个
    return best


_AZ_SRC = _az_with_most_impact(_AZS)
# 目标是**除源之外**的 AZ —— 原来写 `_AZS[1:3]`，源正好是 _AZS[0] 时才对；
# 现在源不再一定是第一个，得显式排除自己，否则会切到正在失守的那个 AZ。
_AZ_TGT = ",".join([a for a in _AZS if a != _AZ_SRC][:2]) or _AZ_SRC

PRESET_SCENARIOS = {
    f"服务故障：petsite（默认，实测可出计划）": {
        "scope": "service",
        "source": "petsite",
        "target": os.environ.get("REGION", "ap-northeast-1"),
    },
    f"AZ 故障：{_AZ_SRC} → {_AZ_TGT}": {
        "scope": "az",
        "source": _AZ_SRC,
        "target": _AZ_TGT,
    },
    "Region 故障：ap-northeast-1 → us-west-2": {
        "scope": "region",
        "source": "ap-northeast-1",
        "target": "us-west-2",
    },
}

# 本地示例（预生成，Neptune 不可达时展示）
EXAMPLE_PLAN_PATH = os.path.join(
    _DR_ROOT, "examples", "az-switchover-apne1-az1.md"
)

EXAMPLE_PLAN_JSON_PATH = os.path.join(
    _DR_ROOT, "examples", "az-switchover-apne1-az1.json"
)


def load_example_plan() -> str:
    """读取本地预生成的示例 Markdown 计划。"""
    if os.path.exists(EXAMPLE_PLAN_PATH):
        with open(EXAMPLE_PLAN_PATH, encoding="utf-8") as f:
            return f.read()
    return ""


def load_example_plan_json() -> dict:
    """读取本地预生成的示例 JSON 计划。"""
    if os.path.exists(EXAMPLE_PLAN_JSON_PATH):
        with open(EXAMPLE_PLAN_JSON_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def generate_dr_plan(scope: str, source: str, target: str, exclude: str) -> dict:
    """调用 dr-plan-generator 生成 DR 计划。

    ## 必须显式选定 workload profile（2026-09-06 修）

    实测这一页的「🚀 生成 DR 计划」**一直生成不出计划**，报的是：

        No workload profile configured. Pass --profile <profile.yaml> or set
        DR_PROFILE. There is deliberately no default: a wrong profile silently
        produces a plan pointing at the wrong domain, SSM keys and namespace,
        which looks correct until it is executed.

    `dr_profile.get_active_profile()` **刻意不提供默认值** —— 那个设计是对的：
    错的 profile 会静默生成一个指向错误域名 / SSM 键 / 命名空间的计划，
    看起来完全正常，直到真的去执行。所以修法**不是**给上游加默认，
    而是让这一页做出**显式**选择。

    这个展示站展示的是 petsite 这套负载，所以选 `profiles/petsite.yaml`。
    页面上会把这个选择显示出来 —— 观众有权知道计划是按哪份 profile 生成的。

    Returns:
        {"markdown": str, "json": dict, "error": str | None}
    """
    try:
        from dr_profile import set_active_profile

        prof_path = os.path.join(C.PROJECT_ROOT, "profiles", "petsite.yaml")
        if not os.path.exists(prof_path):
            return {"markdown": "", "json": {}, "validation_warnings": [],
                    "error": f"workload profile 不存在：{prof_path}"}
        set_active_profile(prof_path)

        from graph.graph_analyzer import GraphAnalyzer
        from output.json_renderer import JSONRenderer
        from output.markdown_renderer import MarkdownRenderer
        from planner.plan_generator import PlanGenerator
        from planner.rollback_generator import RollbackGenerator
        from planner.step_builder import StepBuilder
        from registry.registry_loader import get_registry
        from validation.plan_validator import PlanValidator

        registry = get_registry()
        analyzer = GraphAnalyzer(registry=registry)
        builder = StepBuilder()
        generator = PlanGenerator(analyzer, builder)

        exclude_list = [e.strip() for e in exclude.split(",") if e.strip()] or None

        plan = generator.generate_plan(
            scope=scope,
            source=source,
            target=target,
            exclude=exclude_list,
        )
        plan.rollback_phases = RollbackGenerator().generate_rollback(plan)

        report = PlanValidator().validate(plan)
        validation_warnings = [
            f"[{i.severity}] {i.message}" for i in report.issues
        ] if report.issues else []

        md = MarkdownRenderer().render(plan)
        plan_json = dataclasses.asdict(plan)

        return {
            "markdown": md,
            "json": plan_json,
            "validation_warnings": validation_warnings,
            "error": None,
            "plan_id": plan.plan_id,
            "estimated_rto": plan.estimated_rto,
            "estimated_rpo": plan.estimated_rpo,
            "affected_count": len(plan.affected_services),
            # 「全停」与「降级」分开传 —— 只给一个总数等于让人自己猜。
            # 上游 2026-09-07 起在 DRPlan 上给这两个字段（AZ 范围才有值；
            # region 范围恒空，因为整个 region 失守时所有 pod 都在范围内）。
            "fully_lost_services": list(
                getattr(plan, "fully_lost_services", []) or []),
            "service_az_pods": dict(
                getattr(plan, "service_az_pods", {}) or {}),
        }
    except Exception as exc:
        return {"markdown": "", "json": {}, "error": str(exc), "validation_warnings": []}


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("🛡️ DR 计划生成器")
st.markdown(
    "基于 Neptune 图谱生成灾备切换计划，支持 AZ / Region / Service 三种故障范围，"
    "输出分阶段的切换步骤、RTO/RPO 评估与回滚方案。"
)

with st.sidebar:
    st.header("生成配置")

    # 预设场景
    #
    # 默认**不再**是「自定义」+ 虚构 AZ 名。原实现的自定义分支硬编码
    # `apne1-az1` / `apne1-az2`，那两个名字在图谱里不存在（见文件顶部的说明），
    # 所以一打开点「生成」必然得到 0 个受影响服务 —— 页面看起来生成成功了，
    # 内容却是空的，这比报错更容易误导人。
    #
    # 现在默认落在实测能出计划的那个场景（`scope=service, source=petsite`
    # → 6 个受影响服务 / 4 个阶段 / RTO 61 分钟）。
    _preset_names = list(PRESET_SCENARIOS.keys())
    preset_name = st.selectbox(
        "预设场景",
        _preset_names + ["自定义"],
        index=0,
    )

    if preset_name != "自定义":
        preset = PRESET_SCENARIOS[preset_name]
        default_scope = preset["scope"]
        default_source = preset["source"]
        default_target = preset["target"]
        default_exclude = preset.get("exclude", "")
    else:
        # 自定义也从真实取值起步，不给虚构名
        default_scope = "service"
        default_source = "petsite"
        default_target = os.environ.get("REGION", "ap-northeast-1")
        default_exclude = ""

    scope = st.selectbox(
        "故障范围 (scope)",
        ["az", "region", "service"],
        index=["az", "region", "service"].index(default_scope),
    )
    source = st.text_input("故障源 (source)", value=default_source)
    target = st.text_input("DR 目标 (target)", value=default_target)
    exclude = st.text_input("排除服务（逗号分隔）", value=default_exclude, placeholder="petfood,trafficgenerator")

    generate_btn = st.button("🚀 生成 DR 计划", width="stretch", type="primary")
    show_example = st.button("📄 查看示例计划", width="stretch")

    st.markdown("---")
    st.markdown("**计划说明**")
    st.markdown(
        """
- **az**: AZ 级别切换
- **region**: 区域级别切换
- **service**: 单服务故障恢复

生成的计划包含：
- 影响面评估
- SPOF 风险标识
- 分阶段切换步骤
- RTO/RPO 估算
- 自动生成回滚方案
"""
    )

# ── 会话状态 ──────────────────────────────────────────────────────────────────
if "dr_plan_result" not in st.session_state:
    st.session_state["dr_plan_result"] = None

# ── 处理按钮 ──────────────────────────────────────────────────────────────────
if generate_btn:
    with st.spinner(f"正在生成 {scope.upper()} 级别 DR 计划…"):
        result = generate_dr_plan(scope, source, target, exclude)
    st.session_state["dr_plan_result"] = result

if show_example:
    example_md = load_example_plan()
    example_json = load_example_plan_json()
    # 改造要点（2026-09-05）：原实现把 RTO 13 / RPO 15 / affected 7 硬编码在这里，
    # 与示例 JSON 里的真实值不一致，也与任何新生成的计划不一致。
    # 现在一律从 JSON 现算，取不到就显示「—」而不是编一个数字。
    _ej = example_json if isinstance(example_json, dict) else {}
    _meta = _ej.get("metadata") if isinstance(_ej.get("metadata"), dict) else _ej

    def _pick(*keys, default="—"):
        for src in (_meta, _ej):
            for k in keys:
                if isinstance(src, dict) and src.get(k) not in (None, ""):
                    return src[k]
        return default

    _phases = _ej.get("phases") or []
    _affected = _pick("affected_count", "affected_services_count")
    if _affected == "—":
        svcs = _ej.get("affected_services") or _meta.get("affected_services") or []
        _affected = len(svcs) if isinstance(svcs, list) and svcs else "—"

    st.session_state["dr_plan_result"] = {
        "markdown": example_md,
        "json": example_json,
        "error": None,
        "validation_warnings": [],
        "plan_id": _pick("plan_id", default="（示例计划）"),
        "estimated_rto": _pick("estimated_rto", "estimated_rto_minutes", "rto"),
        "estimated_rpo": _pick("estimated_rpo", "estimated_rpo_minutes", "rpo"),
        "affected_count": _affected,
        "_phase_count": len(_phases) if isinstance(_phases, list) else None,
        "_is_example": True,
    }

# ── 展示结果 ──────────────────────────────────────────────────────────────────
result = st.session_state["dr_plan_result"]

if result is None:
    # 默认展示：说明 + 快速入门
    col1, col2 = st.columns(2)
    with col1:
        st.info(
            "点击左侧「🚀 生成 DR 计划」或「📄 查看示例计划」开始。\n\n"
            "DR 计划生成器将：\n"
            "1. 从 Neptune 提取受影响子图\n"
            "2. 按 Tier 分层拓扑排序\n"
            "3. 生成 Phase 0–4 切换步骤\n"
            "4. 自动生成回滚方案\n"
            "5. 静态验证（SPOF 检测）"
        )

    with col2:
        st.markdown("**支持的故障场景**")
        for name, cfg in PRESET_SCENARIOS.items():
            st.markdown(
                f"- **{name}**  \n"
                f"  `scope={cfg['scope']}` `source={cfg['source']}` → `target={cfg['target']}`"
            )

    # 展示预生成示例
    st.markdown("---")
    st.subheader("预生成示例")
    example_md = load_example_plan()
    if example_md:
        with st.expander("查看 AZ Switchover 示例计划（apne1-az1）", expanded=False):
            st.markdown(example_md)
    st.stop()

# ── 有结果时展示 ──────────────────────────────────────────────────────────────
if result.get("error"):
    st.error(f"DR 计划生成失败：{result['error']}")
    st.info(
        "常见原因：\n"
        "- Neptune 不可达（需要 VPC 内访问）\n"
        "- 故障范围参数不正确\n"
        "- dr-plan-generator 依赖缺失"
    )
    st.markdown("---")
    st.subheader("展示本地示例计划（Neptune 不可达时）")
    example_md = load_example_plan()
    if example_md:
        st.markdown(example_md)
    st.stop()

# 计划元信息
if result.get("_is_example"):
    st.info("📄 正在展示预生成示例计划（非实时生成）")

meta_c1, meta_c2, meta_c3, meta_c4 = st.columns(4)
meta_c1.metric("计划 ID", result.get("plan_id", "—"))
meta_c2.metric("估算 RTO", f"{result.get('estimated_rto', '—')} 分钟")
# RPO 为 None 是生成器**刻意**的输出，不是缺陷：
#   "RPO cannot be derived from configuration (aurora, dynamodb, s3, sqs).
#    The plan will say so rather than print a number that cannot be justified."
# 原来直接插值成 `None 分钟`，看起来像个 bug，反而把这份诚实抹掉了。
_rpo = result.get("estimated_rpo")
if _rpo is None:
    meta_c3.metric("估算 RPO", "无法推导", help=(
        "生成器刻意不给数字：数据层（aurora / dynamodb / s3 / sqs）的复制配置"
        "不足以推导出恢复点。给一个无法论证的数字比说「推导不出」更危险。"))
else:
    meta_c3.metric("估算 RPO", f"{_rpo} 分钟")
meta_c4.metric("受影响服务", result.get("affected_count", 0))

# ── 全停 vs 降级：这是 AZ 场景最该摆出来的一行 ────────────────────────────────
#
# 服务不是「位于」某个 AZ，而是「部分在」。掉一个 AZ，多 AZ 服务是**降级**，
# 单 AZ 服务才是**全停**。把两者混在一份「受影响服务」清单里，等于让运维在
# 「7 个服务受影响」和「1 个服务彻底没了」之间自己猜。
#
# 上游 2026-09-07 起在计划里给出 fully_lost_services 与 service_az_pods
# （见 dr-plan-generator/graph/queries.py 的 _services_hosted_in_az）。
_lost = result.get("fully_lost_services") or []
_pods = result.get("service_az_pods") or {}
if _lost:
    st.error(
        "**⛔ 这些服务会整体不可用（pod 只在失守的这个 AZ 上）：** "
        + "、".join(f"`{s}`" for s in _lost)
        + "\n\n其余受影响服务是**降级**而不是全停 —— 它们在别的 AZ 还有 pod 在跑。",
        icon="⛔",
    )
elif _pods:
    st.success(
        "**没有服务会整体不可用** —— 受影响的服务在别的 AZ 都还有 pod，"
        "掉这个 AZ 是**降级**（少一部分容量），不是全停。",
        icon="✅",
    )
if _pods:
    with st.expander(f"逐服务的 AZ pod 分布（{len(_pods)} 个服务，判断依据）"):
        st.caption(
            "上面那个「全停 / 降级」的结论就是从这张表得出的 —— "
            "摆出来让你能自己核对，而不必信结论。")
        _azs = sorted({a for v in _pods.values() for a in v})
        # 直接用 st.dataframe 是安全的：这张表每一列类型统一 ——
        # 服务名与判定是 str，各 AZ 列是 int（缺失填 0，不填「—」）。
        # 混类型列才会踩 pyarrow 的 ArrowInvalid，那种情况要用
        # 6_Root_Cause_Analysis.py 里的 show_table()。
        st.dataframe(
            C.df([
                {"服务": s, **{a: int(v.get(a, 0)) for a in _azs},
                 "判定": "⛔ 全停（单 AZ）" if s in _lost else "⚠️ 降级（跨 AZ）"}
                for s, v in sorted(_pods.items())
            ]),
            width="stretch", hide_index=True)

# 0 个受影响服务时必须说清楚 —— 否则页面呈现的是一份「看起来生成成功」的空计划。
# 这比报错更容易误导人：有计划 ID、有 RTO/RPO 数字、有阶段，唯独没有内容。
if not result.get("_is_example") and not result.get("affected_count"):
    st.warning(
        "**这份计划的受影响服务是 0 —— 它算不出内容。**\n\n"
        "AZ scope 曾经**总是**这样（2026-09-07 已修）：根因是图模型与查询差一跳，"
        "`Microservice` 从不直接 `LocatedIn` 一个 AZ，真实路径是 "
        "`AZ <-LocatedIn- Pod <-RunsOn- Microservice`。补上这一跳后实测：\n\n"
        "| scope | source | 受影响服务 |\n|---|---|--:|\n"
        "| `az` | `ap-northeast-1a` | **6** |\n"
        "| `az` | `ap-northeast-1c` | **7**（含 1 个单 AZ 全停）|\n"
        "| `az` | `ap-northeast-1d` | 0 —— 那个 AZ 真的只有 2 个资源 |\n"
        "| `service` | `petsite` | **6** |\n\n"
        "所以现在看到 0，最可能是**这个范围里确实没有跑着服务**"
        "（如 `ap-northeast-1d`），而不是查询坏了。"
        "换 `ap-northeast-1a` / `1c`，或左侧的「服务故障：petsite」预设，"
        "都能看到一份有内容的计划。\n\n"
        "⚠️ 另一个仍未修的老问题：`apne1-az1` 这套**虚构 AZ 名**贯穿 "
        "dr-plan-generator 的 examples / fixtures / tests / docs 且没有翻译层，"
        "图谱里是 `ap-northeast-1a/c/d`。本页的 AZ 选项从图谱现取，不受它影响。",
        icon="⚠️",
    )

# 验证警告
if result.get("validation_warnings"):
    with st.expander(
        f"⚠️ 验证警告 ({len(result['validation_warnings'])} 条)", expanded=True
    ):
        for w in result["validation_warnings"]:
            st.warning(w)

# 主展示区
tab_md, tab_json, tab_download = st.tabs(["Markdown 计划", "JSON 数据", "下载"])

with tab_md:
    if result.get("markdown"):
        st.markdown(result["markdown"])
    else:
        st.info("计划内容为空。")

with tab_json:
    if result.get("json"):
        st.json(result["json"])
    else:
        st.info("无 JSON 数据。")

with tab_download:
    st.markdown("**下载计划文件**")
    if result.get("markdown"):
        st.download_button(
            label="⬇️ 下载 Markdown 计划",
            data=result["markdown"],
            file_name=f"{result.get('plan_id', 'dr-plan')}.md",
            mime="text/markdown",
            width="stretch",
        )
    if result.get("json"):
        st.download_button(
            label="⬇️ 下载 JSON 计划",
            data=json.dumps(result["json"], indent=2, ensure_ascii=False, default=str),
            file_name=f"{result.get('plan_id', 'dr-plan')}.json",
            mime="application/json",
            width="stretch",
        )

# ── 多场景对比 ────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("预生成示例对比")

example_dir = os.path.join(_DR_ROOT, "examples")
md_files = [f for f in os.listdir(example_dir) if f.endswith(".md")] if os.path.isdir(example_dir) else []

if md_files:
    cols = st.columns(min(len(md_files), 3))
    for i, fname in enumerate(sorted(md_files)):
        fpath = os.path.join(example_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        with cols[i % len(cols)]:
            # 提取计划头部信息
            lines = content.split("\n")[:15]
            with st.expander(fname.replace(".md", ""), expanded=False):
                st.markdown("\n".join(lines))
                st.download_button(
                    f"⬇️ 下载 {fname}",
                    data=content,
                    file_name=fname,
                    mime="text/markdown",
                    key=f"dl_{fname}",
                )

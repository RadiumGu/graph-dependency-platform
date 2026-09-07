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
# 实测三种 scope 在真实图谱上的结果：
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
# ② 即使换成真实 AZ 名，AZ scope 的受影响服务仍是 0：锚点找不到
#    （`anchors=10, nodes=394` 但一个都没匹配）。profile 声明的服务名里有
#    `petadoptionshistory` / `pethistory-service` / `PetAdoptionStatusUpdater`
#    这些图谱里不存在的名字，而 `Microservice` 节点也不直接挂在 AZ 上
#    （路径是 Service→Pod→EC2→AZ）。这是 dr-plan-generator 侧的问题，
#    已记入 todo/demo-site-rebuild/PLAN.md，不在本页范围内修。
#
# 所以：默认场景改为 `scope=service, source=petsite`；AZ 场景保留但用**真实**
# AZ 名，并在结果里如实说明它当前算不出受影响服务。
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
_AZ_SRC = _AZS[0] if _AZS else "ap-northeast-1a"
_AZ_TGT = ",".join(_AZS[1:3]) if len(_AZS) > 1 else _AZ_SRC

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

# 0 个受影响服务时必须说清楚 —— 否则页面呈现的是一份「看起来生成成功」的空计划。
# 这比报错更容易误导人：有计划 ID、有 RTO/RPO 数字、有阶段，唯独没有内容。
if not result.get("_is_example") and not result.get("affected_count"):
    st.warning(
        "**这份计划的受影响服务是 0 —— 它算不出内容。**\n\n"
        "原因是 scope 锚定没有匹配到图谱里的任何服务，生成器退回了未过滤的子图。"
        "实测三种 scope 的结果：\n\n"
        "| scope | source | 受影响服务 |\n|---|---|--:|\n"
        "| `az` | `apne1-az1`（虚构名） | 0 |\n"
        "| `az` | `ap-northeast-1a`（真实名） | 0 |\n"
        "| `service` | `petsite` | **6** |\n\n"
        "AZ scope 目前算不出来，有两层原因:`apne1-az1` 这套 AZ 名在图谱里不存在"
        "（图谱是 `ap-northeast-1a/c/d`，而 dr-plan-generator 的 examples、"
        "fixtures、tests、docs 全用虚构名且没有翻译层）；即使换成真实名，"
        "profile 声明的服务锚点里有 `petadoptionshistory` / `pethistory-service` / "
        "`PetAdoptionStatusUpdater` 这些图谱里不存在的名字，而 `Microservice` "
        "节点也不直接挂在 AZ 上（路径是 Service→Pod→EC2→AZ）。\n\n"
        "**换左侧的「服务故障：petsite」预设可以看到一份真实计划。**",
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

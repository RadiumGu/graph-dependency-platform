"""
DR Plan — 灾备切换计划生成与查看

调用 dr-plan-generator 生成 AZ / Region / Service 级别的切换计划，以 Markdown 展示。
"""
import dataclasses
import json
import os
import sys

import streamlit as st

# ── 路径设置 ──────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, "..", ".."))
_DR_ROOT = os.path.join(_PROJECT_ROOT, "dr-plan-generator")
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")

for _p in [_PROJECT_ROOT, _DR_ROOT, _RCA_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault(
    "NEPTUNE_ENDPOINT",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
os.environ.setdefault("REGION", "ap-northeast-1")

st.set_page_config(page_title="DR 计划", page_icon="🛡️", layout="wide")

# ── 预设场景 ──────────────────────────────────────────────────────────────────
PRESET_SCENARIOS = {
    "AZ 故障：apne1-az1 → apne1-az2": {
        "scope": "az",
        "source": "apne1-az1",
        "target": "apne1-az2,apne1-az4",
        "exclude": "",
    },
    "AZ 故障（排除 petfood）": {
        "scope": "az",
        "source": "apne1-az1",
        "target": "apne1-az2",
        "exclude": "petfood",
    },
    "Region 故障：apne1 → usw2": {
        "scope": "region",
        "source": "ap-northeast-1",
        "target": "us-west-2",
        "exclude": "",
    },
    "服务故障：petsite": {
        "scope": "service",
        "source": "petsite",
        "target": "ap-northeast-1",
        "exclude": "",
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

    Returns:
        {"markdown": str, "json": dict, "error": str | None}
    """
    try:
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
    preset_name = st.selectbox(
        "预设场景",
        ["自定义"] + list(PRESET_SCENARIOS.keys()),
        index=0,
    )

    if preset_name != "自定义":
        preset = PRESET_SCENARIOS[preset_name]
        default_scope = preset["scope"]
        default_source = preset["source"]
        default_target = preset["target"]
        default_exclude = preset["exclude"]
    else:
        default_scope = "az"
        default_source = "apne1-az1"
        default_target = "apne1-az2"
        default_exclude = ""

    scope = st.selectbox(
        "故障范围 (scope)",
        ["az", "region", "service"],
        index=["az", "region", "service"].index(default_scope),
    )
    source = st.text_input("故障源 (source)", value=default_source)
    target = st.text_input("DR 目标 (target)", value=default_target)
    exclude = st.text_input("排除服务（逗号分隔）", value=default_exclude, placeholder="petfood,trafficgenerator")

    generate_btn = st.button("🚀 生成 DR 计划", use_container_width=True, type="primary")
    show_example = st.button("📄 查看示例计划", use_container_width=True)

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
    st.session_state["dr_plan_result"] = {
        "markdown": example_md,
        "json": example_json,
        "error": None,
        "validation_warnings": [],
        "plan_id": "dr-az-apne1az1-example",
        "estimated_rto": 13,
        "estimated_rpo": 15,
        "affected_count": 7,
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
meta_c3.metric("估算 RPO", f"{result.get('estimated_rpo', '—')} 分钟")
meta_c4.metric("受影响服务", result.get("affected_count", "—"))

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
            use_container_width=True,
        )
    if result.get("json"):
        st.download_button(
            label="⬇️ 下载 JSON 计划",
            data=json.dumps(result["json"], indent=2, ensure_ascii=False, default=str),
            file_name=f"{result.get('plan_id', 'dr-plan')}.json",
            mime="application/json",
            use_container_width=True,
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

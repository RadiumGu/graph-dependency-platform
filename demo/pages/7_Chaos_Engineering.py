"""
7_Chaos_Engineering.py — 故障注入与实验历史。

改造要点（2026-09-05）：
- 原版硬编码 **73 条** 实验清单，而 fault_catalog.yaml 实际是 **59 条**
  （chaosmesh 19 / fis 36 / fis_scenarios 4）→ 现在全部现算
- 原版的 `💥 执行实验` 按钮只隔一个 st.warning 就能 subprocess 打生产
  → 现在必须设 DEMO_ALLOW_INJECTION=1 才出现；线上刻意不设
- 原版服务名清单里有 petstatusupdater / pethistory 不在 KNOWN_SERVICES，
  导致服务维度页永远查不到它们 → 服务清单改为从图谱现取
"""
import glob
import os
import sys

# 页面被单独执行时 demo/ 不在 sys.path 上，显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("混沌工程", icon="💥")
C.sidebar()

st.title("💥 混沌工程")
st.markdown(
    "> 故障注入在这个项目里不是「韧性演练」，而是**依赖图的验证手段**："
    "对边 `A → B`，在 B 注入、观测 A。"
)

fc = C.fault_catalog_counts()
if not fc.get("available"):
    st.error(f"故障目录加载失败：{fc.get('error')}")
    st.stop()

raw = fc.get("raw", {})

# ── 目录概览（现算）──────────────────────────────────────────────────────────
m = st.columns(4)
m[0].metric("故障目录总数", fc["total"],
            help="从 `fault_catalog.yaml` 现算，代码里不出现计数字面量。")
m[1].metric("Chaos Mesh", fc["chaosmesh"],
            help="K8s CRD 形式的故障注入：Pod/网络/IO/时钟等，作用在集群内。")
m[2].metric("AWS FIS 单动作", fc["fis"],
            help="AWS 托管服务提供的单个注入动作，作用在云资源上。")
m[3].metric("FIS 复合场景", fc["fis_scenarios"],
            help="AZ / Region 级的组合场景：多个动作按编排一起注入。")

st.caption(
    "⚠️ 界面上曾长期显示 73 条——那是硬编码清单，与 `chaos/code/runner/fault_catalog.yaml` "
    f"的实际内容（{fc['total']} 条）不一致。现在这些数字每次都从 YAML body 现算。"
)

# ── 故障目录浏览 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("故障目录")

BACKEND_TABS = [
    (f"🕸️ Chaos Mesh（{fc['chaosmesh']}）", "chaosmesh"),
    (f"☁️ AWS FIS（{fc['fis']}）", "fis"),
    (f"🌐 FIS 复合场景（{fc['fis_scenarios']}）", "fis_scenarios"),
]
tabs = st.tabs([t[0] for t in BACKEND_TABS])

for tab, (_, key) in zip(tabs, BACKEND_TABS):
    with tab:
        entries = raw.get(key, []) or []
        if not entries:
            st.info("该后端下暂无条目。")
            continue

        cats = sorted({str(e.get("category", "未分类")) for e in entries})
        pick_cat = st.multiselect(
            "按类别筛选", cats, default=cats, key=f"cat_{key}"
        )
        rows = []
        for e in entries:
            if str(e.get("category", "未分类")) not in pick_cat:
                continue
            rows.append({
                "故障类型": e.get("type") or e.get("scenario_id") or "—",
                "类别": e.get("category", "—"),
                "AWS 动作 / CRD": e.get("fis_action_id") or e.get("crd") or "—",
                "Tier": e.get("tier", "—"),
                "前置要求": ", ".join(e.get("requires", []) or []) or "—",
                "说明": (e.get("description") or "")[:70],
            })
        st.dataframe(C.df(rows), width="stretch", hide_index=True)
        st.caption(f"显示 {len(rows)} / {len(entries)} 条。")

st.warning(
    "**一条重要的诚实说明**：目录里列出某个故障动作，**不等于**它在这个环境里能选出目标资源。"
    "已知实例：`aws:network:disrupt-vpc-endpoint` 的目标类型是 `aws:ec2:vpc-endpoint`，"
    "而 PetSite VPC 里只有一个无关端点，这个动作无从施加。"
    "另有一条 `fis_ec2_network_disrupt` 因为动作 ID 在 `aws fis list-actions` 里根本不存在而被删除。",
    icon="⚠️",
)

# ── 实验规格文件 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("实验规格")

EXP_DIR = os.path.join(C.PROJECT_ROOT, "chaos", "code", "experiments")
spec_files = sorted(glob.glob(os.path.join(EXP_DIR, "**", "*.yaml"), recursive=True))

if spec_files:
    verify_specs = [p for p in spec_files if "verify-edges" in os.path.basename(p)]
    s = st.columns(3)
    s[0].metric("实验规格文件", len(spec_files))
    s[1].metric("边验证专用", len(verify_specs),
                help="文件名以 `verify-edges-*` 开头 —— 这些实验的目的不是"
                     "「看系统扛不扛得住」，而是「这条依赖边到底成不成立」。")
    s[2].metric("目录分组", len({os.path.relpath(os.path.dirname(p), EXP_DIR) for p in spec_files}))

    if verify_specs:
        st.markdown("**边验证实验**——这批规格是专门用来证伪依赖边的：")
        st.dataframe(
            C.df([
                {
                    "规格文件": os.path.basename(p),
                    "分组": os.path.relpath(os.path.dirname(p), EXP_DIR),
                }
                for p in verify_specs
            ]),
            width="stretch", hide_index=True,
        )

    with st.expander(f"全部 {len(spec_files)} 个实验规格"):
        st.dataframe(
            C.df([
                {
                    "分组": os.path.relpath(os.path.dirname(p), EXP_DIR),
                    "文件": os.path.basename(p),
                }
                for p in spec_files
            ]),
            width="stretch", hide_index=True,
        )
else:
    st.info("未找到实验规格文件。")

# ── 实验历史（图谱）──────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("实验历史")

online = C.neptune_online()
if online:
    res = C.gquery(
        "MATCH (e:ChaosExperiment) "
        "RETURN e.name AS name, e.fault_type AS fault_type, e.result AS result, "
        "e.status AS status, e.target AS target, e.started_at AS started_at "
        "ORDER BY e.started_at DESC LIMIT 100"
    )
    if "error" in res:
        st.warning(f"查询失败：{res['error']}")
    else:
        rows = res["results"]
        st.success(f"🟢 实时 —— 图谱中共 {len(rows)} 条实验记录（最近 100）", icon="🟢")
        if rows:
            by_result: dict = {}
            for r in rows:
                k = str(r.get("result") or r.get("status") or "unknown")
                by_result[k] = by_result.get(k, 0) + 1
            rc = st.columns(len(by_result) or 1)
            for i, (k, v) in enumerate(sorted(by_result.items())):
                rc[i].metric(k, v)
            st.dataframe(C.df(rows), width="stretch", hide_index=True)
else:
    st.info(
        "🔵 离线快照模式 —— 实验历史存在 Neptune 的 `ChaosExperiment` 节点上，需要连接图谱才能查看。"
        "离线时可以看上面的故障目录与实验规格，以及"
    )
    C.page_link("pages/1_Edge_Verification.py", "→ 边验证页（有离线快照）")

# ── 运行器阶段说明 ────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("运行器的 6 个阶段")
st.caption("每个阶段都有对应的护栏，其中两处是踩过坑之后加的。")

PHASES = [
    ("Phase 0", "预检", "PolicyGuard 先跑且**失败即关闭**；确认无残留 CRD；记录 Pod 重启基线——"
                        "这是唯一能保证「注入尚未发生」的时点"),
    ("Phase 1", "注入前稳态", "3 次采样求均值；同时采集**观测方**基线（观测方才是边的证据）。"
                             "观测方流量不足不会让实验失败，只会导致判定为未定"),
    ("Phase 2", "注入", "按 backend 分派到 FIS 单动作 / FIS 场景 / Chaos Mesh"),
    ("Phase 3", "观测 + 护栏", "每 10 秒采样。停止条件可挂在**观测方**上——"
                              "因为被注入方本来就该坏，拿它做熔断判据没有意义"),
    ("Phase 4", "恢复", "**成功路径也显式删除 CRD**。「duration 到期会自动清理 CRD」这个假设已被实测推翻："
                       "残留 CRD 会把 tproxy 留在 Pod netns 里，污染下一轮基线"),
    ("Phase 5", "注入后稳态", "主判据是**容器重启次数差值**而非 readiness——"
                             "SLI 可能读到 100% 而被注入的 Pod 正在 CrashLoopBackOff"),
]
for pid, name, desc in PHASES:
    with st.container(border=True):
        cc = st.columns([1, 5])
        cc[0].markdown(f"**{pid}**\n\n{name}")
        cc[1].caption(desc)

st.info(
    "**「注入是否生效」这道闸**：判定证伪之前必须有「注入确实生效」的证据。"
    "没有这条证据时一律判未定——否则「故障根本没打进去」会被误读成「依赖不存在」，"
    "而那会导致删掉一条真实的边。",
    icon="🚦",
)

# ── 真实注入（默认关闭）──────────────────────────────────────────────────────
st.markdown("---")
st.subheader("发起实验")

if not C.INJECTION_ENABLED:
    st.error(
        "**真实故障注入在本界面已禁用。**\n\n"
        "这是刻意的：这个页面挂在公网入口后面，而注入按钮会对**生产环境**"
        "执行真实故障。改造前它只隔了一个提示框。\n\n"
        "需要在受控环境启用时，设置环境变量 `DEMO_ALLOW_INJECTION=1` 后重启服务。"
        "线上刻意不设该变量。",
        icon="🔒",
    )
    st.caption(
        "在命令行发起实验的方式：`cd chaos/code && python3 main.py run --file <规格文件> --dry-run`"
        "（去掉 `--dry-run` 才是真实注入）。"
    )
else:
    st.warning(
        "⚠️ `DEMO_ALLOW_INJECTION=1` 已设置——下面的按钮会对**真实环境**执行故障注入。",
        icon="⚠️",
    )
    if spec_files:
        rel = [os.path.relpath(p, EXP_DIR) for p in spec_files]
        chosen = st.selectbox("选择实验规格", rel)
        dry = st.toggle("🧪 Dry-run（仅演练，不真正注入）", value=True)
        if st.button(
            "🧪 执行 Dry-run" if dry else "💥 执行真实注入",
            type="primary" if dry else "secondary",
        ):
            st.info(
                f"请在命令行执行以下命令（本界面刻意不代为启动子进程）：\n\n"
                f"```bash\ncd {os.path.join(C.PROJECT_ROOT, 'chaos', 'code')}\n"
                f"python3 main.py run --file {os.path.join('experiments', chosen)}"
                f"{' --dry-run' if dry else ''}\n```"
            )
            st.caption(
                "改为「给出命令」而非直接 `subprocess.Popen`：演示界面持有一个能打生产的"
                "长生命周期子进程，风险与收益不成比例。"
            )

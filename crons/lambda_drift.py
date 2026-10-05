"""Lambda 代码漂移检测 —— 线上与源码的两个方向都要查。

## 为什么需要它

这个账号里同一族问题出现了**三次**，每次都是偶然撞见的：

1. **2026-09-26 petsite**：修复 2026-09-05 合并，镜像构建时间比修复提交
   早 3 分 53 秒 → 22 天跑着没有修复的镜像。
2. **2026-10-03 statusupdater**：`attribute_exists(petid)` 守卫在**线上**跑了
   7 天，而 `main` 里一直没有（PR 从未合并）→ 任何从源码重新部署都会
   静默回滚它。
3. **2026-10-04 etl_deepflow**：反方向 —— 契约门禁 2026-09-22 合并进仓库，
   而线上代码是 09-06 的，**门禁 12 天没上线**。那个提交的标题就是
   「它是唯一写图却无门禁的写入方」，于是这句话至今仍然成立。

三次都不是靠监控发现的。所以：**两个方向都要查**。

- 「线上有、源码没有」→ 重新部署会静默回滚修复
- 「源码有、线上没有」→ 所有人以为已修好，实际没在跑

## 判据的两个坑

- **不要用「逐字一致」做跨形态比较。** statusupdater 线上跑的是 esbuild
  打包产物（含 `__commonJS` 包装与内联依赖），与未打包源码永远不可能
  逐字相同。只在**同形态**（都是未打包 .py）时才比字节。
- **「函数比栈新」只说明栈外改过，不说明有风险。** 若栈外改动恰好是把
  线上对齐到仓库（实测 neptune-etl-trigger 就是），重新部署不会丢东西。
  所以方向 A 的结果必须再用方向 B 核实，不能单独当结论。
"""

from __future__ import annotations

import datetime as dt
import io
import subprocess
import zipfile

import boto3
import urllib3

REGIONS = ("ap-northeast-1", "ap-northeast-2")
REPO = "/home/ec2-user/works/graph-dependency-platform"

# 方向 B 的覆盖面：{函数名: (仓库里的主模块路径, 包内文件名)}
# ⚠️ 这个表就是覆盖面本身 —— 不在表里的函数**没有被方向 B 检查过**，
#    报告必须把这一点说出来，不能让人误以为全查过了。
SOURCE_MAP = {
    # value 是**成员列表**而不是单个 (path, member)。改成多成员的理由见下
    # 「为什么单成员会给出假绿」。
    "neptune-etl-trigger": (
        ("infra/lambda/etl_trigger/neptune_etl_trigger.py", "neptune_etl_trigger.py"),
    ),
    "neptune-etl-from-cfn": (
        ("infra/lambda/etl_cfn/neptune_etl_cfn.py", "neptune_etl_cfn.py"),
    ),
    "neptune-etl-from-deepflow": (
        ("infra/lambda/etl_deepflow/neptune_etl_deepflow.py", "neptune_etl_deepflow.py"),
    ),
    # etl_aws 是唯一模块化的 ETL（7 个文件）。2026-10-04 实测：只比
    # neptune_client.py 会**报绿**，而同一个包里有两个模块真在漂移 ——
    #     neptune_client.py        线上 ≡ 仓库     ← 原来只比这个
    #     neptune_client_base.py   线上 ✗（-1014B）← dc144da 凭据 None 保护
    #     business_layer.py        线上 ✗（-2096B）← 93e3120 清 6 条业务层假边
    # 所以必须比全部 7 个。
    "neptune-etl-from-aws": (
        ("infra/lambda/etl_aws/handler.py", "handler.py"),
        ("infra/lambda/etl_aws/neptune_client.py", "neptune_client.py"),
        ("infra/lambda/etl_aws/neptune_client_base.py", "neptune_client_base.py"),
        ("infra/lambda/etl_aws/business_layer.py", "business_layer.py"),
        ("infra/lambda/etl_aws/cloudwatch.py", "cloudwatch.py"),
        ("infra/lambda/etl_aws/config.py", "config.py"),
        ("infra/lambda/etl_aws/graph_gc.py", "graph_gc.py"),
    ),
    # ── 2026-10-04 补：这四个函数此前**完全不在表里** ────────────────────
    #
    # 加它们的直接原因是一次全量核对：把线上 Lambda 列表与本表对照，发现
    # 表里 5 个函数之外还有 4 个在写同一张图 / 读同一套查询，而它们**从未
    # 被内容比对检查过**。逐个实测，**四个全在漂移**：
    #
    #     neptune-etl-from-xray          +10337 B  3a383d2 服务名走真源解析，
    #                                              补出 GenAI 层与 12 条边
    #     neptune-etl-from-appsignals      +310 B  0b0408c scope 由写入方就地写
    #     neptune-etl-from-agentcore        -15 B  仅注释里的文档路径重命名
    #     petsite-rca-engine               +181 B  PR #47 的 active 过滤
    #                                              （与 gp-window-flush 同一份源）
    #
    # 命中率 4/4 不是巧合：**没有判据看着的地方，漂移不会自己停下**。
    # 本表的覆盖面本身就是判据的一部分 —— 漏一个函数等于那个函数永远绿。
    #
    # 前三个是单文件 ETL（手工 `aws lambda create-function` 建的，无栈归属，
    # 见 docs/lessons/tech-debt-etl-lambdas-outside-cfn.md），多成员对它们
    # 没有额外价值；纳入表里拿到的是**广度**。
    "neptune-etl-from-xray": (
        ("infra/lambda/etl_xray/neptune_etl_xray.py", "neptune_etl_xray.py"),
    ),
    "neptune-etl-from-appsignals": (
        ("infra/lambda/etl_appsignals/neptune_etl_appsignals.py", "neptune_etl_appsignals.py"),
    ),
    "neptune-etl-from-agentcore": (
        ("infra/lambda/etl_agentcore/neptune_etl_agentcore.py", "neptune_etl_agentcore.py"),
    ),
    # petsite-rca-engine 与 gp-window-flush 打的是**同一份 rca/ 源码**，
    # 所以同一个漂移会同时出现在两个函数上 —— PR #47 的 active 过滤实测
    # 就是如此。两个都要比，漏一个就留一半。
    "petsite-rca-engine": (
        ("rca/neptune/neptune_queries.py", "neptune/neptune_queries.py"),
        ("rca/handler.py", "handler.py"),
        ("rca/core/rca_engine.py", "core/rca_engine.py"),
        ("rca/config.py", "config.py"),
    ),
    # gp-window-flush 是第一个发现「主模块一致但函数仍在漂移」的实例
    # （2026-10-04，PR #47 合并后未部署）：
    #     window_flush_handler.py      线上 ≡ 仓库   ← Handler 指向它
    #     handler.py / config.py / core/rca_engine.py  线上 ≡ 仓库
    #     neptune/neptune_queries.py   线上 ✗（-181B）← PR #47 的 5 处 active 过滤
    # 入口模块一致掩盖了依赖模块的漂移。现在两者都比。
    "gp-window-flush": (
        ("rca/neptune/neptune_queries.py", "neptune/neptune_queries.py"),
        ("rca/window_flush_handler.py", "window_flush_handler.py"),
        ("rca/handler.py", "handler.py"),
        ("rca/core/rca_engine.py", "core/rca_engine.py"),
        ("rca/config.py", "config.py"),
    ),
}
# ── 为什么单成员会给出假绿（2026-10-04 实测，两个独立实例）──────────────────
#
# 原设计是「一个函数比一个主模块」。实测两次证明这个判据会漏：
#
#   gp-window-flush        入口 window_flush_handler.py 一致，
#                          而 neptune/neptune_queries.py 漂移 181 B
#   neptune-etl-from-aws   表里的 neptune_client.py 一致，
#                          而 neptune_client_base.py / business_layer.py
#                          合计漂移 3110 B（含一条数据正确性修复）
#
# 两次都是「被检查的那个模块恰好一致」。主模块往往是最稳定的那个（入口
# 签名很少改），而业务逻辑在依赖模块里 —— 所以单成员判据在结构上偏向漏报。
#
# `checked_b` 仍按**函数**计数（分母 len(SOURCE_MAP)），刻意不改成按模块：
# #51 把「没有新漂移就 Skip」接在这个计数上，改语义会动那条刚修好的静默
# 判据。报告里单独列出模块级覆盖面，不碰计数。
#
# 加成员的前提仍是**同形态**：只放未打包 .py（test_124 的
# test_source_map_holds_only_python 钉着这条）。json / 打包产物不要进来。

STALE_HOURS = 1.0

# ── 已确认的发现（不每天重报）────────────────────────────────────────
#
# ⚠️ 为什么需要这一节：2026-10-04 这个检测器上线 20 分钟后第一次触发，
#    报的就是我 20 分钟前刚人工核实并已升级给人决策的那一条。它会**每天
#    报同一条**，直到有人部署那个门禁。
#
#    而「一条永远不会消失的告警会训练人忽略整个频道」正是 2026-10-02
#    移除队列积压告警的理由（见 runbook 4.47）。差一点第二次踩同一个坑。
#
# **确认绑定在线上的 CodeSha256 上**，不是绑在函数名上 ——
# 一旦有人部署了这个函数，sha 就变，确认自动失效并重新报告。
# 这样「已升级待决策」不会变成噪声，而「情况变了」仍然会叫人。
ACKNOWLEDGED = {
    # 契约门禁 2026-09-22 合并进仓库，线上代码是 09-06 的 → 门禁没在跑。
    # 前置条件已全部核实（词表/共享层/导入签名/层落后但纯新增），
    # 残留未知只有三处运行时变量参数。两条处置路径的取舍已升级给人决策。
    # 详见 docs/runbooks/deployment-record.md 的 4.50。
    "neptune-etl-from-deepflow": (
        "6Y5lJpi+4eO7I/kqBEKQgVueNXk3ywItZ/inm2/6CJQ=",
        "契约门禁待部署，处置路径已升级给人决策（4.50）",
    ),
    # 守卫线上有、主干曾经没有 —— 已随 ood #8 合并进主干，源码与线上现在
    # 语义一致。只剩 LastModified 的时间差这个历史痕迹。
    # ⚠️ 它的线上代码是 esbuild 打包产物，**不能**用逐字比较，
    #    所以刻意不进 SOURCE_MAP（见本文件顶部「判据的两个坑」）。
    "ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32": (
        "9K35JaeStHGhn+iiyiNr9g9AKfJc7+f5Gv+ELihgcJE=",
        "守卫已随 ood #8 进主干，仅剩时间差痕迹（4.49）",
    ),
}
_http = urllib3.PoolManager()


def _repo_source(path: str) -> bytes | None:
    try:
        r = subprocess.run(
            ["git", "show", f"origin/main:{path}"],
            cwd=REPO, capture_output=True, timeout=30,
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _deployed_package(lam, fn: str) -> bytes | None:
    """下载一次部署包。

    拆出这个函数是为了**多成员比对时只下载一次** —— etl_aws 要比 7 个模块，
    若沿用原来「每取一个成员下载一次整包」的写法就会下 7 次（每次 1~15 MB）。
    """
    try:
        loc = lam.get_function(FunctionName=fn)["Code"]["Location"]
        return _http.request("GET", loc, timeout=60).data
    except Exception:
        return None


def _member_from_package(blob: bytes, member: str) -> bytes | None:
    """从已下载的包里取一个成员。

    ## 为什么不能用裸 endswith（2026-10-04 实测踩到）

    原写法是 `n.endswith(member)`。多成员改造后给 gp-window-flush 加了
    `handler.py`，于是它命中了 **window_flush_handler.py**（9586 B）
    而不是根目录的 handler.py（14454 B）—— 检测器报出一条不存在的漂移。

    同一个包里以 `handler.py` 结尾的路径实测有 9 个：

        handler.py                                   14454 B  ← 想要的
        window_flush_handler.py                       9586 B  ← 裸 endswith 会误中
        starlette/_exception_handler.py               2257 B
        strands/interventions/handler.py              4120 B
        ...

    所以：① 先精确匹配包内路径；② 退化时必须带 `/` 分隔符；
    ③ 候选不唯一就返回 None（算「未核实」）而**不猜** —— 猜错会产生
    假漂移，而假漂移比漏报更快训练人忽略这个检测。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if "__pycache__" not in n]
            if member in names:
                return z.read(member)
            cands = [n for n in names if n.endswith("/" + member)]
            if len(cands) == 1:
                return z.read(cands[0])
            return None
    except Exception:
        return None


def _deployed_module(lam, fn: str, member: str) -> bytes | None:
    """单成员入口 —— 保留是为了不破坏既有调用方与门禁引用。"""
    blob = _deployed_package(lam, fn)
    return _member_from_package(blob, member) if blob else None


# ── 方向 C：包完整性 ────────────────────────────────────────────────────
#
# ## 为什么需要一个新维度（2026-10-04，我自己造成的故障）
#
# 用 `cdk deploy AlertBufferStack` 部署 gp-window-flush 时，
# `Code.fromAsset('../lambda/rca_window_flush')` 打包的是那个目录的**当前
# 内容** —— 而它的依赖是 `build.sh` 用 pip install 装进去的、被 .gitignore
# 排除。在干净工作树上直接 deploy，等于部署了一个缺 urllib3 的包：
#
#     部署前   14.6 MB / 2008 条目   含 urllib3 · shared · strands · .so
#     部署后   0.29 MB /   36 条目   只剩被 git 跟踪的业务文件
#
# **当时三个判据全部给了绿灯**：
#
#   · `cdk diff` 显示 `[~] Code .S3Key` 变化 —— 看起来完全正常，
#     代码确实变了，只是少了 14 MB
#   · CloudWatch 零调用、无告警 —— 不触发就不暴露
#   · **方向 B 逐字比对 5 个模块全部「✓ 一致」** —— 业务代码确实逐字一致
#
# 最后一条是关键：方向 B 在结构上抓不到这个缺陷。它比的是**模块内容**，
# 而这里缺的是**模块本身**。一个缺依赖必挂的函数会从方向 B 拿到满分。
#
# 这个缺陷只在「全量核对线上 Lambda 列表」时才被发现，距部署 50 分钟 ——
# 期间它恰好零调用。换成有流量的时段就是一次 ModuleNotFoundError 故障。
#
# ## 为什么不复用 build.sh 的 completeness 校验
#
# `infra/lambda/rca_window_flush/build.sh` 自己有一段「Verifying package
# completeness」，校验 6 项（window_flush_handler.py / core/rca_engine.py /
# engines/factory.py / shared/__init__.py / profiles /
# neptune/neptune_client.py）。两个原因不够：
#
#   ① **它校验构建目录，不校验线上包。** 不跑 build.sh 直接 deploy ——
#      也就是我干的事 —— 这段校验根本不执行。
#   ② **它的清单里没有 urllib3。** 恰好就是我那次缺的东西。
#      它假定「跑过 pip install 所以第三方依赖必然在」，而这个假定在
#      「不跑 build.sh」的路径上不成立。
#
# 所以方向 C 校验的是**线上包**，判据有两条且都来自实测：
#
#   · 条目数下限 —— 抓大规模丢失（36 vs 2008 这种）
#   · 必需顶层项 —— 指名道姓说缺什么，比「包变小了」可处置
#
# 下限刻意留足余量（实测值的 ~75%），因为依赖版本升级会让条目数小幅波动，
# 而这个检测的目标是抓**数量级**的丢失，不是守一个精确数字。
# 一个会因为依赖升级而误报的门禁，很快就会被人忽略 —— 与 4.47 同理。
#
# 没有依赖的函数（trigger / xray / agentcore 都是单文件）**刻意不进本表**：
# 给它们写下限等于守一个恒为真的判据，只会让人以为覆盖面比实际更广。
PACKAGE_REQUIRED = {
    # 函数名: (条目数下限, (必需顶层目录...))     ← 全部由 2026-10-04 实测得出
    "neptune-etl-from-cfn":        (120,  ("requests", "urllib3", "certifi", "yaml")),
    "neptune-etl-from-deepflow":   (100,  ("requests", "urllib3", "certifi")),
    "neptune-etl-from-aws":        (110,  ("requests", "urllib3", "certifi")),
    "neptune-etl-from-appsignals": (30,   ("yaml", "shared", "profiles")),
    "gp-window-flush":             (1500, ("urllib3", "shared", "strands", "profiles", "neptune")),
    "petsite-rca-engine":          (1500, ("urllib3", "shared", "strands", "profiles", "neptune")),
}


def _package_integrity(blob: bytes, fn: str) -> str | None:
    """检查线上包是否完整。返回问题描述，没问题返回 None。

    复用方向 B 已经下载好的 blob —— 不额外拉一次包。
    """
    spec = PACKAGE_REQUIRED.get(fn)
    if spec is None:
        return None
    floor, required = spec
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [
                n for n in z.namelist()
                if "__pycache__" not in n and not n.endswith("/")
            ]
            tops = {n.split("/")[0] for n in names if "/" in n}
    except Exception:
        return None            # 取不到就不报 —— 方向 B 已经会说「未核实」
    problems = []
    missing = [r for r in required if r not in tops]
    if missing:
        problems.append(
            "**缺运行时必需项** " + ", ".join(f"`{m}`" for m in missing)
            + " —— 这个包一旦被调用就是 ModuleNotFoundError"
        )
    if len(names) < floor:
        problems.append(
            f"**包只有 {len(names)} 个条目**（下限 {floor}）"
            f" —— 疑似部署了未装依赖的目录"
        )
    return "；".join(problems) if problems else None


def check(ctx):
    out_of_band: dict[str, str] = {}
    content_diff: list[tuple[str, str]] = []   # (函数名, 描述)
    acked: list[str] = []
    checked_b = 0
    shas: dict[str, str] = {}

    for region in REGIONS:
        lam = boto3.client("lambda", region_name=region)
        cfn = boto3.client("cloudformation", region_name=region)

        stacks = {}
        for p in cfn.get_paginator("describe_stacks").paginate():
            for s in p["Stacks"]:
                stacks[s["StackName"]] = s.get("LastUpdatedTime") or s["CreationTime"]

        for p in lam.get_paginator("list_functions").paginate():
            for f in p["Functions"]:
                fn = f["FunctionName"]
                lm = dt.datetime.fromisoformat(f["LastModified"].replace("Z", "+00:00"))
                shas[fn] = f.get("CodeSha256", "")

                # 方向 A：函数比它所属的栈新 → 有人在栈外改过它
                try:
                    tags = lam.list_tags(Resource=f["FunctionArn"]).get("Tags", {})
                except Exception:
                    tags = {}
                stack = tags.get("aws:cloudformation:stack-name")
                if stack and stack in stacks:
                    gap = (lm - stacks[stack]).total_seconds() / 3600
                    if gap > STALE_HOURS:
                        out_of_band[fn] = f"栈 {stack}，函数比栈新 {gap:.0f} 小时"

                # 方向 B：线上模块与仓库源码逐字比（仅同形态）
                if region == "ap-northeast-1" and fn in SOURCE_MAP:
                    pairs = SOURCE_MAP[fn]
                    blob = _deployed_package(lam, fn)      # 整包只下一次
                    if blob is None:
                        content_diff.append((fn, "取不到线上部署包，**未核实**"))
                        continue
                    drifted: list[str] = []
                    unverified: list[str] = []
                    verified_any = False
                    # 方向 C 先跑 —— 包不完整时模块内容是否一致已经不是
                    # 重点了（一个缺 urllib3 的包，业务代码再准也挂）。
                    integrity = _package_integrity(blob, fn)
                    for path, member in pairs:
                        src = _repo_source(path)
                        live = _member_from_package(blob, member)
                        if src is None or live is None:
                            unverified.append(member)
                            continue
                        verified_any = True
                        if src != live:
                            sl, ll = src.count(b"\n"), live.count(b"\n")
                            which = "源码有、线上没有" if sl > ll else "线上有、源码没有"
                            drifted.append(
                                f"`{member}`（**{which}**，仓库 {sl} 行 / 线上 {ll} 行，"
                                f"差 {abs(sl - ll)} 行）"
                            )
                    # 只有真比对成功过才算这个函数被核实 —— 与改造前语义一致，
                    # 不让「全部成员都取不到」冒充已核实。
                    if verified_any:
                        checked_b += 1
                    if drifted or unverified or integrity:
                        parts = []
                        # 完整性问题排在最前 —— 它比内容漂移紧急：
                        # 内容漂移是「线上是旧的」，包不完整是「线上是坏的」。
                        if integrity:
                            parts.append("🔴 " + integrity)
                        if drifted:
                            parts.append(
                                f"**{len(drifted)}/{len(pairs)} 个模块漂移** —— "
                                + "；".join(drifted)
                            )
                        if unverified:
                            parts.append(
                                f"⚠️ {len(unverified)}/{len(pairs)} 个模块**未核实**："
                                + ", ".join(f"`{m}`" for m in unverified)
                            )
                        content_diff.append((fn, "  ".join(parts)))

    # 已确认的剔出去 —— 但**只在 CodeSha256 未变**时才算确认。
    # 有人部署过 → sha 变 → 确认失效 → 重新报告。
    fresh: list[str] = []
    for fn, desc in content_diff:
        ack = ACKNOWLEDGED.get(fn)
        if ack and shas.get(fn) == ack[0]:
            acked.append(f"{fn}: {ack[1]}")
        else:
            if ack:
                desc += "  ⚠️ **该函数已被重新部署**（CodeSha256 与确认时不同），确认失效"
            fresh.append(f"{fn}: {desc}")

    # 方向 A 里**已被方向 B 逐字核实过**的，不再单独列 —— B 是权威。
    # 只留下 B 没覆盖到的，并如实说它们未经内容核实。
    uncovered = {
        fn: d for fn, d in out_of_band.items()
        if fn not in SOURCE_MAP
        and not (ACKNOWLEDGED.get(fn) and shas.get(fn) == ACKNOWLEDGED[fn][0])
    }

    from kiro_crew.cron_script import Report, Skip

    if not fresh and not uncovered:
        # 刻意不发无事通知，也不重报已确认项 —— 否则这个检测器自己会变成
        # 「永远红着的告警」，而那正是 4.47 移除队列告警的理由。
        raise Skip(f"没有新漂移（已确认 {len(acked)} 项，方向 B 核实 {checked_b}/{len(SOURCE_MAP)}）")

    lines = ["Lambda 代码漂移检测 —— 有新发现", ""]
    lines.append(
        f"**方向 B 覆盖面**：{checked_b}/{len(SOURCE_MAP)} 个函数逐字核实过"
        f"（合计 {sum(len(v) for v in SOURCE_MAP.values())} 个模块）。"
    )
    lines.append(
        "不在 SOURCE_MAP 里的函数**没有被内容比对检查过** —— 别当成全查过了。"
        "在表里但成员没列全的函数同理：2026-10-04 实测两次「被检查的那个模块"
        "恰好一致、漂移在没检查的模块里」，所以模块数比函数数更能说明覆盖面。"
    )
    if acked:
        lines.append(f"已确认项 {len(acked)} 条不在下面重报（确认绑在 CodeSha256 上，重新部署即失效）。")
    lines.append("")

    if fresh:
        lines.append("### 线上与源码内容不一致（这一类才是真风险）")
        for d in fresh:
            lines.append(f"  ⚠️ {d}")
        lines += [
            "",
            "  「源码有、线上没有」= 所有人以为已修好，实际没在跑。",
            "  「线上有、源码没有」= 任何重新部署都会静默回滚修复。",
            "",
        ]

    if uncovered:
        lines.append("### 栈外改过、且**未经内容核实**的函数")
        for fn, d in list(uncovered.items())[:10]:
            lines.append(f"  · {fn[:56]} （{d}）")
        lines += [
            "",
            "  它们不在 SOURCE_MAP 里，所以只知道「被栈外改过」，不知道内容是否有差。",
            "  若栈外改动恰好是把线上对齐到仓库，重新部署不会丢东西 ——",
            "  所以这一节单独看**不是**结论，要么加进 SOURCE_MAP，要么人工核一次。",
        ]

    raise Report("\n".join(lines))

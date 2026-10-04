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
    "neptune-etl-trigger": ("infra/lambda/etl_trigger/neptune_etl_trigger.py", "neptune_etl_trigger.py"),
    "neptune-etl-from-cfn": ("infra/lambda/etl_cfn/neptune_etl_cfn.py", "neptune_etl_cfn.py"),
    "neptune-etl-from-deepflow": ("infra/lambda/etl_deepflow/neptune_etl_deepflow.py", "neptune_etl_deepflow.py"),
    "neptune-etl-from-aws": ("infra/lambda/etl_aws/neptune_client.py", "neptune_client.py"),
}

STALE_HOURS = 1.0
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


def _deployed_module(lam, fn: str, member: str) -> bytes | None:
    try:
        loc = lam.get_function(FunctionName=fn)["Code"]["Location"]
        blob = _http.request("GET", loc, timeout=60).data
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if n.endswith(member) and "__pycache__" not in n:
                    return z.read(n)
    except Exception:
        return None
    return None


def check(ctx):
    out_of_band: list[str] = []
    content_diff: list[str] = []
    checked_b = 0

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

                # 方向 A：函数比它所属的栈新 → 有人在栈外改过它
                try:
                    tags = lam.list_tags(Resource=f["FunctionArn"]).get("Tags", {})
                except Exception:
                    tags = {}
                stack = tags.get("aws:cloudformation:stack-name")
                if stack and stack in stacks:
                    gap = (lm - stacks[stack]).total_seconds() / 3600
                    if gap > STALE_HOURS:
                        out_of_band.append(
                            f"{fn[:52]} （栈 {stack}，函数比栈新 {gap:.0f} 小时）"
                        )

                # 方向 B：线上主模块与仓库源码逐字比（仅同形态）
                if region == "ap-northeast-1" and fn in SOURCE_MAP:
                    path, member = SOURCE_MAP[fn]
                    src = _repo_source(path)
                    live = _deployed_module(lam, fn, member)
                    if src is None or live is None:
                        content_diff.append(f"{fn}: 取不到源码或线上代码，**未核实**")
                        continue
                    checked_b += 1
                    if src != live:
                        sl, ll = src.count(b"\n"), live.count(b"\n")
                        which = "源码有、线上没有" if sl > ll else "线上有、源码没有"
                        content_diff.append(
                            f"{fn}: **{which}** （仓库 {sl} 行 / 线上 {ll} 行，差 {abs(sl - ll)} 行）"
                        )

    from kiro_crew.cron_script import Report, Skip

    if not content_diff and not out_of_band:
        raise Skip("没有检出漂移")

    lines = ["Lambda 代码漂移检测", ""]
    lines.append(f"**方向 B 覆盖面**：{checked_b}/{len(SOURCE_MAP)} 个函数逐字核实过。")
    lines.append("不在 SOURCE_MAP 里的函数**没有被内容比对检查过** —— 别当成全查过了。")
    lines.append("")

    if content_diff:
        lines.append("### 线上与源码内容不一致（这一类才是真风险）")
        for d in content_diff:
            lines.append(f"  ⚠️ {d}")
        lines.append("")
        lines.append("  「源码有、线上没有」= 所有人以为已修好，实际没在跑。")
        lines.append("  「线上有、源码没有」= 任何重新部署都会静默回滚修复。")
        lines.append("")

    if out_of_band:
        lines.append("### 函数比其所属栈新（栈外改过，**不一定**有风险）")
        for d in out_of_band[:10]:
            lines.append(f"  · {d}")
        lines.append("")
        lines.append("  若栈外改动恰好是把线上对齐到仓库，重新部署不会丢东西 ——")
        lines.append("  所以这一节必须用上面的内容比对来核实，不能单独当结论。")

    raise Report("\n".join(lines))

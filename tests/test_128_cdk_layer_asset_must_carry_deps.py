"""`Code.fromAsset` 打 Layer 的目录必须自带依赖，或声明怎么装。

## 守的是什么（2026-10-05 实测，一个待爆的雷）

`infra/lib/neptune-etl-stack.ts` 这样定义共享层：

    const neptuneClientLayer = new lambda.LayerVersion(this, 'NeptuneClientBaseLayer', {
      layerVersionName: 'neptune-client-base',
      code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/shared')),
      ...
    });

`cdk synth` 实测，它为这个 Layer 打出的 asset 是：

    10 个文件，顶层只有 python/
    python/ 下: __pycache__ · graph_cleanup.py · graph_confidence.py
                graph_contract.py · graph_contract_data.py · neptune_client_base.py

而**线上 Layer 21 有 90 个条目**，其中 77 个是依赖文件
（`certifi` / `idna` / `urllib3` / `requests` 及各自的 dist-info）。

`neptune_client_base.py` 的 import 行：

    import boto3, botocore, json, logging, os, requests, urllib3

`boto3` / `botocore` 由 Lambda 运行时提供，**`requests` 和 `urllib3` 不是**。

所以 `cdk deploy NeptuneEtlStack` 会把 Layer 从 90 条目替换成 5 个 .py
（外加一个 `__pycache__`，CDK 没排除它），而挂这个 Layer 的
**6 个 ETL 函数全部会在 import 阶段挂掉**。

这与 2026-10-04 在 `gp-window-flush` 上实际发生过的故障是同一族
（见 `docs/lessons/cdk-fromasset-packages-ungitted-deps.md`）：
`Code.fromAsset` 打包目录的**当前内容**，而运行时依赖不在那个目录里。

两处的区别让这一处更隐蔽：`rca_window_flush/` 至少有 `build.sh` 能把依赖
装进去——**`shared/` 连构建脚本都没有**，也就是说线上那 77 个依赖文件是
怎么进 Layer 的，仓库里没有任何记录。

## 判据

对每个被 CDK `Code.fromAsset` 打成 Layer 的目录：该目录下 Python 模块的
第三方 import 必须**在目录内可满足**，或者目录里有构建声明
（`requirements.txt` / `build.sh`）说明依赖怎么装。

判据刻意是**静态**的 —— 它要能在离线 CI 里跑，不依赖线上 Layer 的状态。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CDK_LIB = ROOT / "infra" / "lib"

#: Lambda Python 运行时自带、不需要随包下发的顶层模块。
#: 刻意只列运行时**确定**提供的那几个，不含任何「大概有」的。
_RUNTIME_PROVIDED = {"boto3", "botocore", "s3transfer", "dateutil", "jmespath"}

#: 标准库 —— python3.9 没有 sys.stdlib_module_names，所以显式列。
#: 只需覆盖本仓 shared 层实际用到的，不求完整。
_STDLIB = {
    "__future__", "abc", "argparse", "ast", "base64", "collections", "contextlib",
    "copy", "csv", "dataclasses", "datetime", "decimal", "enum", "functools",
    "gzip", "hashlib", "io", "itertools", "json", "logging", "math", "os",
    "pathlib", "random", "re", "shutil", "socket", "ssl", "string", "subprocess",
    "sys", "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unicodedata", "urllib", "uuid", "warnings", "zipfile", "zoneinfo",
}


def _layer_asset_dirs() -> list[tuple[str, Path]]:
    """从 CDK 源码里找出所有「LayerVersion + Code.fromAsset」的目录。

    ## 为什么不用一条正则跨过整个构造块

    第一版写的是

        new\\s+lambda\\.LayerVersion\\s*\\([^)]*?code:\\s*lambda\\.Code\\.fromAsset\\(

    它在 2026-10-05 当场失效：给那个构造加了说明注释之后，注释里出现了
    中文括号和 `import requests, urllib3` 这样的文本，`[^)]*?` 跨不过去，
    于是判据**一个 Layer 都找不到**，静默变成「什么都没检查」。

    抓到它的是 test_cdk_defines_at_least_one_layer_from_asset ——
    那条测试存在的全部理由就是这个：一个找不到目标的门禁比没有门禁更坏，
    因为它让人以为这一类问题有人看着。

    改成按行扫：定位 `new lambda.LayerVersion(` 所在行，然后在它之后的
    有限窗口内找 `fromAsset(path.join(__dirname, '<rel>'))`。
    注释怎么写都不影响定位。
    """
    found: list[tuple[str, Path]] = []
    asset_re = re.compile(
        r"lambda\.Code\.fromAsset\(\s*path\.join\(\s*__dirname\s*,\s*['\"]([^'\"]+)['\"]"
    )
    for ts in sorted(CDK_LIB.glob("*.ts")):
        lines = ts.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if "new lambda.LayerVersion(" not in ln.replace(" ", " "):
                continue
            # 窗口取 60 行：足够容纳一个带长注释的构造，又不会窜到下一个
            for cand in lines[i : i + 60]:
                m = asset_re.search(cand)
                if m:
                    rel = m.group(1)
                    found.append((f"{ts.name}:{rel}", (CDK_LIB / rel).resolve()))
                    break
    return found


def _third_party_imports(d: Path) -> set[str]:
    """目录下所有 .py 的第三方顶层 import（排除标准库、运行时自带、目录内本地模块）。"""
    pys = [p for p in d.rglob("*.py") if "__pycache__" not in p.parts]
    local = {p.stem for p in pys} | {
        p.name for p in d.rglob("*") if p.is_dir() and (p / "__init__.py").exists()
    }
    third: set[str] = set()
    for p in pys:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                # level > 0 是相对 import，必然本地
                mods = [] if n.level else [(n.module or "").split(".")[0]]
            else:
                continue
            for mod in mods:
                if not mod or mod in _STDLIB or mod in _RUNTIME_PROVIDED or mod in local:
                    continue
                third.add(mod)
    return third


def _satisfied_in_dir(d: Path, mod: str) -> bool:
    """这个第三方模块能在目录里找到吗（包目录或单文件）。"""
    for cand in (d, d / "python"):
        if (cand / mod).is_dir() or (cand / f"{mod}.py").is_file():
            return True
    return False


def _has_build_declaration(d: Path) -> list[str]:
    return [
        n for n in ("requirements.txt", "build.sh", "Makefile", "pyproject.toml")
        if (d / n).exists()
    ]


LAYER_DIRS = _layer_asset_dirs()


def test_cdk_defines_at_least_one_layer_from_asset():
    """判据本身要能找到目标 —— 找不到就是正则失效了，而一个什么都没检查的
    门禁比没有门禁更坏（它会让人以为这一类问题有人看着）。"""
    assert LAYER_DIRS, (
        "在 infra/lib/*.ts 里没找到任何 LayerVersion + Code.fromAsset。"
        "要么 CDK 改了写法（请更新本判据的正则），要么 Layer 不再由 CDK 管理。"
    )


@pytest.mark.parametrize("label,d", LAYER_DIRS, ids=[x[0] for x in LAYER_DIRS])
def test_layer_asset_dir_exists(label: str, d: Path):
    assert d.is_dir(), f"{label} 指向的目录不存在: {d}"


@pytest.mark.parametrize("label,d", LAYER_DIRS, ids=[x[0] for x in LAYER_DIRS])
def test_layer_deps_are_satisfied_or_declared(label: str, d: Path):
    """核心判据。

    `cdk deploy` 会把这个目录的**当前内容**发布成新 Layer 版本。
    第三方依赖既不在目录里、又没有任何构建声明，意味着那次 deploy 会
    发布一个在 import 阶段就挂的 Layer —— 而 `cdk diff` 只会显示
    `[~] Content (requires replacement)`，看起来和任何一次正常的内容更新
    毫无区别。
    """
    third = _third_party_imports(d)
    missing = sorted(m for m in third if not _satisfied_in_dir(d, m))
    if not missing:
        return
    decls = _has_build_declaration(d)
    assert decls, (
        f"{label}\n"
        f"  目录 {d.relative_to(ROOT)} 下的模块 import 了第三方包 {missing}，\n"
        f"  但目录里既没有这些包，也没有 requirements.txt / build.sh 说明怎么装。\n"
        f"  cdk deploy 会用这个目录的当前内容发布 Layer，结果是挂载它的函数\n"
        f"  在 import 阶段全部失败，而 cdk diff 只显示一行 Content 变化。\n"
        f"  同族故障已实际发生过一次，见\n"
        f"  docs/lessons/cdk-fromasset-packages-ungitted-deps.md"
    )


@pytest.mark.parametrize("label,d", LAYER_DIRS, ids=[x[0] for x in LAYER_DIRS])
def test_layer_asset_dir_has_no_pycache_committed(label: str, d: Path):
    """`__pycache__` 不该被跟踪 —— CDK 不排除它，会一起打进 Layer。"""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", str(d.relative_to(ROOT))],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    bad = [p for p in tracked if "__pycache__" in p or p.endswith(".pyc")]
    assert not bad, f"{label} 目录里有被跟踪的 __pycache__/.pyc，会被打进 Layer: {bad}"


class TestTheCommentMustNotLieAboutContents:
    """CDK 里那段注释曾经说这个 Layer「仅 python/neptune_client_base.py」，
    而实盘有 5 个业务模块 + 77 个依赖文件。

    注释与实盘脱节在这里不是风格问题：下一个读它的人会据此判断
    「这个 Layer 很简单，deploy 一下没事」。
    """

    def test_comment_does_not_claim_single_module(self):
        for ts in sorted(CDK_LIB.glob("*.ts")):
            txt = ts.read_text(encoding="utf-8")
            if "NeptuneClientBaseLayer" not in txt:
                continue
            assert "仅 python/neptune_client_base.py" not in txt, (
                f"{ts.name} 的注释仍声称这个 Layer 只有一个模块。"
                f"实盘是 5 个业务模块 + 77 个依赖文件 —— 这个说法会让下一个人"
                f"低估 cdk deploy 的后果。"
            )

    def test_comment_warns_about_deploy_consequence(self):
        hit = False
        for ts in sorted(CDK_LIB.glob("*.ts")):
            txt = ts.read_text(encoding="utf-8")
            if "NeptuneClientBaseLayer" not in txt:
                continue
            hit = True
            assert "依赖" in txt and (
                "手工" in txt or "requirements" in txt or "build" in txt
            ), (
                f"{ts.name} 没有写清这个 Layer 的依赖是怎么来的。"
                f"线上那 77 个依赖文件在仓库里没有任何记录，"
                f"不写下来下一个人只能靠猜。"
            )
        assert hit, "没找到定义 NeptuneClientBaseLayer 的文件"


# ─────────────────────────────────────────────────────────────────────────
# 构建脚本必须固定目标架构
#
# 2026-10-06 实测的静默降级：
#
#   Layer 20/21 里 charset_normalizer 的两个预编译扩展是 **aarch64**，
#   而挂这个 Layer 的 6 个 ETL **全部是 x86_64**。
#   etl_appsignals 的 PyYAML 同理 —— 本机裸 pip install 装出 aarch64 的
#   `yaml/_yaml.cpython-312-aarch64-linux-gnu.so`，而函数是 x86_64。
#
# 后果不是故障而是**静默降级**：两个库对架构不匹配的扩展都是
#
#     try:    from .cyaml import *          # 或 md.so
#     except ImportError:  <纯 Python 回退>
#
# 于是 import 成功、功能正常、只是慢。没有任何日志、指标或告警会提到它。
#
# 病因是「裸 pip install 用构建机的架构」，而构建机是 ARM、目标函数是
# x86_64，没人检查过两者是否一致。
# ─────────────────────────────────────────────────────────────────────────

#: 本仓所有为 Lambda/Layer 装依赖的构建脚本。
#: 新增一个就加进来 —— 这张表是覆盖面本身。
_BUILD_SCRIPTS = (
    "infra/lambda/shared/build.sh",
    "infra/lambda/etl_appsignals/build.sh",
    "infra/lambda/rca_window_flush/build.sh",
)


@pytest.mark.parametrize("rel", _BUILD_SCRIPTS)
def test_build_script_exists(rel: str):
    assert (ROOT / rel).is_file(), f"{rel} 不存在 —— 表里的脚本被删了？"


@pytest.mark.parametrize("rel", _BUILD_SCRIPTS)
def test_build_script_pins_target_platform(rel: str):
    """装依赖的脚本必须显式固定目标平台。

    不固定 = 用构建机的架构。本仓构建机是 aarch64，而 7 个 ETL 里
    6 个是 x86_64 —— 实测这已经让两个库的 C 加速静默失效。

    例外：目标函数本身就是 arm64 且构建机也是 arm64 时，不固定也能对 ——
    但那是**碰巧对**，构建机一换就错，而错了不会有任何信号。
    """
    txt = (ROOT / rel).read_text(encoding="utf-8")
    if "pip install" not in txt and "pip3 install" not in txt:
        pytest.skip(f"{rel} 不装依赖")
    assert "--platform" in txt, (
        f"{rel} 的 pip install 没有 --platform。\n"
        f"  不固定就是用构建机的架构（本机 aarch64），而目标函数多为 x86_64。\n"
        f"  后果是静默降级：PyYAML / charset_normalizer 对架构不匹配的扩展\n"
        f"  都有纯 Python 回退，所以 import 成功、功能正常、只是慢，\n"
        f"  没有任何信号。2026-10-06 实测两处都已发生。"
    )


@pytest.mark.parametrize("rel", _BUILD_SCRIPTS)
def test_build_script_forbids_source_fallback(rel: str):
    """带了 --platform 就必须带 --only-binary=:all:。

    没有它，pip 在找不到目标平台 wheel 时会**退回源码编译** ——
    而那会编出构建机架构的产物，静默回到原问题。
    `--platform` 单独使用时 pip 甚至会直接报错要求配合 --only-binary，
    但那只在它真的需要选 wheel 时才触发，不能当保证。
    """
    txt = (ROOT / rel).read_text(encoding="utf-8")
    if "--platform" not in txt:
        pytest.skip(f"{rel} 没用 --platform")
    assert "--only-binary" in txt, (
        f"{rel} 用了 --platform 但没有 --only-binary=:all: —— "
        f"pip 会在找不到目标 wheel 时退回源码编译，编出构建机架构的产物"
    )


@pytest.mark.parametrize("rel", _BUILD_SCRIPTS)
def test_build_script_pins_python_version(rel: str):
    """也要固定 --python-version。

    `--platform` 只定架构，不定 ABI。装 cp312 的 wheel 到 py3.11 的构建
    环境里不会报错，但 .so 的名字会是 cpython-311 —— Lambda 运行时是
    python3.12，找不到它就静默回退。

    rca_window_flush/build.sh 一开始就两个都固定了；2026-10-05 我补写
    shared/build.sh 时只写了 --platform，漏了这个。
    """
    txt = (ROOT / rel).read_text(encoding="utf-8")
    if "--platform" not in txt:
        pytest.skip(f"{rel} 没用 --platform")
    assert "--python-version" in txt, (
        f"{rel} 固定了 --platform 但没固定 --python-version —— "
        f"架构对了而 ABI 可能不对，表现同样是静默回退"
    )


@pytest.mark.parametrize("rel", _BUILD_SCRIPTS)
def test_build_script_records_why_platform_is_pinned(rel: str):
    """固定平台的理由必须写在脚本里。

    不写的话，下一个人看到 `--platform manylinux2014_x86_64` 会觉得多余
    而删掉它 —— 删了之后一切照常工作（有回退），要等到有人去量性能
    才可能发现。一个无声的缺陷最需要注释解释它为什么在这里。
    """
    txt = (ROOT / rel).read_text(encoding="utf-8")
    if "--platform" not in txt:
        pytest.skip(f"{rel} 没用 --platform")
    assert "静默降级" in txt or "回退" in txt, (
        f"{rel} 没写清为什么要固定平台。删掉 --platform 后一切照常工作，"
        f"所以必须留下「为什么」"
    )

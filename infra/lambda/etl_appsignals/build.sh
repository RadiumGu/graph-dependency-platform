#!/bin/bash
# build.sh — 把依赖与共享目录装进 etl_appsignals/，供部署使用
#
# 用法:
#   bash infra/lambda/etl_appsignals/build.sh
#
# ## 为什么这个脚本必须存在（2026-10-06 立）
#
# 这个目录只有**一个**被 git 跟踪的文件（`neptune_etl_appsignals.py`），
# 而线上部署包有 **38 个**：
#
#     (根)                      1   neptune_etl_appsignals.py
#     yaml/                    18   PyYAML
#     _yaml/                    1
#     PyYAML-6.0.2.dist-info/   7
#     profiles/                 7   ← 仓库根 profiles/
#     shared/                   4   ← 仓库根 shared/（**不是** infra/lambda/shared/）
#
# 这 37 项差额在本脚本之前**在仓库里没有任何记录** —— 它们是
# 2026-09-06 手工 `aws lambda create-function` 时手工 pip install + 手工
# 复制进去的（见 docs/lessons/tech-debt-etl-lambdas-outside-cfn.md）。
#
# 后果：任何「打包这个目录然后上传」的部署都会发布一个只有 1 个文件的包。
# 冷启动就挂 —— `import yaml` 在模块级（第 100 行）。
#
# 同一族的故障本仓已实际发生过两次：
#   · 2026-10-04 gp-window-flush：cdk deploy 打了个没装依赖的目录，
#     包从 14.6 MB / 2008 条目变成 0.29 MB / 36 条目，缺 urllib3
#   · 2026-10-05 共享 Layer：cdk synth 实测会把 90 条目的 Layer 替换成
#     5 个 .py，挂它的 6 个 ETL 全挂
# 见 docs/lessons/cdk-fromasset-packages-ungitted-deps.md
#
# 本脚本是把这个函数**纳入 CloudFormation 栈**的前置条件：纳入栈意味着
# 用 `Code.fromAsset` 打包这个目录，而那正是上面两次故障的形态。
#
# ## 为什么 shared/ 和 profiles/ 要复制而不是让它们留在原地
#
# Lambda 的 Python 搜索路径以包根为起点，`from shared.service_registry
# import ServiceRegistry`（第 127 行）要求 `shared/` 在包根下。
#
# `rca_window_flush/build.sh` 的注释记了这两项各自缺失的表现，逐字适用：
#
#     shared/    缺它 → 冷启动就挂
#     profiles/  缺它 → **冷启动能过**，运行到业务路径才炸
#                （ModuleNotFoundError: No module named 'profiles'）
#
# 后者值得单记：它证明「部署前验证 import 通过」**不足以**保证可用 ——
# 缺失只在运行时路径上暴露。所以本脚本的完整性校验覆盖两者。
#
# ## 工作树污染与清理
#
# 本脚本就地构建（将来 CDK 的 fromAsset 路径会硬编码到这个目录）。
# 装出来的文件被 .gitignore 排除，所以 `git status` 看起来干净 ——
# 这正是危险处。清理用，**注意不要写成整个目录**：
#
#     git clean -fdx infra/lambda/etl_appsignals/yaml \
#                    infra/lambda/etl_appsignals/_yaml \
#                    infra/lambda/etl_appsignals/profiles \
#                    infra/lambda/etl_appsignals/shared \
#                    'infra/lambda/etl_appsignals/PyYAML-*'
#
# ⚠️ 不要用 `git clean -fdx infra/lambda/etl_appsignals/`。2026-10-05 实测
#    过同类后果：那个范围会把本脚本自己和 requirements.txt 一起删掉
#    （首次提交前它们是未跟踪文件，而 -x 不区分 ignored 与单纯未跟踪）。
#    **清理范围也是个白名单，宽一格就误删。**

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

echo "Building etl_appsignals package content..."
echo "  目标: $SCRIPT_DIR"
echo "  仓库根: $REPO_ROOT"

[ -f "$SCRIPT_DIR/neptune_etl_appsignals.py" ] || {
  echo "  ✗ 找不到 neptune_etl_appsignals.py —— 目录不对？"; exit 1; }

# ── 共享目录 ────────────────────────────────────────────────────────────
for extra in shared profiles; do
  if [ -d "$REPO_ROOT/$extra" ]; then
    rm -rf "$SCRIPT_DIR/$extra"
    cp -r "$REPO_ROOT/$extra" "$SCRIPT_DIR/"
    echo "  Copied: $extra/"
  else
    echo "  ✗ $REPO_ROOT/$extra 不存在 —— 运行时会 ModuleNotFoundError"
    exit 1
  fi
done

# ── 第三方依赖 ──────────────────────────────────────────────────────────
#
# 用 python3.12 -m pip：Lambda 运行时是 python3.12，而构建机的 pip3
# 可能指向别的版本（本机 pip3 是 py3.9）。
PIP=python3.12
command -v $PIP >/dev/null || PIP=python3.11
command -v $PIP >/dev/null || { echo "  ✗ 找不到 python3.12 / python3.11"; exit 1; }

echo "Installing dependencies ($($PIP --version))..."

# ── 目标架构与 Python 版本必须显式固定 ──────────────────────────────────
#
# 做法沿用 `infra/lambda/rca_window_flush/build.sh` —— 那个脚本从一开始
# 就固定了 `--platform` / `--python-version` / `--only-binary`，
# 注释写得比这里更准：
#
#     · 平台定向 aarch64 + py3.12：与 Lambda 运行时一致，
#       否则 pydantic-core / _yaml 这类二进制扩展在运行时 ImportError。
#
# 这个函数的包一直没这么做，因为它是 2026-09-06 手工
# `aws lambda create-function` + 手工 pip install 的产物。
#
# 实测（2026-10-06）：本机裸 pip install 装出
# `yaml/_yaml.cpython-312-aarch64-linux-gnu.so`，而线上包里那个是
# **x86_64**（函数架构也是 x86_64）。构建机是 ARM。
#
# PyYAML 这一处的表现是**静默降级**而不是故障：
#
#     try:    from .cyaml import *
#     except ImportError:  __with_libyaml__ = False
#
# 于是 import 成功、功能正常、只是退回纯 Python 解析器。
# 没有任何日志、指标或告警会提到这件事。
# （rca 那边的 pydantic-core 没有回退，所以会直接挂 —— 同一个病，
#   严厉程度只取决于库有没有写回退。）
#
# --only-binary=:all: 是必须的：没有它 pip 会在找不到目标平台 wheel 时
# 退回源码编译，编出构建机架构的产物 —— 静默回到原问题。
TARGET_PLATFORM="${APPSIGNALS_TARGET_PLATFORM:-manylinux2014_x86_64}"
TARGET_PY="${APPSIGNALS_TARGET_PY:-3.12}"
echo "  目标: $TARGET_PLATFORM / py$TARGET_PY （构建机: $(uname -m)）"

$PIP -m pip install -q --upgrade --target "$SCRIPT_DIR" \
     --platform "$TARGET_PLATFORM" \
     --python-version "$TARGET_PY" \
     --only-binary=:all: \
     -r "$SCRIPT_DIR/requirements.txt"

# ── 清理不必要文件 ──────────────────────────────────────────────────────
find "$SCRIPT_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$SCRIPT_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$SCRIPT_DIR" -maxdepth 1 -name "bin" -type d -exec rm -rf {} + 2>/dev/null || true

# ── 完整性校验 ──────────────────────────────────────────────────────────
#
# 覆盖**模块级 import 与运行时路径两者**。只校验前者不够 ——
# profiles/ 缺失时冷启动能过，要跑到业务路径才炸。
echo "Verifying package completeness..."
_missing=""
[ -f "$SCRIPT_DIR/neptune_etl_appsignals.py" ]        || _missing="$_missing neptune_etl_appsignals.py"
[ -d "$SCRIPT_DIR/yaml" ]                             || _missing="$_missing yaml/"
[ -f "$SCRIPT_DIR/shared/service_registry.py" ]       || _missing="$_missing shared/service_registry.py"
[ -f "$SCRIPT_DIR/profiles/graph_contract.yaml" ]     || _missing="$_missing profiles/graph_contract.yaml"
[ -f "$SCRIPT_DIR/profiles/__init__.py" ]             || _missing="$_missing profiles/__init__.py"
if [ -n "$_missing" ]; then
  echo "  ✗ 包不完整，缺:$_missing"
  echo "    yaml/ 与 shared/ 缺了冷启动就挂；profiles/ 缺了要跑到业务路径才炸。"
  exit 1
fi
echo "  ✓ 必需项齐全"

N=$(find "$SCRIPT_DIR" -type f ! -name "*.pyc" | wc -l)
echo ""
echo "Done. $N 个文件，$(du -sh "$SCRIPT_DIR" | cut -f1)"
echo ""
echo "  ⚠️ 部署后清理（路径逐个列出，不要用整个目录 —— 会删掉 build.sh 自己）:"
echo "     git clean -fdx infra/lambda/etl_appsignals/{yaml,_yaml,profiles,shared} \\"
echo "                    'infra/lambda/etl_appsignals/PyYAML-*'"

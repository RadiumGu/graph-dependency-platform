#!/bin/bash
# build.sh — 把 requirements.txt 的依赖装进 python/，供 CDK Code.fromAsset 使用
#
# 用法:
#   bash infra/lambda/shared/build.sh
#
# ## 为什么这个脚本必须存在（2026-10-05 立）
#
# infra/lib/neptune-etl-stack.ts 用
#
#     code: lambda.Code.fromAsset(path.join(__dirname, '../lambda/shared'))
#
# 定义 neptune-client-base Layer。`fromAsset` 打包这个目录的**当前内容**。
#
# 2026-10-05 实测 `cdk synth`：它打出的 asset 只有 10 个文件
# （__pycache__ + 5 个业务 .py），而**线上 Layer 有 90 个条目**，其中
# 77 个是依赖文件。而 python/neptune_client_base.py 的 import 行是：
#
#     import boto3, botocore, json, logging, os, requests, urllib3
#
# boto3 / botocore 由 Lambda 运行时提供，**requests 和 urllib3 不是**。
#
# 也就是说：在没跑过本脚本的工作树上 `cdk deploy NeptuneEtlStack`，会把
# Layer 替换成一个缺 requests/urllib3 的版本，**挂载它的 6 个 ETL 函数
# 全部在 import 阶段挂掉** —— 而 cdk diff 只显示一行
# `[~] Content (requires replacement)`，与任何一次正常的内容更新无从区分。
#
# 这不是假想。同一族的故障 2026-10-04 在 gp-window-flush 上真实发生过：
# 包从 14.6 MB / 2008 条目变成 0.29 MB / 36 条目，缺 urllib3，
# 而四个判据同时报绿。详见
# docs/lessons/cdk-fromasset-packages-ungitted-deps.md
#
# 在本脚本存在之前，线上那 77 个依赖文件是怎么进 Layer 的，
# 仓库里**没有任何记录** —— 应该是某次手工 pip install + 手工 publish。
#
# ## 工作树污染与清理
#
# 本脚本就地把依赖装进 python/（CDK 的 fromAsset 路径是硬编码的，
# 没法指向别处）。装出来的文件由 .gitignore 排除，所以 `git status`
# 看起来是干净的 —— 这正是它的危险之处：
#
#   · 跑过本脚本后，目录里多出上百个未跟踪文件而 git status 显示 0 项改动
#   · `git clean -fd` **不会**删掉它们（被 ignore 的文件需要 -x）
#   · 2026-10-04 实测后果：残留依赖让 tests/test_53 的 test_m06
#     （部署包里不得有从 handler 不可达的模块）报 certifi.core /
#     charset_normalizer.api 是死代码
#
# 清理用 —— **注意路径到 python/ 为止**：
#
#     git clean -fdx infra/lambda/shared/python/
#
# ⚠️ 不要写成 `git clean -fdx infra/lambda/shared/`。2026-10-05 实测：
#    那个范围会把本脚本自己和 requirements.txt 一起删掉（它们在首次提交前
#    是未跟踪文件，而 -x 不区分 ignored 与单纯未跟踪）。
#    这与本脚本要防的缺陷同源 —— **清理范围也是个白名单，宽一格就误删**。
#
# 门禁 tests/test_128_cdk_layer_asset_must_carry_deps.py 守着
# 「fromAsset 打 Layer 的目录必须自带依赖或声明怎么装」这条判据。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_DIR="$SCRIPT_DIR/python"

echo "Building neptune-client-base layer content..."
echo "  目标: $PY_DIR"

[ -d "$PY_DIR" ] || { echo "  ✗ 找不到 $PY_DIR"; exit 1; }

# ── 装依赖 ──────────────────────────────────────────────────────────────
#
# 用 python3.12 -m pip 而非裸 pip3：Lambda 运行时是 python3.12，
# 而构建机的 pip3 可能指向别的版本（本机 pip3 是 py3.9）。
# 装到 python/ 下 —— Lambda Layer 的 Python 搜索路径是 /opt/python。
PIP=python3.12
command -v $PIP >/dev/null || PIP=python3.11
command -v $PIP >/dev/null || { echo "  ✗ 找不到 python3.12 / python3.11"; exit 1; }

echo "  pip: $($PIP --version)"
$PIP -m pip install -q --upgrade --target "$PY_DIR" \
     -r "$SCRIPT_DIR/requirements.txt"

# ── 清理不必要文件 ──────────────────────────────────────────────────────
#
# __pycache__ 必须清：CDK 的 fromAsset **不排除**它，会一起打进 Layer。
find "$PY_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$PY_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$PY_DIR" -maxdepth 1 -name "bin" -type d -exec rm -rf {} + 2>/dev/null || true

# ── 完整性校验 ──────────────────────────────────────────────────────────
#
# 校验的是**实际 import 的第三方包**，不是一份手工白名单。
# 手工白名单漏一项的后果本仓有过三次（见 infra/dr-korea 的 deploy-worker.sh），
# 而 rca_window_flush/build.sh 的那份清单恰好就漏了 urllib3 ——
# 也就是 2026-10-04 故障里缺的那个。
echo "Verifying layer completeness..."
_missing=""
for req in requests urllib3 certifi charset_normalizer idna; do
  [ -d "$PY_DIR/$req" ] || _missing="$_missing $req"
done
# 业务模块也要在 —— 它们是这个 Layer 存在的理由
for req in neptune_client_base.py graph_contract.py graph_contract_data.py \
           graph_confidence.py graph_cleanup.py; do
  [ -f "$PY_DIR/$req" ] || _missing="$_missing $req"
done
if [ -n "$_missing" ]; then
  echo "  ✗ Layer 内容不完整，缺:$_missing"
  echo "    这些是运行时必需项，缺了挂载本 Layer 的 6 个 ETL 函数会在"
  echo "    import 阶段全部失败。"
  exit 1
fi
echo "  ✓ 必需项齐全（5 个依赖包 + 5 个业务模块）"

N=$(find "$PY_DIR" -type f | wc -l)
echo ""
echo "Done. python/ 下 $N 个文件，$(du -sh "$PY_DIR" | cut -f1)"
echo ""
echo "  可运行 'cdk deploy NeptuneEtlStack' 发布。"
echo "  ⚠️ 部署后清理工作树: git clean -fdx infra/lambda/shared/python/"
echo "     （-fd 不够，依赖文件被 .gitignore 排除；路径到 python/ 为止，"
echo "       写成 shared/ 会把 build.sh 和 requirements.txt 一起删掉）"

# ── 产物与线上 Layer 21 的已知差异 ──────────────────────────────────────
#
# 2026-10-05 实测比对（Layer 21 是当时 6 个 ETL 函数实际挂载并验证过的版本）：
#
#     线上有而产物没有:  无          ← 产物是线上的超集，不会丢东西
#     产物有而线上没有:  34 个 *.dist-info/ 元数据文件
#
#     线上 Layer 21   90 文件
#     build.sh 产物  124 文件
#
# 差额全是 pip 的 dist-info。线上那次（手工 publish）把它们清掉了，
# 本脚本**刻意保留**：dist-info 是 pip 的标准产物，清掉属于人为干预，
# 而它带来的是可审计性 —— 装的到底是哪个版本，看目录名就知道。
# Lambda 的死代码检测（tests/test_53 的 test_m06）只扫函数包、不扫 Layer，
# 所以这 34 个文件不会把那道门禁弄红。
#
# 后果是下一次 cdk deploy 会发布一个新 Layer 版本（内容变了）。
# 那个版本功能上等价于 21 —— 业务模块与 5 个依赖包逐项相同。

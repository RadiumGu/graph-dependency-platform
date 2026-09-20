#!/bin/bash
# build.sh - 将 rca/ 源码打包到当前目录，供 CDK Code.fromAsset 使用
#
# 用法（在项目根或此目录下均可运行）:
#   bash infra/lambda/rca_window_flush/build.sh
#   # 或者
#   cd infra/lambda/rca_window_flush && bash build.sh
#
# 依赖: pip3, rsync

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RCA_DIR="$(cd "$SCRIPT_DIR/../../../rca" && pwd)"
# DEST_DIR 可由外部覆盖，用于**隔离构建**。
#
# ⚠️ 默认值是 SCRIPT_DIR，也就是**就地构建** —— 这个目录本质是构建产物
# （内容全部从 rca/ 复制而来），却被 git 跟踪着。于是每跑一次 build.sh
# 工作树就多出几十项改动：2026-09-20 实测污染 80 项，还删掉了一个
# `.so`（被上面那段 find -delete 清掉）。
#
# 保留就地构建作默认，是因为 CDK 的
# `lambda.Code.fromAsset(path.join(__dirname, '../lambda/rca_window_flush'))`
# 直接打包这个目录 —— 改默认值会让 cdk deploy 打出空包。
#
# 所以在干净工作树上构建时，请显式隔离：
#     DEST_DIR=$KIROCREW_SCRATCH/wf-build bash build.sh
# 然后把 CDK 的资产路径指向它，或构建完再 `git checkout -- .` 还原。
DEST_DIR="${DEST_DIR:-$SCRIPT_DIR}"

echo "=== gp-window-flush 打包 ==="
echo "源码目录: $RCA_DIR"
echo "目标目录: $DEST_DIR"

# ── 清理旧产物（保留 README.md 和 build.sh）────────────────────────────────
find "$DEST_DIR" -mindepth 1 \
  ! -name 'README.md' \
  ! -name 'build.sh' \
  -delete 2>/dev/null || true

# ── 复制 rca/ Python 源文件 ────────────────────────────────────────────────
echo "Copying source files..."

# 根目录 .py 文件
cp "$RCA_DIR"/*.py "$DEST_DIR/" 2>/dev/null || true

# 子目录（core / neptune / actions / collectors / data / search）
for dir in core neptune actions collectors data search; do
  if [ -d "$RCA_DIR/$dir" ]; then
    cp -r "$RCA_DIR/$dir" "$DEST_DIR/"
    echo "  Copied: $dir/"
  fi
done

# ── 安装第三方依赖 ──────────────────────────────────────────────────────────
echo "Installing dependencies..."
# ⚠️ 2026-09-20 修正：原先只装 `requests`，而本包里的
# `core/rca_engine.py:775` 会调 `make_layer2_engine()` ——
# **gp-window-flush 才是真正执行 RCA 分析的那个 Lambda**
# （petsite-rca-engine 只做告警缓冲，窗口到期后交给这里）。
#
# 实测线上包里 strands 条目数为 0，于是 factory ImportError →
# warning + 回退 direct。也就是说 Layer 2 探测一直跑的是 direct 实现。
#
# 这个缺陷比 rca/deploy.sh 那处更严重：那个包只缓冲、不跑分析，
# 所以给它装上 strands 之后"回退 direct 日志为 0"看起来像切换成功，
# 实际只是那条路径没被走到 —— 差点据此得出错误结论。
#
# 依赖清单与平台参数与 rca/deploy.sh 保持一致（两处必须同步，
# 因为它们装的是同一份 rca/ 源码）：
#   · 刻意不装 strands-agents-tools：它带 sympy 81M + Pillow 22M，
#     全量解压 251M 超 Lambda 250M 上限；本仓库只 import strands 本体。
#   · 不显式钉 pydantic：显式钉会与 strands 冲突
#     （ResolutionImpossible：pydantic==2.0 与 2.0.1 互斥）。
#   · 用 python3.11 -m pip 而非裸 pip3：构建机 pip3 是 py3.9，
#     而 strands-agents 要求 >=3.10，`--python-version` 只影响选 wheel、
#     不改解释器自身的版本校验。
#   · 平台定向 aarch64 + py3.12：与 Lambda 运行时一致，
#     否则 pydantic-core / _yaml 这类二进制扩展在运行时 ImportError。
python3.11 -m pip install requests pyyaml strands-agents \
    --platform "${LAMBDA_ARCH:-manylinux2014_aarch64}" \
    --python-version "${LAMBDA_PY:-3.12}" \
    --only-binary=:all: \
    -t "$DEST_DIR" -q
# boto3 / botocore 由 Lambda 运行时提供（27M+），装进包里纯属浪费体积。
rm -rf "$DEST_DIR"/boto3* "$DEST_DIR"/botocore* "$DEST_DIR"/awscli* 2>/dev/null || true

# ── 清理不必要文件 ─────────────────────────────────────────────────────────
find "$DEST_DIR" -name "*.pyc" -delete
find "$DEST_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$DEST_DIR" -name "deploy.sh" -delete 2>/dev/null || true
find "$DEST_DIR" -path "*/scripts/*" -delete 2>/dev/null || true

echo ""
echo "Done. Package size: $(du -sh "$DEST_DIR" | cut -f1)"
echo ""
echo "可运行 'cdk deploy AlertBufferStack' 部署。"

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
DEST_DIR="$SCRIPT_DIR"

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
pip3 install requests -t "$DEST_DIR" -q

# ── 清理不必要文件 ─────────────────────────────────────────────────────────
find "$DEST_DIR" -name "*.pyc" -delete
find "$DEST_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$DEST_DIR" -name "deploy.sh" -delete 2>/dev/null || true
find "$DEST_DIR" -path "*/scripts/*" -delete 2>/dev/null || true

echo ""
echo "Done. Package size: $(du -sh "$DEST_DIR" | cut -f1)"
echo ""
echo "可运行 'cdk deploy AlertBufferStack' 部署。"

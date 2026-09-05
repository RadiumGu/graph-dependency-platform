#!/usr/bin/env bash
# build_package.sh —— 打 AgentCore「直接代码部署」用的 arm64 zip。
#
# 为什么要指定平台：AgentCore Runtime 跑 Linux arm64。本机是 aarch64，
# 但仍显式传 --platform manylinux2014_aarch64 + --only-binary=:all:，
# 免得在 x86 机器上跑这个脚本时静默装出 x86 轮子（本仓踩过这个坑：
# 层里的依赖架构不对，函数起不来而错误信息完全不指向架构）。
#
# 用法：
#   bash mcp/build_package.sh [输出路径]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${TMPDIR:-/tmp}/graphdp-mcp-package.zip}"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

PY_VER="${PY_VER:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_aarch64}"

echo "==> 构建目录 $BUILD"

# ── 依赖（arm64 轮子）────────────────────────────────────────────────────────
echo "==> 安装依赖（platform=$PLATFORM, python=$PY_VER）"
python3 -m pip install \
  --quiet \
  --target "$BUILD" \
  --platform "$PLATFORM" \
  --python-version "$PY_VER" \
  --only-binary=:all: \
  --upgrade \
  -r "$ROOT/mcp/requirements.txt"

# ── 应用代码 ────────────────────────────────────────────────────────────────
echo "==> 拷入应用代码"
# 入口与 MCP 实现平铺在 zip 根（entryPoint 是 ["python","main.py"]）
cp "$ROOT/mcp/main.py"           "$BUILD/"
cp "$ROOT/mcp/agentcore_app.py"  "$BUILD/"
cp "$ROOT/mcp/server.py"         "$BUILD/"
cp "$ROOT/mcp/catalog_tools.py"  "$BUILD/"
cp "$ROOT/mcp/provenance.py"     "$BUILD/"

# 查询库与契约。刻意**不**拷 chaos/ 与 dr-plan-generator 的执行侧代码——
# 这个 server 是只读的，包里不该有能发起故障注入的东西。
mkdir -p "$BUILD/rca/neptune" "$BUILD/profiles" "$BUILD/shared" \
         "$BUILD/dr-plan-generator/graph"
cp "$ROOT/rca/__init__.py"          "$BUILD/rca/" 2>/dev/null || touch "$BUILD/rca/__init__.py"
cp -r "$ROOT/rca/neptune/."          "$BUILD/rca/neptune/"
cp -r "$ROOT/profiles/."             "$BUILD/profiles/"
cp -r "$ROOT/shared/."               "$BUILD/shared/"
# query_catalog 用 importlib 按路径加载这 5 条 DR 查询
cp -r "$ROOT/dr-plan-generator/graph/." "$BUILD/dr-plan-generator/graph/"

# ── 清理 ────────────────────────────────────────────────────────────────────
find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD" -name '*.pyc' -delete 2>/dev/null || true
find "$BUILD" -name '*.dist-info' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# 测试与文档不进包
find "$BUILD" -name 'tests' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ── 打包 ────────────────────────────────────────────────────────────────────
rm -f "$OUT"
( cd "$BUILD" && zip -qr "$OUT" . -x '*.git*' )

echo "==> 完成：$OUT ($(du -h "$OUT" | cut -f1))"
echo "==> 根目录内容："
unzip -l "$OUT" | awk 'NR>3 && $4 !~ /\// {print "    " $4}' | head -12

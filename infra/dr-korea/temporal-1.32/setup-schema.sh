#!/bin/bash
# /opt/temporal/setup-schema.sh —— auto-setup 以前替我们做的那件事，显式化。
#
# ⚠️ 顺序是承重的：create-database -> setup-schema -v 0.0 -> update-schema。
# 跳过 setup-schema 直接 update-schema 会失败（没有版本表可比对）；
# 反过来只 setup-schema 不 update-schema 会留下一个**空 schema 版本 0.0**，
# 服务端起来后报 schema 版本不兼容 —— 那个报错指向配置，实际是这一步漏了。
#
# 幂等：两个 create-database 对已存在的库返回"已存在"并继续；
# setup-schema 与 update-schema 都能安全重跑。所以这个作业可以反复执行。
set -euo pipefail

: "${SQL_PLUGIN:?}" "${SQL_HOST:?}" "${SQL_PORT:?}" "${SQL_USER:?}" "${SQL_PASSWORD:?}"

# schema 在镜像里的位置（admin-tools:1.32.0 实测：/etc/temporal/schema/postgresql/v12）
SCHEMA_ROOT=/etc/temporal/schema/postgresql/v12

sql() {
  temporal-sql-tool \
    --plugin "$SQL_PLUGIN" \
    --endpoint "$SQL_HOST" \
    --port "$SQL_PORT" \
    --user "$SQL_USER" \
    --password "$SQL_PASSWORD" \
    "$@"
}

for db in temporal temporal_visibility; do
  echo "── $db ──"
  # 已存在时不当成失败：这个作业按设计可重跑。
  sql --database "$db" create-database 2>&1 | tail -2 || echo "  （库已存在，继续）"
  sql --database "$db" setup-schema -v 0.0 2>&1 | tail -2
done

echo "── 升级 schema 到最新 ──"
sql --database temporal update-schema \
    --schema-dir "$SCHEMA_ROOT/temporal/versioned" 2>&1 | tail -3
sql --database temporal_visibility update-schema \
    --schema-dir "$SCHEMA_ROOT/visibility/versioned" 2>&1 | tail -3

echo "schema 就绪。"

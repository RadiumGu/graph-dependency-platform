#!/usr/bin/env python3
"""
check_migration_deadlines.py - Strands 迁移时间线 CI 检查（空壳）。

本 Phase 不接入 CI，仅存在占位。Phase 3 启动前由大乖乖激活，参考
experiments/strands-poc/migration-strategy.md § 5.2 的骨架。

行为：
  - 读取 docs/migration/timeline.md（YAML 文档部分）
  - 对每个 module：
    * 若 delete_date 已过且 direct_file 仍存在 → 错误退出 1
    * 若 freeze_date / delete_date 为空 → 跳过该模块（尚未规划）

当前所有模块 freeze_date / delete_date 均为空 → 正常退出 0。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml


def _load_timeline() -> dict:
    # 从脚本所在处向上找到仓库根（含 docs/migration/timeline.md）
    here = Path(__file__).resolve()
    for cand in [here.parent.parent] + list(here.parents):
        p = cand / "docs" / "migration" / "timeline.md"
        if p.exists():
            path = p
            break
    else:
        path = Path("docs/migration/timeline.md")
    if not path.exists():
        print(f"ERROR: {path} not found")
        sys.exit(2)
    # YAML 文档写在文件开头，以 "# -----" 作为分隔注释，或第一行 "---"
    # 这里尽量健壮：遇到第一个 "---\n\n## " 或以上后停止喂给 yaml.safe_load
    text = path.read_text(encoding="utf-8")
    # 截取到第一个 "\n---\n" 人类可读分隔之前
    sep = "\n---\n"
    if sep in text:
        text = text.split(sep, 1)[0]
    return yaml.safe_load(text) or {}


def main() -> int:
    tl = _load_timeline()
    today = date.today()
    errors: list[str] = []
    for m in tl.get("modules", []) or []:
        freeze = m.get("freeze_date")
        delete = m.get("delete_date")
        direct_file = m.get("direct_file", "")
        if not delete:
            # 尚未规划冻结/删除 → skip
            continue
        try:
            deadline = date.fromisoformat(str(delete))
        except Exception as e:
            errors.append(f"❌ {m.get('name')}: delete_date 解析失败 ({delete!r}): {e}")
            continue
        path = Path(direct_file)
        if path.exists() and today > deadline:
            errors.append(
                f"❌ {path} 已过删除日 {deadline}（module={m.get('name')}）；请删除或提交延期 ADR"
            )

    if errors:
        print("\n".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""合规依赖报告导出 CLI。

    export NEPTUNE_ENDPOINT=petsite-neptune.cluster-xxx.ap-northeast-1.neptune.amazonaws.com
    export REGION=ap-northeast-1
    export PYTHONPATH=infra/lambda/shared/python

    python3 -m compliance_export --dry-run                 # 只看摘要
    python3 -m compliance_export --out-dir <dir>           # 落盘
    python3 -m compliance_export --out-dir compliance_export/samples --sample

**只读**：只发 openCypher 读查询，不写图谱（由 test_54::m07 静态锁定）。

入口刻意只有这一个 —— 本仓库有过「同一份清单四处各抄一份、其中两处漂移到实际
错误」的记录，两个 CLI 入口是同类风险。
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from compliance_export import (  # noqa: E402
    Breakdown, render_markdown, take_snapshot, write_bundle,
)

#: `--sample` 用的固定文件名 —— 样例要能被 diff、被覆盖更新，不能每次换名字。
SAMPLE_BASENAME = "SAMPLE-compliance-dependency-report"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m compliance_export",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "todo", "compliance"),
                    help="输出目录（默认 todo/compliance，不入版本库）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印摘要与自检结果，不落盘")
    ap.add_argument("--sample", action="store_true",
                    help="用固定文件名写样例（供入版本库；正常导出请勿使用 —— "
                         "SYSC 15A.6.2R 要求保留每个版本，同名覆盖会让历史消失）")
    ap.add_argument("--max-hops", type=int, default=6,
                    help="多跳可达统计的最大跳数（默认 6）")
    args = ap.parse_args(argv)

    if not os.environ.get("NEPTUNE_ENDPOINT"):
        print("✗ 未设置 NEPTUNE_ENDPOINT", file=sys.stderr)
        return 2

    snap = take_snapshot(max_hops=args.max_hops)
    bd = Breakdown(snap.function_mapping)

    problems = bd.self_check()
    if problems:
        # 比例算错的报告比没有报告更糟 —— 宁可失败也不输出。
        print("✗ 分列统计自检失败：", file=sys.stderr)
        for p in problems:
            print("    %s" % p, file=sys.stderr)
        return 1

    print("快照时刻   %s" % snap.taken_at)
    print("业务能力   %d 个" % snap.capability_count)
    print("一跳依赖   %d 条" % snap.dependency_count)
    print("依赖边类型 %d 种（来自契约）" % len(snap.dependency_edge_labels))
    print()
    for _field, title, _why, buckets in bd.dimensions():
        print("%s:" % title)
        for b in buckets:
            print("    %-16s %3d 条  %s" % (b.value, b.count, b.share_pct))
    print()
    print("集中度对象 %d 个（被 >1 个业务功能共同依赖）" % len(snap.concentration))
    print("承载层     %d 类，共 %d 条（单列，非依赖）"
          % (len(snap.hosting_layer), sum(r["count"] for r in snap.hosting_layer)))

    if args.dry_run:
        print("\n--dry-run：未落盘。Markdown 长度 %d 字符"
              % len(render_markdown(snap, bd)))
        return 0

    basename = SAMPLE_BASENAME if args.sample else None
    paths = write_bundle(snap, bd, args.out_dir, basename=basename)
    print()
    for kind, path in paths.items():
        print("✓ %-9s %s" % (kind, os.path.relpath(path, _ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""合规依赖报告导出层。

对应监管要求里的 **A 类：穿透式依赖映射**
（DORA Art. 8(4) / BCBS POR 原则四 / SYSC 15A.4.1R / 关基条例第九条）。

**不做** B 类（DORA Art. 28 合同型登记册 —— 本质是合同清单不是图，结构上做不到）。

用法：

    python3 scripts/export_compliance_report.py --out-dir todo/compliance

设计判据见 `queries.py` 与 `breakdown.py` 的模块 docstring；
四条验收要求由 `tests/test_54_compliance_export.py` 静态锁定。
"""
from .breakdown import Breakdown, Bucket, DIMENSIONS, MISSING_LABEL
from .queries import HOSTING_EDGE_LABELS, Snapshot, take_snapshot
from .report import CSV_COLUMNS, render_csv, render_markdown, write_bundle

__all__ = [
    "Breakdown", "Bucket", "DIMENSIONS", "MISSING_LABEL",
    "HOSTING_EDGE_LABELS", "Snapshot", "take_snapshot",
    "CSV_COLUMNS", "render_csv", "render_markdown", "write_bundle",
]

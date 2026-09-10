"""合规依赖报告导出层。

对应监管要求里的 **A 类：穿透式依赖映射**
（DORA Art. 8(4) / BCBS POR 原则四 / SYSC 15A.4.1R / 关基条例第九条）。

**不做** B 类（DORA Art. 28 合同型登记册 —— 其主连接键是合同编号，
本平台无合同数据，结构上做不到；见 `report.py` 的 §1.3）。

用法（唯一入口，由 test_54::m10 锁定）：

    python3 -m compliance_export --dry-run
    python3 -m compliance_export --out-dir todo/compliance
    python3 -m compliance_export --out-dir compliance_export/samples --sample

文档形制的依据见 `report.py` 的模块 docstring（SYSC 15A.6.1R 的记录清单 +
ISAE 3000 §69 的要素纪律，但**刻意不声称是鉴证报告**）。
设计判据见 `queries.py` 与 `breakdown.py` 的模块 docstring；
验收要求由 `tests/test_54_compliance_export.py` 静态锁定。
"""
from .breakdown import Breakdown, Bucket, DIMENSIONS, MISSING_LABEL
from .queries import HOSTING_EDGE_LABELS, Snapshot, take_snapshot
from .report import (APPLICABLE_CRITERIA, CSV_COLUMNS, GLOSSARY,
                     LIMITATIONS_HEADING, render_csv, render_markdown,
                     write_bundle)

__all__ = [
    "Breakdown", "Bucket", "DIMENSIONS", "MISSING_LABEL",
    "HOSTING_EDGE_LABELS", "Snapshot", "take_snapshot",
    "APPLICABLE_CRITERIA", "CSV_COLUMNS", "GLOSSARY", "LIMITATIONS_HEADING",
    "render_csv", "render_markdown", "write_bundle",
]

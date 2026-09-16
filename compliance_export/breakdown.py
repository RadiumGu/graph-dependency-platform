"""三栏分列统计 —— 结构上不允许合并成单一「覆盖率」。

## 为什么要在类型层面阻止合并

监管审查最容易挑的就是「混算覆盖率」。三个维度回答的是三个不同问题：

    证据等级   verify_status      「凭什么说这条依赖成立」（SYSC 15A.5.3R 的测试证据）
    声明/观测  dependency_kind    「配置里写的，还是运行时看到的」
    第三方范围 target scope       「哪些是第三方」（DORA Art. 8(5) 的 interconnections）

把它们平均成一个数字，会让「已声明但从未观测」和「已观测但从未验证」这两种
完全不同的状态互相抵消。所以 `Breakdown` **不提供**任何返回单一比率的方法 ——
这不是文档纪律，是 API 契约。由 `tests/test_54_compliance_export.py::m03` 锁定。

## 为什么保留 None 而不折叠成 0

`verify_status` 缺失表示「从未验证」，与 `untested`（登记为待验证）语义不同；
`dependency_kind` 缺失表示「尚未打标」（如 2026-09-08 才改判的 PublishesTo）。
折叠会丢掉「我们知道自己不知道」这个信息，而那正是审计要看的诚实度。
"""
from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: 三个维度的字段名与人类可读标题。顺序即报告里的呈现顺序。
DIMENSIONS: Tuple[Tuple[str, str, str], ...] = (
    ("verify_status", "证据等级", "凭什么说这条依赖成立（SYSC 15A.5.3R 的测试证据）"),
    ("dependency_kind", "声明 vs 观测", "配置声明的，还是运行时观测到的"),
    ("target_scope", "第三方范围", "哪些是第三方（DORA Art. 8(5) 的 interconnections）"),
)

#: 缺失值在报告里的呈现。刻意不是 "0" 或空串 —— 见模块 docstring。
#:
#: 措辞对齐 ITS (EU) 2024/2956 B_06.01.0050 的枚举码 3
#: `Assessment not performed`：DORA 的法定填报模版把「未评估」当成一个**显式
#: 枚举取值**，而不是空值。原先这里写的是 `（无此字段）` —— 那是数据库实现
#: 细节泄漏进正式文档，读者无从判断「没有这一列」与「这一列没值」的区别。
#:
#: 每个维度下该取值的**精确含义不同**（verify_status 缺失 = 未做场景测试；
#: dependency_kind 缺失 = ETL 尚未打标），故报告的术语定义节逐字段说明。
MISSING_LABEL = "未评估（Assessment not performed）"


@dataclass(frozen=True)
class Bucket:
    """一个维度里的一档。"""

    value: str
    count: int
    share: float          # 占该维度总数的比例，0..1

    @property
    def share_pct(self) -> str:
        return "%.0f%%" % (self.share * 100)


class Breakdown:
    """三栏分列统计。

    **刻意没有** `overall_coverage()` / `score()` 之类的方法 —— 任何把三个维度
    压成一个数字的接口都会被 test_54::m03 拦住。需要单一指标的场合应当明确
    指定维度，例如 `dimension('verify_status')` 再自行解读。
    """

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._total = len(rows)
        self._by_dim: Dict[str, List[Bucket]] = {}
        for field_name, _title, _why in DIMENSIONS:
            counter = collections.Counter(
                (r.get(field_name) or MISSING_LABEL) for r in rows
            )
            self._by_dim[field_name] = [
                Bucket(value=v, count=n, share=(n / self._total if self._total else 0.0))
                for v, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
            ]

    @property
    def total(self) -> int:
        """依赖边总数。三个维度各自求和都应等于它 —— 这是自检点。"""
        return self._total

    def dimension(self, field_name: str) -> List[Bucket]:
        if field_name not in self._by_dim:
            raise KeyError(
                "%r 不是三栏之一（%s）。要加维度请同时更新 DIMENSIONS 与报告模板。"
                % (field_name, ", ".join(f for f, _, _ in DIMENSIONS))
            )
        return self._by_dim[field_name]

    def dimensions(self):
        """按 DIMENSIONS 顺序产出 (field, title, why, buckets)。"""
        for field_name, title, why in DIMENSIONS:
            yield field_name, title, why, self._by_dim[field_name]

    def bucket(self, field_name: str, value: str) -> Optional[Bucket]:
        for b in self.dimension(field_name):
            if b.value == value:
                return b
        return None

    def self_check(self) -> List[str]:
        """返回不一致之处；空列表表示自检通过。

        每个维度的桶计数之和必须等于总数 —— 若不等，说明有行在某个维度上被漏掉，
        那种情况下比例是错的，而一份比例错误的合规报告比没有报告更糟。
        """
        problems = []
        for field_name, _title, _why, buckets in self.dimensions():
            s = sum(b.count for b in buckets)
            if s != self._total:
                problems.append(
                    "维度 %s 的桶计数之和 %d != 总数 %d" % (field_name, s, self._total)
                )
        return problems

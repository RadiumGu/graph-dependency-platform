"""learning_common.py — LearningAgent 两个引擎共用的非 LLM 逻辑。

## 为什么是 mixin 而不是模块函数

`learning_strands.py` 原先持有一个 `DirectBedrockLearning` 实例
（`self._direct`），并把**五个方法里的四个**整个委托过去：

    analyze / iterate_hypotheses / update_graph / generate_report

也就是说 learning-agent 的迁移只做了 `generate_recommendations` 一个方法
（那个确实被真正 Strands 化了，构造 agent、发 prompt、解析输出）。
其余四个连同它们的四个私有辅助方法，一共 **304 行，全都不含任何 LLM 调用**
—— `update_graph` 的注释明写 "pure Gremlin writes"。

所以收尾不是「重新实现 agent」，是**代码搬家**。

搬成 mixin 而不是模块级函数，是因为这些方法只用到两个实例属性
（`self.ENGINE_NAME`、`self.hypothesis_engine`，后者在 `LearningBase` 里），
再加上互相调用那四个私有方法 —— 继承下来 self 引用全部照旧有效，
签名一个字不用改，改写风险最低。改成模块函数则要把 self 逐个参数化，
每处都是一次可能出错的手工改动，换不来任何好处。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .models import (  # noqa: F401
    Hypothesis, LearningReport, ServiceStats, FailurePattern,
    CoverageGap, Trend, Recommendation, GraphUpdate,
)
from runner.neptune_helpers import gremlin_query  # type: ignore

logger = logging.getLogger(__name__)


ALL_FAULT_DOMAINS = {"compute", "data", "network", "dependencies", "resources"}

FAULT_TYPE_DOMAIN = {
    "pod_kill": "compute", "pod_failure": "compute",
    "pod_cpu_stress": "resources", "pod_memory_stress": "resources",
    "network_delay": "network", "network_loss": "network",
    "network_partition": "network", "dns_chaos": "network",
    "http_chaos": "dependencies",
    "fis-aurora-failover": "data", "fis-aurora-reboot": "data",
    "fis-lambda-delay": "dependencies", "fis-lambda-error": "dependencies",
    "fis-ebs-io-latency": "resources", "fis-network-disruption": "network",
    "fis-eks-node-terminate": "compute",
}


def _ddb_str(item: dict, key: str, default: str = "") -> str:
    return item.get(key, {}).get("S", default)


def _ddb_num(item: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(item.get(key, {}).get("N", default))
    except (ValueError, TypeError):
        return default


def _extract_json(text: str) -> list | dict:
    """从 LLM 输出中提取 JSON。"""
    import re
    # 尝试 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # 尝试直接 parse
    text_stripped = text.strip()
    if text_stripped.startswith("[") or text_stripped.startswith("{"):
        return json.loads(text_stripped)
    raise ValueError(f"无法从 LLM 输出中提取 JSON: {text[:200]}")


class LearningCommonMixin:
    """四个不含 LLM 的 Learning 方法 + 它们的私有辅助。

    使用方需自己提供：
      · `self.ENGINE_NAME` —— 用于在返回值里标明引擎
      · `self.hypothesis_engine` —— `LearningBase.__init__` 会设
    """

    def analyze(self, experiment_results: list[dict]) -> dict:
        t0 = time.time()
        report = LearningReport(analysis_window_days=90)
        if not experiment_results:
            return {
                "coverage": {}, "gaps": [], "service_stats": {},
                "repeated_failures": [], "improvement_trends": [],
                "report": report,
                "engine": self.ENGINE_NAME, "latency_ms": 0, "error": None,
            }

        report.total_experiments = len(experiment_results)

        by_service: dict[str, list[dict]] = defaultdict(list)
        for item in experiment_results:
            svc = _ddb_str(item, "target_service")
            if svc:
                by_service[svc].append(item)

        total_passed = 0
        all_recovery: list[float] = []

        for svc, items in by_service.items():
            stats = ServiceStats(service=svc, total=len(items))
            recovery_times: list[float] = []
            tested_domains: set[str] = set()

            for item in items:
                status = _ddb_str(item, "status")
                if status == "PASSED":
                    stats.passed += 1
                elif status == "FAILED":
                    stats.failed += 1
                elif status == "ABORTED":
                    stats.aborted += 1

                rec = _ddb_num(item, "recovery_seconds", -1)
                if rec >= 0:
                    recovery_times.append(rec)

                ft = _ddb_str(item, "fault_type")
                domain = FAULT_TYPE_DOMAIN.get(ft)
                if domain:
                    tested_domains.add(domain)

            stats.pass_rate = round(stats.passed / stats.total * 100, 1) if stats.total else 0
            stats.avg_recovery_seconds = round(sum(recovery_times) / len(recovery_times), 1) if recovery_times else 0
            stats.tested_domains = sorted(tested_domains)
            report.service_stats[svc] = stats

            total_passed += stats.passed
            all_recovery.extend(recovery_times)

        report.pass_rate = round(total_passed / report.total_experiments * 100, 1)
        report.avg_recovery_seconds = round(sum(all_recovery) / len(all_recovery), 1) if all_recovery else 0

        report.repeated_failures = self._find_repeated_failures(by_service)
        report.coverage_gaps = self._find_coverage_gaps(report.service_stats)
        report.improvement_trends = self._calc_trends(by_service)
        report.graph_updates = self._build_graph_updates(report)

        # coverage dict: per-service tested domains
        coverage = {
            svc: {"tested_domains": s.tested_domains, "pass_rate": s.pass_rate}
            for svc, s in report.service_stats.items()
        }
        gaps = [
            {"service": g.service, "missing_domains": g.missing_domains}
            for g in report.coverage_gaps
        ]

        return {
            "coverage": coverage,
            "gaps": gaps,
            "service_stats": {svc: s.__dict__ for svc, s in report.service_stats.items()},
            "repeated_failures": [f.__dict__ for f in report.repeated_failures],
            "improvement_trends": [t.__dict__ for t in report.improvement_trends],
            "report": report,  # 内部使用，保留完整对象
            "engine": self.ENGINE_NAME,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": None,
        }

    def iterate_hypotheses(self, coverage: dict, existing_hypotheses: list) -> dict:
        t0 = time.time()
        # 从 analysis dict 提取 report 对象（如果传了 analysis 而非 coverage）
        report = coverage.get("report") if isinstance(coverage, dict) else None

        failed_services: set[str] = set()
        passed_services: set[str] = set()

        if report and hasattr(report, "repeated_failures"):
            failed_services = {fp.service for fp in report.repeated_failures}
            passed_services = {
                svc for svc, s in report.service_stats.items()
                if s.pass_rate >= 90 and s.total >= 3
            }

        # 标记已有假设状态
        for h in existing_hypotheses:
            if hasattr(h, "status") and h.status == "draft":
                svcs = set(getattr(h, "target_services", []))
                if svcs & passed_services:
                    h.status = "validated"
                elif svcs & failed_services:
                    h.status = "invalidated"

        # 为覆盖空白生成新假设
        new_hypotheses: list = []
        gaps = coverage.get("gaps", []) if isinstance(coverage, dict) else []
        error = None

        if gaps and self.hypothesis_engine:
            try:
                for gap_info in gaps[:3]:
                    svc = gap_info.get("service", "") if isinstance(gap_info, dict) else getattr(gap_info, "service", "")
                    missing = gap_info.get("missing_domains", []) if isinstance(gap_info, dict) else getattr(gap_info, "missing_domains", [])
                    generated = self.hypothesis_engine.generate(max_hypotheses=3, service_filter=svc)
                    for h in generated:
                        if hasattr(h, "failure_domain") and h.failure_domain in missing:
                            h.generated_by = "learning-agent-v2"
                            new_hypotheses.append(h)
            except Exception as e:
                logger.warning(f"新假设生成失败: {e}")
                error = str(e)

        return {
            "updated_hypotheses": existing_hypotheses + new_hypotheses,
            "new_hypotheses": new_hypotheses,
            "engine": self.ENGINE_NAME,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": error,
        }

    def update_graph(self, learning_data: dict) -> dict:
        t0 = time.time()
        report = learning_data.get("report") if isinstance(learning_data, dict) else None
        vertices_updated = 0
        edges_updated = 0
        error = None

        if report and hasattr(report, "graph_updates"):
            for upd in report.graph_updates:
                try:
                    val = f"'{upd.value}'" if isinstance(upd.value, str) else upd.value
                    g = (
                        f"g.V().has('Microservice','name','{upd.service}')"
                        f".property(single,'{upd.property_name}',{val})"
                    )
                    gremlin_query(g)
                    vertices_updated += 1
                    logger.info(f"图谱更新: {upd.service}.{upd.property_name} = {upd.value}")
                except Exception as e:
                    logger.warning(f"图谱更新失败 {upd.service}.{upd.property_name}: {e}")
                    error = str(e)

        return {
            "vertices_updated": vertices_updated,
            "edges_updated": edges_updated,
            "engine": self.ENGINE_NAME,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": error,
        }

    def generate_report(self, analysis: dict) -> dict:
        t0 = time.time()
        report = analysis.get("report") if isinstance(analysis, dict) else None
        if not report:
            return {
                "report_md": "# 无数据\n\n无实验历史。\n",
                "engine": self.ENGINE_NAME,
                "latency_ms": 0,
                "error": None,
            }

        lines = [
            f"# 混沌工程学习报告",
            f"",
            f"> 生成时间: {report.generated_at}",
            f"> 分析窗口: 最近 {report.analysis_window_days} 天",
            f"",
            f"## 总览",
            f"",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| 实验总数 | {report.total_experiments} |",
            f"| 通过率 | {report.pass_rate}% |",
            f"| 平均恢复时间 | {report.avg_recovery_seconds}s |",
            f"",
        ]

        if report.service_stats:
            lines += [
                "## 服务统计", "",
                "| 服务 | 总数 | 通过 | 失败 | 熔断 | 通过率 | 平均恢复 | 已覆盖域 |",
                "|------|------|------|------|------|--------|----------|----------|",
            ]
            for svc, s in sorted(report.service_stats.items()):
                domains = ", ".join(s.tested_domains) if s.tested_domains else "-"
                lines.append(
                    f"| {svc} | {s.total} | {s.passed} | {s.failed} | {s.aborted} "
                    f"| {s.pass_rate}% | {s.avg_recovery_seconds}s | {domains} |"
                )
            lines.append("")

        if report.repeated_failures:
            lines += ["## ⚠️ 重复失败模式", ""]
            for fp in report.repeated_failures:
                lines.append(f"- **{fp.service}** / `{fp.fault_type}`: 失败 {fp.failure_count} 次 (最近: {fp.last_failure[:10]})")
            lines.append("")

        if report.coverage_gaps:
            lines += ["## 🔍 覆盖空白", ""]
            for gap in report.coverage_gaps:
                lines.append(f"- **{gap.service}**: 未覆盖 {', '.join(gap.missing_domains)}")
            lines.append("")

        if report.improvement_trends:
            lines += ["## 📈 趋势", ""]
            icon = {"improving": "✅", "degrading": "❌", "stable": "➡️"}
            for t in report.improvement_trends:
                lines.append(f"- {icon.get(t.direction, '❓')} {t.description}")
            lines.append("")

        # recommendations 从 analysis dict 取（generate_recommendations 产出的）
        recs = analysis.get("recommendations_result", {}).get("recommendations", [])
        if recs:
            lines += ["## 💡 改进建议", ""]
            for r in recs:
                if isinstance(r, dict):
                    lines.append(f"{r.get('priority', '-')}. **[{r.get('category', '')}]** {r.get('title', '')}")
                    lines.append(f"   {r.get('description', '')}")
            lines.append("")

        content = "\n".join(lines)

        return {
            "report_md": content,
            "engine": self.ENGINE_NAME,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": None,
        }

    def _find_repeated_failures(self, by_service: dict[str, list[dict]]) -> list[FailurePattern]:
        patterns = []
        for svc, items in by_service.items():
            fault_failures: dict[str, list[str]] = defaultdict(list)
            for item in items:
                if _ddb_str(item, "status") in ("FAILED", "ABORTED"):
                    ft = _ddb_str(item, "fault_type", "unknown")
                    fault_failures[ft].append(_ddb_str(item, "start_time"))
            for ft, times in fault_failures.items():
                if len(times) >= 2:
                    patterns.append(FailurePattern(
                        service=svc, fault_type=ft, failure_count=len(times),
                        last_failure=max(times),
                        description=f"{svc} 在 {ft} 故障下连续失败 {len(times)} 次",
                    ))
        patterns.sort(key=lambda p: p.failure_count, reverse=True)
        return patterns

    def _find_coverage_gaps(self, stats: dict[str, ServiceStats]) -> list[CoverageGap]:
        gaps = []
        for svc, s in stats.items():
            missing = sorted(ALL_FAULT_DOMAINS - set(s.tested_domains))
            if missing:
                gaps.append(CoverageGap(
                    service=svc, missing_domains=missing,
                    suggestion=f"建议为 {svc} 补充 {', '.join(missing)} 域的实验",
                ))
        return gaps

    def _calc_trends(self, by_service: dict[str, list[dict]]) -> list[Trend]:
        trends = []
        for svc, items in by_service.items():
            sorted_items = sorted(items, key=lambda i: _ddb_str(i, "start_time"))
            recovery_vals = [_ddb_num(i, "recovery_seconds", -1) for i in sorted_items]
            recovery_vals = [v for v in recovery_vals if v >= 0]
            if len(recovery_vals) >= 3:
                first_half = recovery_vals[:len(recovery_vals) // 2]
                second_half = recovery_vals[len(recovery_vals) // 2:]
                avg_first = sum(first_half) / len(first_half)
                avg_second = sum(second_half) / len(second_half)
                if avg_second < avg_first * 0.8:
                    direction = "improving"
                elif avg_second > avg_first * 1.2:
                    direction = "degrading"
                else:
                    direction = "stable"
                trends.append(Trend(
                    service=svc, metric="recovery_time", direction=direction,
                    values=recovery_vals,
                    description=f"{svc} 恢复时间趋势: {direction} (前期均值 {avg_first:.0f}s → 后期 {avg_second:.0f}s)",
                ))
        return trends

    def _build_graph_updates(self, report: LearningReport) -> list[GraphUpdate]:
        updates = []
        now = datetime.now(timezone.utc).isoformat()
        for svc, stats in report.service_stats.items():
            # 属性名与量纲统一到 runner/graph_feedback.py 的写法：
            # 该文件写 resilience_score 且是 0-100 整数，活图里已有 6 个节点带它。
            # 这里原先写 chaos_resilience_score 且是 0-1 浮点（pass_rate/100），
            # 与 A 既不同名也不同量纲，导致读取方 neptune_helpers.query_learning_nodes
            # 只读到 B 的名字、而 B 从未真正落数据，每次都拿 -1 兜底。
            updates.append(GraphUpdate(svc, "resilience_score", round(stats.pass_rate)))
            updates.append(GraphUpdate(svc, "last_chaos_test", now))
            updates.append(GraphUpdate(svc, "test_coverage", ",".join(stats.tested_domains)))
        for fp in report.repeated_failures:
            updates.append(GraphUpdate(fp.service, "weakness_pattern", f"{fp.fault_type}:fail_count={fp.failure_count}"))
        return updates

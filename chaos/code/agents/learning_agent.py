"""
learning_agent.py — 实验结果闭环学习 Agent (Phase 2)

输入：DynamoDB 实验历史 + Neptune 图谱 + 现有假设库
输出：learning_report.md + hypotheses.json 更新 + Neptune 图谱更新
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

from .models import (
    Hypothesis, LearningReport, ServiceStats, FailurePattern,
    CoverageGap, Trend, Recommendation, GraphUpdate,
)
from .hypothesis_agent import (
    HypothesisAgent, _gremlin_query, _invoke_llm, _extract_json, HYPOTHESES_PATH,
)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from runner.query import ExperimentQueryClient
from runner.config import REGION

logger = logging.getLogger(__name__)

ALL_FAULT_DOMAINS = {"compute", "data", "network", "dependencies", "resources"}

# fault_type → failure_domain 映射
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


class LearningAgent:
    """实验结果学习 Agent — 分析历史、迭代假设、更新图谱、生成报告。"""

    def __init__(self):
        self._query = ExperimentQueryClient()

    # ── 1. 分析 ──────────────────────────────────────────────────────

    def analyze(
        self,
        experiments: list[dict],
        graph_context: list[dict] | None = None,
        hypotheses: list[Hypothesis] | None = None,
    ) -> LearningReport:
        """
        分析 DynamoDB 实验历史：
        1. 统计通过率/失败率 by 服务/故障域
        2. 识别重复失败模式
        3. 发现未覆盖故障域
        4. 评估恢复时间趋势
        """
        report = LearningReport(analysis_window_days=90)
        if not experiments:
            return report

        report.total_experiments = len(experiments)

        # ── 按服务聚合 ──
        by_service: dict[str, list[dict]] = defaultdict(list)
        for item in experiments:
            svc = _ddb_str(item, "target_service")
            if svc:
                by_service[svc].append(item)

        total_passed = 0
        all_recovery = []

        for svc, items in by_service.items():
            stats = ServiceStats(service=svc, total=len(items))
            recovery_times = []
            tested_domains = set()

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

        # ── 重复失败模式 ──
        report.repeated_failures = self._find_repeated_failures(by_service)

        # ── 未覆盖故障域 ──
        report.coverage_gaps = self._find_coverage_gaps(report.service_stats)

        # ── 恢复时间趋势 ──
        report.improvement_trends = self._calc_trends(by_service)

        # ── 图谱更新 ──
        report.graph_updates = self._build_graph_updates(report)

        # ── LLM 生成建议 ──
        report.recommendations = self._generate_recommendations(report)

        return report

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
            # 按时间正序取恢复时间
            sorted_items = sorted(items, key=lambda i: _ddb_str(i, "start_time"))
            recovery_vals = [_ddb_num(i, "recovery_seconds", -1) for i in sorted_items]
            recovery_vals = [v for v in recovery_vals if v >= 0]
            if len(recovery_vals) >= 3:
                first_half = recovery_vals[:len(recovery_vals)//2]
                second_half = recovery_vals[len(recovery_vals)//2:]
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
            # resilience_score: pass_rate 归一化到 0-1
            updates.append(GraphUpdate(svc, "chaos_resilience_score", round(stats.pass_rate / 100, 2)))
            updates.append(GraphUpdate(svc, "last_tested_at", now))
            updates.append(GraphUpdate(svc, "test_coverage", ",".join(stats.tested_domains)))
        # weakness_pattern
        for fp in report.repeated_failures:
            updates.append(GraphUpdate(fp.service, "weakness_pattern", f"{fp.fault_type}:fail_count={fp.failure_count}"))
        return updates

    def _generate_recommendations(self, report: LearningReport) -> list[Recommendation]:
        """用 LLM 基于分析结果生成改进建议。"""
        summary = {
            "total": report.total_experiments,
            "pass_rate": report.pass_rate,
            "avg_recovery": report.avg_recovery_seconds,
            "repeated_failures": [
                {"service": f.service, "fault": f.fault_type, "count": f.failure_count}
                for f in report.repeated_failures
            ],
            "coverage_gaps": [
                {"service": g.service, "missing": g.missing_domains}
                for g in report.coverage_gaps
            ],
            "trends": [
                {"service": t.service, "metric": t.metric, "direction": t.direction}
                for t in report.improvement_trends
            ],
        }
        prompt = f"""你是混沌工程改进顾问。基于以下实验分析结果，生成 3-5 条改进建议。

## 分析摘要
{json.dumps(summary, ensure_ascii=False, indent=2)}

## 输出格式
JSON 数组，每个元素: {{"priority": 1, "category": "coverage|resilience|process", "title": "标题", "description": "描述", "target_services": ["svc1"]}}

```json
[...]
```"""
        try:
            raw = _extract_json(_invoke_llm(prompt, max_tokens=2048))
            return [Recommendation(
                priority=r.get("priority", 5),
                category=r.get("category", "resilience"),
                title=r.get("title", ""),
                description=r.get("description", ""),
                target_services=r.get("target_services", []),
            ) for r in raw]
        except Exception as e:
            logger.warning(f"LLM 建议生成失败: {e}")
            # fallback: 基于规则生成
            recs = []
            for i, gap in enumerate(report.coverage_gaps[:3], 1):
                recs.append(Recommendation(
                    priority=i, category="coverage", title=f"补充 {gap.service} 故障域覆盖",
                    description=gap.suggestion, target_services=[gap.service],
                ))
            for fp in report.repeated_failures[:2]:
                recs.append(Recommendation(
                    priority=len(recs)+1, category="resilience",
                    title=f"修复 {fp.service} 的 {fp.fault_type} 弱点",
                    description=fp.description, target_services=[fp.service],
                ))
            return recs

    # ── 2. 迭代假设 ──────────────────────────────────────────────────

    def iterate_hypotheses(
        self,
        report: LearningReport,
        existing: list[Hypothesis],
    ) -> list[Hypothesis]:
        """
        基于分析结果迭代假设：
        - 标记已验证/失败的假设
        - 调用 HypothesisAgent 生成后续假设填补空白
        """
        # 标记已有假设状态
        failed_services = {fp.service for fp in report.repeated_failures}
        passed_services = {
            svc for svc, s in report.service_stats.items()
            if s.pass_rate >= 90 and s.total >= 3
        }

        for h in existing:
            if h.status == "draft":
                svcs = set(h.target_services)
                if svcs & passed_services:
                    h.status = "validated"
                elif svcs & failed_services:
                    h.status = "invalidated"

        # 为覆盖空白生成新假设
        new_hypotheses = []
        if report.coverage_gaps:
            try:
                import os as _os, sys as _sys
                _rca = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "rca")
                if _os.path.abspath(_rca) not in _sys.path:
                    _sys.path.insert(0, _os.path.abspath(_rca))
                from engines.factory import make_hypothesis_engine
                agent = make_hypothesis_engine()
                for gap in report.coverage_gaps[:3]:
                    generated = agent.generate(max_hypotheses=3, service_filter=gap.service)
                    # 只保留覆盖缺失域的
                    for h in generated:
                        if h.failure_domain in gap.missing_domains:
                            h.generated_by = "learning-agent-v1"
                            new_hypotheses.append(h)
            except Exception as e:
                logger.warning(f"新假设生成失败: {e}")

        report.new_hypotheses = new_hypotheses
        return existing + new_hypotheses

    # ── 3. 更新图谱 ──────────────────────────────────────────────────

    def update_graph(self, report: LearningReport):
        """将分析结果写回 Neptune 图谱。"""
        for upd in report.graph_updates:
            try:
                val = f"'{upd.value}'" if isinstance(upd.value, str) else upd.value
                gremlin = (
                    f"g.V().has('Microservice','name','{upd.service}')"
                    f".property(single,'{upd.property_name}',{val})"
                )
                _gremlin_query(gremlin)
                logger.info(f"图谱更新: {upd.service}.{upd.property_name} = {upd.value}")
            except Exception as e:
                logger.warning(f"图谱更新失败 {upd.service}.{upd.property_name}: {e}")

    # ── 4. 生成报告 ──────────────────────────────────────────────────

    def generate_report(self, report: LearningReport, output_path: str = "learning_report.md") -> str:
        """输出 Markdown 格式的学习报告。"""
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

        # 按服务统计
        if report.service_stats:
            lines += [
                "## 服务统计",
                "",
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

        # 重复失败
        if report.repeated_failures:
            lines += ["## ⚠️ 重复失败模式", ""]
            for fp in report.repeated_failures:
                lines.append(f"- **{fp.service}** / `{fp.fault_type}`: 失败 {fp.failure_count} 次 (最近: {fp.last_failure[:10]})")
            lines.append("")

        # 覆盖空白
        if report.coverage_gaps:
            lines += ["## 🔍 覆盖空白", ""]
            for gap in report.coverage_gaps:
                lines.append(f"- **{gap.service}**: 未覆盖 {', '.join(gap.missing_domains)}")
            lines.append("")

        # 趋势
        if report.improvement_trends:
            lines += ["## 📈 趋势", ""]
            icon = {"improving": "✅", "degrading": "❌", "stable": "➡️"}
            for t in report.improvement_trends:
                lines.append(f"- {icon.get(t.direction, '❓')} {t.description}")
            lines.append("")

        # 建议
        if report.recommendations:
            lines += ["## 💡 改进建议", ""]
            for r in report.recommendations:
                lines.append(f"{r.priority}. **[{r.category}]** {r.title}")
                lines.append(f"   {r.description}")
            lines.append("")

        # 新假设
        if report.new_hypotheses:
            lines += [f"## 🆕 新生成假设 ({len(report.new_hypotheses)} 个)", ""]
            for h in report.new_hypotheses:
                lines.append(f"- [{h.id}] {h.title} ({h.failure_domain}/{h.backend})")
            lines.append("")

        # 图谱更新
        if report.graph_updates:
            lines += [f"## 🔄 图谱更新 ({len(report.graph_updates)} 项)", ""]
            for u in report.graph_updates:
                lines.append(f"- `{u.service}.{u.property_name}` = `{u.value}`")
            lines.append("")

        content = "\n".join(lines)
        with open(output_path, "w") as f:
            f.write(content)
        logger.info(f"学习报告已生成: {output_path}")
        return output_path

    # ── 便捷入口 ─────────────────────────────────────────────────────

    def run(
        self,
        service: str | None = None,
        limit: int = 50,
        output: str = "learning_report.md",
        update_neptune: bool = True,
    ) -> LearningReport:
        """
        完整闭环流程：analyze → iterate_hypotheses → update_graph → generate_report
        """
        # 1. 获取实验历史
        if service:
            experiments = self._query.list_by_service(service, days=90, limit=limit)
        else:
            # --all: 查所有状态的实验
            experiments = []
            for status in ("PASSED", "FAILED", "ABORTED"):
                experiments.extend(self._query.list_by_status(status, days=90))

        if not experiments:
            logger.warning("无实验历史记录，跳过分析")
            return LearningReport()

        logger.info(f"加载 {len(experiments)} 条实验记录")

        # 2. 加载现有假设
        existing = HypothesisAgent.load()

        # 3. 分析
        report = self.analyze(experiments)

        # 4. 迭代假设
        updated = self.iterate_hypotheses(report, existing)
        HypothesisAgent().save(updated)
        logger.info(f"假设库已更新: {len(updated)} 个假设")

        # 5. 更新图谱
        if update_neptune:
            self.update_graph(report)

        # 6. 生成报告
        self.generate_report(report, output)

        return report

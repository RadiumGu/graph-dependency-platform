# RESULTS.md — 测试执行摘要

**日期**: 2026-04-01

| 阶段 | 文件 | 通过/总计 | 耗时 |
|------|------|-----------|------|
| Phase 1 (P0) | test_00_regression, test_01_security | 34/34 | 1.24s |
| Phase 2 (P2) | test_02~05 单元测试 | 31/31 | 6.39s |
| Phase 3 (P1) | test_06~09 集成测试 | 29/29 | 51.30s |
| Phase 4 (P3) | test_10 端到端 | 17/17 | 112.96s |
| **合计** | | **111/111** | **~172s** |

**结论: 全部 111 个测试用例通过**

详细报告: `/home/ubuntu/tech/blog/gp-improve/test-results-2026-04-01.md`
已知源码问题: `tests/FAILURES.md`（2 个 bug，均有建议修复方案）

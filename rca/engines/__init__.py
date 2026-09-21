"""rca.engines — NLQuery / future agent engine base + factory.

Phase 1 地基（Strands 迁移）：
  - base.NLQueryBase: 抽象接口（Wave 返回字段 + 迁移元数据）
  - factory.make_nlquery_engine: 只构造 strands（2026-09-21 起 direct 已删，
    env NLQUERY_ENGINE 不再有作用）
  - strands_common: BedrockModel 构造 + tool helper 占位
"""

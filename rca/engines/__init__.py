"""rca.engines — NLQuery / future agent engine base + factory.

Phase 1 地基（Strands 迁移）：
  - base.NLQueryBase: 抽象接口（Wave 返回字段 + 迁移元数据）
  - factory.make_nlquery_engine: env 开关 NLQUERY_ENGINE=direct|strands
  - strands_common: BedrockModel 构造 + tool helper 占位
"""

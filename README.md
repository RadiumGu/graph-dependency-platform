# Graph Dependency Platform

PetSite 可观测性与韧性工程平台 — 基于 AWS EKS 微服务的"可观测 → 知识图谱 → 智能根因 → 混沌验证"完整闭环。

## 项目结构

```
├── infra/       # 基础设施层 — CDK 全栈部署 + Neptune ETL 数据管道
├── rca/         # 智能根因分析引擎 — Graph RAG + Layer2 AWS Probers
├── chaos/       # 混沌工程平台 — AI 驱动假设生成 + 5 Phase 实验引擎
└── shared/      # 共享配置与工具
```

## 架构总览

```
EKS PetSite (ARM64 Graviton3)
        │
   infra/ ── CDK 部署 + 模块化 ETL ──► Neptune 知识图谱 (171 节点)
        │
   rca/  ── CW Alarm 触发 ──► 多层 RCA + Layer2 Probers + Graph RAG ──► Neptune
        │
   chaos/ ── AI 假设生成 ──► 故障注入 (Chaos Mesh + FIS) ──► 验证 RCA ──► 闭环学习
```

## 各子项目文档

- **infra**: [`infra/README.md`](infra/README.md) — CDK 部署指南、ETL 架构
- **rca**: [`rca/README.md`](rca/README.md) — RCA 引擎架构、Layer2 Probers 扩展指南
- **chaos**: [`chaos/code/README.md`](chaos/code/README.md) — 混沌工程平台使用说明

## 核心 AWS 资源

| 资源 | 标识 | 用途 |
|------|------|------|
| EKS | `petsite-cluster` (ARM64) | 微服务运行环境 |
| Neptune | `petsite-neptune` | 知识图谱存储 |
| Region | `ap-northeast-1` | 主部署区域 |

## 历史项目

本 monorepo 由以下三个独立项目合并而来（完整 Git 历史已保留）：

- [RadiumGu/ETL-Neptune](https://github.com/RadiumGu/ETL-Neptune) → `infra/`
- [RadiumGu/graph-rca-engine](https://github.com/RadiumGu/graph-rca-engine) → `rca/`
- [RadiumGu/graph-driven-chaos](https://github.com/RadiumGu/graph-driven-chaos) → `chaos/`

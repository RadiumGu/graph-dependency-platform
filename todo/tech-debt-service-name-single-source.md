# 技术债：把服务名解析收敛到单一真源

**立卡日期**：2026-09-06
**状态**：待排期（**不阻塞任何功能，只造成重复**）
**触发**：接入 `etl_appsignals` 时发现（见 `todo/adot-migration-goal/GOAL.md` cycle-2）

---

## 现状：同一件事有四份实现

「服务叫什么名」这个判据，仓库里有四处各自为政的实现：

| 位置 | 做法 | 用的是真源吗 |
|---|---|---|
| `infra/lambda/rca_window_flush/config.py` | `CANONICAL` / `NEPTUNE_TO_DEPLOYMENT`，由 `EnvironmentProfile` + `ServiceRegistry` 派生 | ✅ 是 |
| `infra/lambda/etl_appsignals/` | 直接读 `profiles/petsite.yaml` 的 `services` 段喂 `ServiceRegistry` | ✅ 是 |
| `infra/lambda/etl_xray/neptune_etl_xray.py` | 自己的 `_strip_k8s_fqdn()` + 本地映射表（第 133 行一份、第 522 行一个函数） | ❌ 否 |
| `infra/lambda/etl_aws/handler.py` | 走 K8s `app_label` | ❌ 否 |
| `infra/lambda/rca_window_flush/actions/action_executor.py` | `SVC_TO_DEPLOYMENT` | 间接 |

真源是 `profiles/petsite.yaml` 的
`services.<svc>.{k8s_deployment, k8s_label, neptune_name, aliases}`，
经 `profiles/profile_loader.py` + `shared/service_registry.py` 消费。

**实测：`ServiceRegistry` / `EnvironmentProfile` / `CANONICAL` 在 5 个 ETL 里的
引用数是 0**（`etl_appsignals` 是第一个用它的 ETL）。

## 后果

同一个物理服务在不同 ETL 写出的节点**可能对不上**，而且**没有任何断言会失败** ——
节点和边各自都在、类型也都合法，只是指向了两个不同的名字。

本仓库已经因「判据两份分歧实现」吃过一次亏：
`verify_confidence` 的 ±4.0 与 0.0 两套权重同源于此（见
`todo/goal-loop-chaos-verify/tasks.md` Stage 9）。这次分歧的是服务名，形状一样。

已实际暴露过的映射缺口（都靠人工发现，没有任何自动检查抓到）：
- `petstatusupdater` 缺 alias `petadoptionstatusupdater`（Application Signals 的报法）
  → 2026-09-05 补
- `_DELEGATION_TOOLS` 缺 `concierge_chat` / `food_ordering`（Orchestrator 真实 tool 名）
  → 2026-09-06 补，补完 `Delegates` 边从 2 条涨到 3 条

## 正解

把 `profiles/` + `shared/service_registry.py` **提升进 Lambda layer**
（`infra/lambda/shared/python/`），让 5 个 ETL 逐步收敛到一份。
`etl_appsignals` 现在的做法（打进函数包）是权宜，沿用了 `rca_window_flush` 的先例。

顺带一并处理：
- 契约给 `AWSServiceEndpoint` 加 `appsignals_aliases` 属性，
  让 Application Signals 特有的服务报法也落到图上、与 `xray_aliases` 并列，
  而不是藏在 `etl_appsignals` 的 `_APPSIGNALS_AWS_ALIASES` 里
- 加一个门禁测试：**任何 ETL 直接内联服务名映射即失败**，
  强制走真源（否则这笔债会再长出来）

## 为什么当时没做

Lambda layer `neptune-client-base` **正被另一会话并发发布** ——
2026-09-05 单日发到 v15，其中 v12 是本会话发的、当天就被对方的 v13 顶掉。
在飞行中重构共享 layer，会把「重构对不对」与「谁的 layer 生效」两件事的
失败原因缠在一起。

**排期前提：layer 发布稳定下来（单一负责人或有协调机制）。**

## 验收

1. 5 个 ETL 中至少 4 个通过 `ServiceRegistry` 解析服务名（不含自造映射表）
2. 门禁测试存在且能抓住"新增内联映射"
3. 全量测试不回归（基线 `python3.11 -m pytest tests/` → 656 passed / 0 failed）

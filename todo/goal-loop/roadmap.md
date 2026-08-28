# Roadmap — 分阶段路线

> 每轮由代理重读。阶段内可并行,阶段间尽量按序(后一阶段常依赖前一阶段的产出)。
> 详细证据见 `../design-goals-assessment_20260828-1705.md`。

---

## 阶段 0 — 让图谱说真话(P0)🔴

**为何最先做**:这是唯一会让图谱**输出错误结论**的缺陷,而且已经在输出了。
实测:18 条 `Calls` 边里 17 条陈旧 2–5 个月却全部 `active=True`;
`gateway-service → auth-service` 对应的 `awesomeshop` 命名空间 6 个 Deployment
副本数全为 0(服务已下线),图谱仍声称该依赖活跃且带 376 次调用量。
后果:RCA 正在把缩容到零的服务当作 petsite 的上游做根因推理。

在此阶段完成前,上层所有分析(RCA / DR / SPOF)的可信度都存疑,
因此不要先做阶段 2/3 的"开放与提速"。

- P0-1 依赖边失效对账 + 补 `first_seen`
- P0-2 修 `resilience_score` 三向属性名分裂 + 补 `chaos_last_verified`

**出口条件**:DoD-1、DoD-2 全绿。

---

## 阶段 1 — 让依赖可分辨(P1)

**依赖阶段 0**:先保证边是真的,再给边分类,否则是在给假数据打标签。

- P1-1 依赖边补 `source` 与 `dependency_kind ∈ {static, dynamic}`;
  在 `profiles/petsite.yaml` schema 中约定该属性;
  `neptune_queries.py` 的 `q1`/`q3` 支持按它过滤
- P1-2 图谱 MCP server 端点(把 Q1–Q18 + NL 引擎包成 MCP tools)

注:P1-2 不严格依赖 P0,但**建议在 P0 之后**——先开放一个会说假话的图谱,
会把错误结论扩散到所有接入的 agent。

**出口条件**:DoD-3、DoD-4 全绿。

---

## 阶段 2 — 收敛与启用(P2)

- P2-1 收敛访问层:3 套 openCypher 客户端 + 割裂的 Q1–Q18/Q12–Q16 合成 graph SDK;
  `chaos/code/runner/neptune_client.py` 改用 Session 复用;
  删 `SERVICE_FUNCTION_MAP` ×3 与 `SVC_TO_CW`;修 `rca_window_flush/config.py` 漂移
- P2-2 `causal_weight` 接入 `step4_score()` + 加时间衰减
- P2-3 schema ↔ 活图一致性校验测试

**出口条件**:DoD-5 全绿。

---

## 阶段 3 — 补齐历史维度(P3)

- P3-1 拓扑历史快照机制("上周拓扑长什么样"目前完全无法回答)

这是四个目标里唯一需要**新增机制**而非修补的一项。可选:定期快照导出,
或给写入加有效期区间(bi-temporal)。

---

## 阶段 X — 遗留欠项(可穿插,不阻塞主线)

这些是本次会话中发现但尚未处理的:

- X-1 `window_flush_handler` 调用不存在的 `generate_group_report`
  ——"按 EventGroup 聚合出报告"这条路径从未实现,靠 fallback 降级掩盖了
- X-2 `K8S_NAMESPACE` 默认 `default`,而服务实际在 `petadoptions`
  (另有 `awesomeshop` 第二个应用),`profiles/petsite.yaml` 里有该值但代码未读
- X-3 `q1` 遍历 `:Serves` 边,而 `etl_aws/handler.py:1217` 每轮主动删除它
  ——实测活图 `Serves` 边 0 条,是死查询
- X-4 `etl_cfn` SQS label 不一致:`:54` 映射 `'Queue'`,`:389` 写入 `'SQSQueue'`
- X-5 未提交的工作树改动需进特性分支并开 PR
- X-6 `petsite.yaml` schema header 写 28 种边,正文枚举 26,活图实测 26 —— header 错
- X-7 schema 正文未声明 `AffectedService`(28 条)与 `Involves`(8 条)两种活图存在的边
  ——导致它们对自然语言查询隐形

---

## 明确不做(除用户另行指示)

- 不迁移剩余 6 个 x86_64 Lambda 到 arm64(需单独评估,不在本目标范围)
- 不动 PetSite 应用自身代码(本目标只涉及依赖图谱平台)
- 不改生产安全组、不收窄 EKS API 公网 CIDR(属独立的安全加固议题)
- 不重建 Bedrock KB(已于 2026-08-28 删除,理由见
  `../bedrock-kb-pgvector-removal_20260828-1635.md`)

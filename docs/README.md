# 文档导航

这个项目的文档此前散落在 `todo/` 下十个子目录里，混放了「有长期价值的设计说明与
实测教训」和「已完成工作的过程台账」。2026-09-21 做了一次归类：**前者移入 `docs/`，
后者留在 `todo/`**。

每个移入 `docs/` 的文件头部都有一行溯源抬头，写明它的原路径与成文时间；
内容已被后续改动推翻的，抬头里还有一条过期警示。

---

## 先读这五份（按顺序）

如果你是第一次接触这个项目，按这个顺序读，大约两小时能建立完整认知：

| # | 文件 | 为什么先读它 |
|---|---|---|
| 1 | [`docs/design/project-positioning.md`](design/project-positioning.md) | 先建立框：市面产品回答「我看到了什么」，本项目回答「**我看到的是真的吗**」。文档还明列了不做什么 |
| 2 | [`docs/design/design-goals-assessment.md`](design/design-goals-assessment.md) | 四大设计目标逐项打分，每条断言带证据。知道现状离目标还有多远 |
| 3 | [`docs/design/architecture-graph-io-map.md`](design/architecture-graph-io-map.md) | 系统怎么接线 —— 四大模块与 Neptune 的读写关系，Neptune 是唯一共享状态 |
| 4 | [`docs/lessons/graph-correctness-audit-and-gaps.md`](lessons/graph-correctness-audit-and-gaps.md) | 图上哪些是真的、哪些是缺口，所有断言带 `文件:行号` |
| 5 | [`docs/lessons/injection-found-defects.md`](lessons/injection-found-defects.md) | 项目立身之本：只有真实故障注入才能暴露的缺陷合集（1867 行，读前 3 例即可） |

---

## 目录结构

```
docs/
├── README.md          ← 你在这里
├── design/     (11)   核心设计说明：为什么这样设计，新人必读
├── lessons/    (26)   实测教训与踩坑记录：防止重犯，长期价值最高
├── runbooks/    (3)   可照着执行的作业手册
├── migration/   (3)   Strands 迁移的时间线与 ADR（历史决策记录）
└── (根目录)     (3)   dependency-definition / prd / tdd
```

`todo/` 保留下来的是**过程记录与运行数据**：目标循环台账（`goal-loop*/`、
`agentobv/`、`adot-migration-goal/`）、部署与变更报告、以及约 67 个脚本运行输出
JSON。新人不必读，但它们**不能随意删**——`scripts/emit_resilience_scorecard.py`
会读 `todo/*.json`，`scripts/prune_deleted_workloads.py` 会往 `todo/` 写。

---

## design/ — 核心设计说明

| 文件 | 讲什么 |
|---|---|
| [`project-positioning.md`](design/project-positioning.md) | 项目定位与边界：答「看到的是不是真的」，并明列不做什么 |
| [`design-goals-assessment.md`](design/design-goals-assessment.md) | 四大目标（唯一真源／动静态／退化变化／agent 可调）逐项打分 |
| [`architecture-graph-io-map.md`](design/architecture-graph-io-map.md) | 四大模块与 Neptune 的读写关系图（据真实代码绘制） |
| [`etl-pipeline-walkthrough.md`](design/etl-pipeline-walkthrough.md) | ETL 从触发到写图的完整机制：采集／点边定义／契约门禁／upsert 两种写法／失活。以 `etl_agentcore`（API+span）与 `etl_deepflow`（eBPF）对照 |
| [`project-intro-outline.md`](design/project-intro-outline.md) | 对外讲稿提纲：四范式 → 缺口 → 故障注入证伪。权威叙事 |
| [`dependency-graph-improvement-roadmap.md`](design/dependency-graph-improvement-roadmap.md) | 综合三份调研得出的改进路线与答辩要点 |
| [`agent-layer-taxonomy-design.md`](design/agent-layer-taxonomy-design.md) | Agent 层分类学：Gateway／Runtime／Tool 建模与 `RoutesTo` 拆分的推理 |
| [`01-agent系统接入方案.md`](design/01-agent系统接入方案.md) | PetSite 接入 Agent 系统的取舍（结论：移植上游而非从零设计） |
| [`02-agent可观测性方案.md`](design/02-agent可观测性方案.md) | Agent 可观测性接入，含 CloudWatch Transaction Search 的账号级硬门槛 |
| [`合规依赖报告-能力评估与补齐路线.md`](design/合规依赖报告-能力评估与补齐路线.md) | 合规报告能力评估与补齐路线（DORA／BCBS 映射） |
| [`P0-P1_三项可落地实施说明.md`](design/P0-P1_三项可落地实施说明.md) | 合规导出层／Agent 层证伪／impact tolerance 三项实施规格 |

## lessons/ — 实测教训与踩坑

这一类的共同特征是：**结论都由实测得出，且记录了被否决的假设**。
读它们能避免重走弯路，也是理解「为什么某处代码写得反直觉」的钥匙。

**图谱正确性**
- [`graph-correctness-audit-and-gaps.md`](lessons/graph-correctness-audit-and-gaps.md) — 依赖正确性实测与缺口
- [`graph-schema-review.md`](lessons/graph-schema-review.md) — schema 合理性核查（867 节点／1341 边／31 类型）
- [`LESSON-dependency-attribution.md`](lessons/LESSON-dependency-attribution.md) — 依赖归属判反：**挂错探针制造「反向证据」比没探针更糟**
- [`injection-found-defects.md`](lessons/injection-found-defects.md) — 只有真注入才暴露的缺陷合集

**可观测性与数据质量**
- [`four-pillars-assessment.md`](lessons/four-pillars-assessment.md) — 四支柱实测现状（回答「tracing 到底有没有」）
- [`containerinsights-iam-zero-writes.md`](lessons/containerinsights-iam-zero-writes.md) — IAM 缺失致零写入，约 3.5 个月观测数据全丢
- [`deepflow-noise-reduction.md`](lessons/deepflow-noise-reduction.md) — 自噪声从 73.2% 压到 4.6%，总采集量 −80%
- [`cloudwatch-cost-findings.md`](lessons/cloudwatch-cost-findings.md) — 日志量与成本发现（跳变并非 addon 升级引起）
- [`trafficgenerator-config-rootcause.md`](lessons/trafficgenerator-config-rootcause.md) — 流量生成器静默失效 95 天的根因

**架构与运维**
- [`production-drift-audit.md`](lessons/production-drift-audit.md) — 「我测的代码不是在跑的代码」
- [`etl-complexity-and-maintenance.md`](lessons/etl-complexity-and-maintenance.md) — 用 AST 实测 ETL 复杂度，先剔除误导数字
- [`rca-pipeline-diagnosis.md`](lessons/rca-pipeline-diagnosis.md) — RCA 管道接线诊断：直接 invoke 验不到完整链路
- [`sqs-orphan-and-topology-hints.md`](lessons/sqs-orphan-and-topology-hints.md) — 恢复流量暴露的 SQS 孤儿队列
- [`search-service-topology-capacity.md`](lessons/search-service-topology-capacity.md) — 前提不成立：真正约束是集群 CPU 容量
- [`keepalive-listadoptions-search.md`](lessons/keepalive-listadoptions-search.md) — 根因是并发扇出，不是 keep-alive 未开
- [`petsite-traffic-coverage.md`](lessons/petsite-traffic-coverage.md) — 跨 VPC 流量是否覆盖集群内应用
- [`tech-debt-etl-lambdas-outside-cfn.md`](lessons/tech-debt-etl-lambdas-outside-cfn.md) — 三个生产 ETL Lambda 不在任何 CFN 栈
- [`tech-debt-service-name-single-source.md`](lessons/tech-debt-service-name-single-source.md) — 服务名解析应收敛到单一真源

**选型与业界调研**
- [`research-lab-graphdb-necessity-FINDINGS.md`](lessons/research-lab-graphdb-necessity-FINDINGS.md) — 依赖管理是否必须用图 DB（按场景分档、带证据等级）
- [`industry-research-graph-dependency-value.md`](lessons/industry-research-graph-dependency-value.md) — 图 DB 做依赖管理的企业场景与价值
- [`industry-dependency-management-practices.md`](lessons/industry-dependency-management-practices.md) — 业界四种范式的机制层调研
- [`neptune-selection-assessment.md`](lessons/neptune-selection-assessment.md) — Neptune 选型评估
- [`research-agent-dependency-graph.md`](lessons/research-agent-dependency-graph.md) — 扩展到 GenAI Agent 依赖管理的可行性

**Agent 与前端**
- [`05-etl_xray影响面量化.md`](lessons/05-etl_xray影响面量化.md) — 开启 Transaction Search 对 etl_xray 的影响面
- [`03-可复用资产与证据清单.md`](lessons/03-可复用资产与证据清单.md) — 方案的证据底座，并记录「查不到什么」的边界
- [`05-图谱展示方案调研.md`](lessons/05-图谱展示方案调研.md) — 图谱可视化前端方案调研

## runbooks/ — 作业手册

| 文件 | 用途 |
|---|---|
| [`coverage-push-runbook.md`](runbooks/coverage-push-runbook.md) | 依赖证据覆盖率推进手册。**含一条重要结论：IAM deny 手段的上限是 14/47，剩余边是「可观测性缺口」而非「验证做得不够」** |
| [`crossvpc-loadgen-internal-alb.md`](runbooks/crossvpc-loadgen-internal-alb.md) | 跨 VPC 内网压测入口搭建 |
| [`06-展示界面实验方式说明.md`](runbooks/06-展示界面实验方式说明.md) | 展示界面两种运行模式的操作说明 |

## migration/ — Strands 迁移记录

`timeline.md` 与 `decisions/` 下的 ADR 是**历史决策记录**。它们指向
`*_direct.py` 这类已删除的文件是正常的 —— 那正是它们记录的内容。
`scripts/check_migration_deadlines.py` 会读 `timeline.md` 做 CI 门禁。

---

## 关于「direct 引擎」的统一说明

很多旧文档会提到 `direct` 与 `strands` 两种引擎实现并存。**这是迁移期的设定，
已经结束**：2026-09-20 起七个模块（smart-query／hypothesis／learning／
policy-guard／chaos-runner／dr-executor／rca-layer2）的 direct 实现全部删除，
`factory` 也去掉了回退分支 —— 现在只有 strands 一种，设 `XXX_ENGINE=direct`
不会报错但也不生效。

凡是把双轨当现状描述的文档，抬头里都加了警示。

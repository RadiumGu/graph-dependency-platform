# Neptune 选型评估：对照业界调研结论

生成时间：2026-09-05 06:50 UTC
调研来源：Research Lab 活动 c95a89e0（`~/.kiro/crew/workspace/research/c95a89e0/FINDINGS.md`，263 行）
被评估对象：`petsite-neptune`（Neptune 1.4.6.3，单实例 `petsite-neptune-instance-1` / db.r6g.large，ap-northeast-1）

---

## 一、结论

**按调研给出的六档判定表，本项目当前的查询画像落在第 ② / ③ 档，不落第 ⑤ 档（真 graph-hard）。也就是说：以「依赖分析需要多跳遍历」为理由论证 Neptune 是硬需求，这条论证站不住。**

但结论不等于「应该迁走」。评估把两件事分开：

- **选型是否被「查询硬需求」证成** → 否，实测不支持。
- **是否应该现在迁移** → 否。真正证成保留的是另外三条理由（异构 schema、agent 可调用、openCypher 方言风险最低），而迁移代价远大于这个规模下的成本收益。

**最重要的一条**：调研里最扎实的一击不在存储选型上——本项目已知的全部依赖质量缺陷，没有一个是换存储引擎能解决的。它们全落在数据契约层，而那一层与 Neptune 正交。

---

## 二、实测证据（2026-09-05 06:50 现场查活图谱与代码，非推断）

### 规模与密度

| 指标 | 实测值 | 调研里的判据 |
|---|---|---|
| 节点总数 | **1,278** | 独立基准用的是 541K / 1.63M 节点量级 |
| 边总数 | **2,472** | 同上，5.19M / 30.6M 边 |
| 参与依赖边的节点数 | **35** | — |
| 依赖边最大出度 | **14** | 「拐点取决于密度而非跳数」；稠密图 3 跳即入秒级 |
| 依赖边平均出度 | **2.94** | 稀疏图（平均度 5）6 跳去重 CTE 仅 0.16ms |
| 节点类型 / 边类型 | **39 / 29** | Backstage 等 ② 档系统的实体类型数远少于此 |

比对结论：本图谱比任何一份公开基准小 2–3 个数量级，且出度 14 / 平均 2.94 属于典型稀疏图。按 AlexPasheva 与 markaicode 两份独立基准的口径，这个规模下递归 CTE 是毫秒级，第 ⑤ 档的三个触发条件（稠密 fan-out + 无界深度 + 路径/算法）一个都不成立。

### 查询画像

**全部遍历都是有界的，无一处无界 `*`：**

| 位置 | 模式 | 用途 |
|---|---|---|
| `rca/neptune/neptune_queries.py:24` | `[:Calls\|DependsOn*1..5]` | Q1 爆炸半径 |
| `rca/neptune/neptune_queries.py:34,37-38` | `[:DependsOn*1..3]` | 业务能力反查 |
| `rca/core/rca_engine.py:301` | `[:Calls*1..5]` | 上游候选 |
| `dr-plan-generator/graph/queries.py:91` | `[...*0..8]` | 依赖树 |
| `dr-plan-generator/graph/queries.py:155` | `[:Calls\|DependsOn*1..10]` | 关键路径取数 |

`rca/neptune/schema_prompt.py:54` 甚至把「多跳遍历限制 *1..5，避免路径爆炸」写成了给 LLM 的硬约束——项目自己就没打算做无界遍历。

**零处图算法下推：** 全仓 `rca/` `dr-plan-generator/` `chaos/` 无 `shortestPath` / `allShortestPaths` / `pageRank` / `gds.*`。

**图算法全在 Python 侧：** Kahn 拓扑排序、环检测、最长路 DP 都在 `dr-plan-generator/graph/graph_analyzer.py`（`topological_sort_within_layer:115`、`Cycle detected:170`、`Longest path via DP on a DAG:266`、`Kahn's algorithm:352`）。Neptune 在这条链路里只承担「把完整结果集取出来」，算法本身不经过图引擎——这正是调研第 ⑤ 档「确需图引擎/算法库」不成立的直接证据：需要图算法的那部分，项目从一开始就没交给 Neptune。

**最深查询的真实代价：** `*1..10` 那条实跑结果是 **234 条路径，真实最大深度 7**。不是「理论上可能爆炸」，是实测就这么点。

---

## 三、逐档定位

| 档 | 判定 | 本项目 |
|---|---|---|
| ① derive-on-read | 不需图库 | **部分命中**：DeepFlow/X-Ray 派生的 Calls 边本质是这一档，但项目刻意物化并加了 `first_seen`/`last_seen`/`expires` —— 这是对 ① 档「无法回答历史时点拓扑」盲区的正确回应，也是不能停在 ① 档的理由 |
| ② 规范化关系表 | 关系库即可 | **主要命中**：有界 join + 目录 + 血缘，规模与密度都在关系型舒适区 |
| ③ 图可选/可插拔 | 图库可选 | **命中**：22 条确定性 Cypher 是有界模式匹配，SQL/PGQ 或 PuppyGraph 在原理上都能承载 |
| ④ 图原生模型、引擎不可知 | 需图模型，引擎可选 | **部分命中**：爆炸半径本质是可达性查询，但深度有界、图稀疏，未触及 BloodHound 那种无界最短路 |
| ⑤ 真 graph-hard | 确需图引擎 | **不命中**：无加权最短/关键路径下推、无图算法下推、图稀疏且深度有界 |
| ⑥ 真图存储物化 | 少数选真图存储 | **不命中**：Dynatrace Grail 那档需要的是可变拓扑 + 图遍历 + 独立保留期 + 历史时点四件齐备，本项目有物化和 last_seen，但没有独立保留期语义 |

---

## 四、反过来说：真正证成保留 Neptune 的三条理由

调研没有否定图数据库，它否定的是「用多跳遍历论证图数据库」。本项目有三条与遍历无关的理由，这些才是选型的正当依据：

1. **39 个节点类型 / 29 个边类型的异构度。** 关系型建模到这个异构度只有两条路：39 张表加大量 join，或 EAV 退化成弱类型。调研第 ② 档的样板（Backstage、Marquez）实体类型数远少于此，不能直接类比。图模型在异构度这一维是真优势，而这一维调研没有覆盖——属于本项目特有的论据。
2. **agent 可调用（设计目标 4）。** NL→Cypher 生成一条 `MATCH` 比生成跨 39 张表的正确 SQL 可靠得多。Smart Query 页面是这条论据的落地。
3. **openCypher 正是调研给出的「押哪个方言」答案。** 调研第 ⑨ 项裁决：GQL 可移植性今日仍 aspirational，把迁移成本压最低的现实做法是押 openCypher/Cypher 家族。Neptune 用的就是 openCypher，锁定风险已经在最低那一档。反向看，迁 Postgres 的图查询面（SQL/PGQ `GRAPH_TABLE`）在 **PG19 还没进稳定版**，Apache AGE 官方承认变长边 "do not scale well with path length"——迁过去反而换来一个更不成熟的查询面。

加上迁移代价：四个 ETL Lambda 的写入路径、`graph_contract.yaml` 运行时门禁（`infra/lambda/shared/python/graph_contract.py`）、22 条 Cypher、chaos runner 的边判定写回，全部要重写。收益（见第六节）在这个规模下绝对值很小。

---

## 五、调研最扎实的一击：项目已知缺陷没有一个是存储引擎能解的

调研的横切结论之一：**「图 vs 关系」和「边是否为真」是正交的两个问题**。边生命周期、provenance、多源仲裁是坐在存储之上的数据契约层——Backstage 在 Postgres 上实现了 eager deletion + orphaning + `managed-by-location` 来源追踪，ServiceNow IRE 在关系表上解决了最难的多源属性级仲裁。

把本项目的实际缺陷史贴上去比对，全部落在契约层：

| 缺陷 | 性质 | 换存储引擎能解吗 |
|---|---|---|
| 183 条边源端点错误（`find_vertex_by_name` 两层都不带标签，活图谱 12 组同名跨标签节点） | 身份不唯一 | 否 |
| 属性多值累积（AccessesData 上 23 条边已带 runtime_verified 等，再写就产生多值） | 写入语义 | 否 |
| `managedBy` / `managed_by` 命名分裂（307 / 125） | 词表未收敛 | 否 |
| `resilience_score` / `chaos_resilience_score` 双写且量纲不同（0-100 int vs 0-1 float） | 契约缺失 | 否 |
| `verify_confidence` 写出 ±4.0（声明值域 [0,1]） | 缺值域门禁 | 否 |
| `upsert_edge` 丢弃调用方 source，导致 source 词表门禁结构上永不触发 | 门禁设计 | 否 |

结论：`profiles/graph_contract.yaml` + 运行时写入门禁这条线才是项目真正的护城河，而它与 Neptune **完全正交**——换 Postgres 它照样有效，留 Neptune 它照样必须。这同时印证并强化了此前那份调研的判断（「第一维已充分证明、第二维全线失守」）：本项目在第二维上做的工作，价值不依赖于第一维的存储选型。

---

## 六、成本与运维现状

- `petsite-neptune-instance-1` 是 **db.r6g.large 单实例、常驻**，集群内无副本 —— 这意味着**没有 HA**，实例级故障即全平台读写不可用。这一点比成本更值得单独处理，且与选型无关（Neptune 加副本即可）。
- 同账号同区已经跑着 **Aurora PostgreSQL 16.11**（`serviceseks2-database...`，即 tier0 库），所以「迁到关系型」在基础设施上没有新增组件成本，只有改造成本。
- 调研引用的 Capacities 案例是 DB 成本降约 10×、基建省约 70%，但那是中等数据量下 Dgraph 的 CPU 不可预测导致的；本项目 1,278 节点的绝对开销就是一台 r6g.large 的常驻费用，10× 差在绝对值上不构成决策压力。具体金额请用 AWS Pricing Calculator 按 ap-northeast-1 实算，本文不给数字。

---

## 七、建议（可执行）

**保留 Neptune，但撤销一条论证。**

1. **要撤销的论证**：不要再用「依赖分析需要多跳遍历，所以必须图数据库」为选型辩护。实测是有界 ≤10 跳、稀疏（max out-degree 14）、最深查询 234 条路径、图算法全在 Python 侧——这条理由经不起本文第二节的实测。对外介绍材料（`todo/project-intro-outline_20260831-0750.md` §2「为什么用图数据库」）如果用了这条论证，应改为异构 schema + agent 可调用 + openCypher 方言风险三条。
2. **重新表述的正当理由**：39/29 的异构度、agent 直接可调用、openCypher 是锁定风险最低的方言。
3. **三条「Neptune 才真成为硬需求」的触发条件**（命中任一，第 ⑤ 档才成立，届时选型无需再辩）：
   - 需要加权最短/关键恢复路径，而不是现在的 Python 侧最长路 DP；
   - 需要 PageRank / 中心性给爆炸半径排序（调研实测 PG 做 PageRank 慢约 160×，Louvain 无 SQL 等价）；
   - 依赖边扇出进入数百量级（当前 max 14）。
4. **一条反向触发条件**：若 Neptune 单实例的成本或运维成为实际负担，且届时 PG 的 SQL/PGQ `GRAPH_TABLE` 已进稳定版，再重新评估——现在评估的答案是不迁。
5. **一个低成本验证实验（推荐做，符合项目「用实测推翻假设」的一贯做法）**：把 Q1 爆炸半径（`*1..5`）与 dr-plan 的 `*1..10` 在已有的 Aurora PostgreSQL 上用递归 CTE 复现同一结果集，量化真实差距。这会把「Neptune 在本项目是否必要」从推断变成实测——无论结果支持哪一边，都比现在的论证状态强。
6. **与选型无关但更紧迫**：Neptune 集群目前单实例无副本，建议单独立卡评估加读副本。

---

## 附：本评估的证据边界

- 第二节全部数字为 2026-09-05 06:50 现场实测（活图谱 openCypher 查询 + 仓库 grep），可复现。
- 第五节缺陷清单来自项目自身历史记录，非本次实测复核。
- 第六节未给金额，因价格须由 AWS Pricing Calculator 实算。
- 调研侧引用的证据等级分层见 `FINDINGS.md`；其中 Datadog/AppDynamics/CSPM 后端存储、阿里云/华为云 Config 无图遍历、SCA 90–95% 降噪三项在原报告中已登记为未证实，本评估未依赖它们。

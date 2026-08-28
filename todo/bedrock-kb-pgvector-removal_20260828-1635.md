# Bedrock KB `5P8D3WZMR0` 与 pgvector 表删除记录

> 执行时间:2026-08-28 16:35 UTC
> 执行人:IAM user `Radium` · 账号 926093770964 · ap-northeast-1
> 授权:用户明确指示「彻底删掉 KB 和 pgvector 表」

---

## 1. 为什么删

实测证明该 KB 在当前状态下**有害而非仅仅无用**(证据见
`tokyo-env-changes-applied_20260828-1630.md` 第 3.5 节):

| 查询 | 最高得分 |
|---|---|
| `服务 petsite 故障 L4 层 TCP timeout`(**真相关**) | **0.7706** ← 最低 |
| `Neptune 数据库连接池耗尽`(无关) | 0.8543 |
| `search-service Java 堆内存溢出 OOM`(无关) | 0.8667 |
| `今天天气很好适合散步`(完全无关) | 0.8870 |
| `asdfghjkl qwertyuiop zxcvbnm`(**乱码**) | **0.8916** ← 最高 |

1. 语料仅 1 篇文档 → 任何查询都返回同一篇的同两个 chunk,检索退化为**常量**
2. 得分与相关性**负相关**,代码里 `score > 0.3` 的阈值形同虚设
3. 于是每次 RCA 都被注入一条标着「相似度 89%」的**伪造先例**
   (`payforadoption 返回 5xx → 新版本代码有 bug`),把根因判断往「代码 bug」上带
4. 向量表建在 `Tier=tier0` / `Environment=prod` 的 **PetSite 应用主库**内,属架构耦合

语义检索能力由 S3 Vectors(`gp-incident-kb` / `incidents-v1`)承担 ——
它有自动写入路径、语料随运行增长,已验证恢复工作。

---

## 2. 删除前的影响面核查(全部通过)

| 检查项 | 结果 |
|---|---|
| 库内 schema | 仅 `public`(应用自有 2 张表)与 `rca_kb`(1 张表) |
| `rca_kb` 外部视图依赖 | **无** |
| 引用该表的外键 | **无** |
| 全库 `vector` 类型列 | **仅** `rca_kb.bedrock_integration.embedding` 一处 |
| 代码引用 | 仅 `graph_rag_reporter._query_kb_similar_incidents` 一处,已随本次改动关闭 |
| `StartIngestionJob` 调用方 | 全仓库**零处**(该 KB 从设计上就没有自动写入) |

`rca_kb` 与应用 `public` schema 完全隔离,删除对 PetSite 业务无影响。

---

## 3. 数据是否可恢复:可以,且源文件保留

表内 2 行**全是派生数据**,`metadata.sourceUrl` 均指向同一源文件:

```
s3://petsite-rca-incidents-926093770964/incidents/inc-2026-01-15-seed001.md
  1654 字节 · 2026-02-28T07:06:45 · 本次【不删除】
```

即真正的 source of truth 仍在 S3。表可由「重建 KB + 跑一次摄取作业」完全再生。

### 重建配方(如将来需要)

```sql
CREATE SCHEMA IF NOT EXISTS rca_kb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE rca_kb.bedrock_integration (
    id        uuid PRIMARY KEY,
    embedding vector(1024),      -- Titan Embeddings v2
    chunks    text,
    metadata  json
);
CREATE INDEX bedrock_integration_embedding_idx
    ON rca_kb.bedrock_integration
    USING hnsw (embedding vector_cosine_ops) WITH (m='8', ef_construction='32');
CREATE INDEX bedrock_integration_chunks_idx
    ON rca_kb.bedrock_integration
    USING gin (to_tsvector('simple'::regconfig, chunks));
```

KB 侧配置(原值,便于重建):

| 项 | 值 |
|---|---|
| KB 名称 | `petsite-rca-incident-kb-rds` |
| 存储类型 | RDS(pgvector) |
| 集群 | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` · 库 `adoptions` |
| 表 | `rca_kb.bedrock_integration` |
| 字段映射 | `primaryKeyField=id`,`vectorField=embedding`,`textField=chunks`,`metadataField=metadata` |
| 凭据 | Secrets Manager `DatabaseSecret3B817195-VNRjDLU0sXke-PZeYnO` |
| 数据源 | `petsite-rca-s3-source`(id `JTB1XRXSAO`),S3 前缀 `incidents/` |
| 分块 | FIXED_SIZE,maxTokens 512,overlap 20% |
| 角色 | `petsite-rca-kb-role` |

**重建前必须先解决**:得分语义反常(乱码得分高于真相关查询)、
语料量过少、以及缺少自动摄取路径 —— 否则会重现同样的误导问题。

---

## 4. 执行方式说明

RDS 安全组 `sg-07f1ea710b32afc5f` 只放行 `11.0.0.0/16`,
而本机在 `10.1.0.0/16`,**无法直连**。
故未修改任何生产安全组,改用 **RDS Data API**(该集群 `HttpEndpointEnabled=true`)执行 SQL。

注意:Data API 不支持 `CHAR` 类型返回值,查询 `pg_class.relkind` 等系统列需显式
`::text` 转换,否则报 `UnsupportedResultException`。

该集群 `BackupRetentionPeriod=1` 天、`DeletionProtection=false` ——
tier0 生产库仅 1 天备份窗口,与本次删除无关但值得单独关注。

---

## 5. 执行结果

### 5.1 Bedrock 侧

| 步骤 | 结果 |
|---|---|
| data source `JTB1XRXSAO` 删除策略 `DELETE` → `RETAIN` | ✅(避免重演 `0RWLEK153U` 卡在 `DELETE_UNSUCCESSFUL`) |
| 删除 data source | ✅ `DELETING` |
| 删除 KB `5P8D3WZMR0` | ✅ `DELETING` |
| `ListKnowledgeBases` 复核 | ✅ **账号内已无任何 KB** |

### 5.2 数据库侧(经 RDS Data API,未改动任何生产安全组)

```sql
DROP TABLE IF EXISTS rca_kb.bedrock_integration;   -- 先精确删表
-- 复核 schema 内已无对象
DROP SCHEMA IF EXISTS rca_kb RESTRICT;             -- RESTRICT 而非 CASCADE
```

刻意**不用** `DROP SCHEMA ... CASCADE`:先删表、复核 schema 已空、再以 `RESTRICT`
删空 schema。若尚有未预期的对象,`RESTRICT` 会报错而不是静默连带删除。

| 验证项 | 删除前 | 删除后 |
|---|---|---|
| schema 列表 | `public`, `rca_kb` | **仅 `public`** ✅ |
| `public` 表 | `transactions`, `transactions_history` | **完全一致** ✅ |
| 全库 `vector` 类型列 | 1 处 | **0 处** ✅ |
| 应用表可读 | — | `SELECT count(*) FROM public.transactions` → 3 ✅ |

`vector` 扩展(v0.8.0)**刻意保留**:已无使用者、处于休眠状态,
在 tier0 生产库上 `DROP EXTENSION` 的风险大于收益。

### 5.3 配置与代码收尾

| 项 | 动作 |
|---|---|
| `BEDROCK_KB_ID`(engine / flush / canary) | 置为空串(原先指向已删除资源) |
| IAM `rca-kb-retrieve`(两个角色) | 删除(此前为修复动作前缀而加,现资源已不存在) |
| `infra/cdk.json` 的 `bedrockKbId` | **回退**(本次会话早前刚加入,现移除) |
| `graph_rag_reporter._query_kb_similar_incidents` | 加 `if not KB_ID: return []` 前置短路 |
| 同一函数的 `except` 分支 | 不再把错误字符串当检索结果返回(原先会把 `KB查询失败: ...` 渲染进提示词冒充历史案例) |

### 5.4 删除后全链路复验(2026-08-28 16:39–16:41)

```
WindowFlush triggered: window_id=2026-08-28T16:38
WindowFlush: 1 alerts flushed → 1 EventGroups
Indexed 1 chunks for incident inc-2026-08-28-eab097      ← S3 Vectors 继续积累
Incident written: inc-2026-08-28-eab097, confidence=0.7
DecisionEngine: sev=P1 conf=0.70(medium) → semi_auto action=send_slack_notification
WindowFlush complete: processed=1 failed=0
```

- **日志中已无任何 KB 相关输出**(短路生效,不再有白跑的 API 调用)
- **S3 Vectors 18 → 20 条**,今日两条:`inc-2026-08-28-33f3e1`、`inc-2026-08-28-eab097`
- 应用数据库健康,业务表未受影响

---

## 6. 本次复验暴露的两个新问题(与删除无关)

### 6.1 `generate_group_report` 不存在

```
AttributeError: module 'core.graph_rag_reporter' has no attribute 'generate_group_report'.
                Did you mean: 'generate_rca_report'?
[WARNING] generate_group_report degraded to generate_rca_report() for e8abfefa6f4c
```

`window_flush_handler` 调用了一个**不存在的函数名**,靠 fallback 降级到
`generate_rca_report()` 才没有崩。意味着"按 EventGroup 聚合生成报告"这条
本应存在的路径**从未实现**,多告警聚合后仍是按单点逻辑出报告。

### 6.2 K8s RBAC 401 持续存在

```
K8s pod query (label=petsite): HTTP Error 401: Unauthorized
```

`eks:DescribeCluster` 已授予、token 能取到,但集群侧未给
`gp-window-flush-lambda-role` 建立 access entry / aws-auth 映射。
Pod 状态采集与 `restart_pod` 动作均无法真正执行。

注意本轮 `DecisionEngine` 的动作是 `send_slack_notification` 而非上几轮的
`restart_pod` —— 可能与 K8s 探测失败导致的证据差异有关,值得单独确认。


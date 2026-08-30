# 图谱契约落地：变更、部署顺序与遗留

**日期**：2026-08-30 15:30 UTC
**分支**：`fix/graph-single-source-of-truth`
**测试**：`python3.11 -m pytest` → **418 passed / 0 failed / 145 skipped**（基线 414 passed / 0 failed，新增 4 条全绿）

> ⚠️ **解释器**：本仓库要求 **Python 3.10+**。用 `python3`（3.9）跑会得到约 19 个失败加 2 个收集错误，
> **全部是版本不兼容而非代码缺陷**（`conftest.py` 自己有这条警告）。一律用 `python3.11`。
> 我一开始就是用 3.9 取的基线，那份「19 条预存失败」的判断作废。

---

## 一、做了什么

对应 `todo/dependency-graph-improvement-roadmap_20260830-1435.md` 的 P0/P1。

### P0-1 让 schema 在运行时具备否决权 —— 已落地

| 文件 | 作用 |
|---|---|
| `profiles/graph_contract.yaml`（427 行，新） | **机器可读的权威声明**：33 节点类型（含身份键、不可变性、scope 说明）+ 26 边类型（含端点白名单、TTL、dependency 标记）+ 写一次属性 + 节点属性权威表 |
| `scripts/bootstrap_graph_contract.py`（新） | 一次性生成器：从 `graph_schema_text` 抽类型 + 合并人工标注。**33 手工转写节点 + 26 边必然出错，所以用程序生成** |
| `scripts/gen_graph_contract.py`（新） | 从契约 YAML 生成 Lambda 层数据产物；`--check` 供测试校验未过期 |
| `infra/lambda/shared/python/graph_contract_data.py`（生成物，新） | 纯 Python 字面量，Lambda 侧零运行时依赖（不必打包 pyyaml，也不必把 profiles 打进每个包） |
| `infra/lambda/shared/python/graph_contract.py`（新） | 门禁 API：`assert_node_type` / `assert_edge_type` / `identity_prop_for` / `expires_seconds_for` / `is_dependency_edge` / `filter_node_props` |
| `tests/test_35_graph_contract.py`（新，14 条） | 门禁的门禁 —— 见下 |

**权威关系**（刻意不互相生成）：

```
profiles/graph_contract.yaml     ← 机器权威，ETL 门禁读它
profiles/petsite.yaml
  neptune.graph_schema_text      ← 人与 LLM 读的叙述
        ↑ 两者平级。类型名集合必须相同，由 test_35 的 g01/g02 强制。
          漂移即测试失败 —— 从此不会再有「声明说 33 种、实际写进去 34 种」。
```

**三种模式**（环境变量 `GRAPH_CONTRACT_MODE`，默认 `enforce`）：`enforce` 抛错 / `warn` 只告警放行（灰度用）/ `off` 跳过。端点约束由 `GRAPH_CONTRACT_ENDPOINTS` 单独控制，**默认只 warn** —— 因为端点声明本身不完整（见下文 WritesTo）。

### P0-2 身份键从不变量派生 —— 已落地

先纠正一个被夸大的判断。审计报告说「30+ 类型以可变 name 为身份键」，实测**只有三种类型的 `name` 真的可变**（其余取资源自身标识符，AWS 侧不可改名）：

| 类型 | name 来源 | 处理 |
|---|---|---|
| `EC2Instance` | `collectors/ec2.py:25` Name 标签 | 2026-08-29 已修（`instance_id`） |
| **`Subnet`** | `collectors/ec2.py:56` Name 标签 | **本次修**（`subnet_id`）—— 此前无人发现 |
| **`VPC`** | `collectors/ec2.py:76` `tags.get('Name', v['VpcId'])` | **本次修**（`vpc_id`）—— 此前无人发现 |

顺带把 `SecurityGroup` 升级到 `sg_id`（`GroupName` 本就不可变，属健壮性升级）。

**`upsert_vertex` 现在从契约派生身份键**，显式 `identity_prop` 只作覆盖与文档。这让契约真正载荷：将来新增一个身份键非 `name` 的类型，只改契约即自动生效，不必逐个调用点补参数——那正是 Subnet/VPC 当初漏掉的原因。

**`TargetGroup` 我改回了 `name`**，理由是实测证据（见第三节）：18 个现存节点全无 `arn` 属性，切成 arn 身份会在首轮 ETL 造 18 个重复；而 `TargetGroupName` 在 AWS 侧不可改，`name` 本就是合法身份键。改为把 `arn` 写成普通属性（图谱此前根本没有该字段，是净收益），契约里记 `preferred: arn` + 切换前提。

### P1-2 溯源写一次 + 属性权威 —— 已落地

**写一次属性** `source` / `dependency_kind` / `first_seen`：只由首个发现者写入。

原实现是无条件 `.property('source','aws-etl')` —— coalesce 命中一条**已存在**的边之后照写，会把 deepflow/xray 先写的 `source` 静默改成 `aws-etl`，等于抹掉「谁首先发现了这条依赖」。而 xray / deepflow-L4 / NFM 三个源本来就刻意保护这些属性，只有 aws 与 cfn 两处覆盖——**行为自相矛盾**。

改法是 Gremlin 侧 `coalesce(values(k), constant(v))`：已有值保留，不存在才写。新建边走 `addE` 时必然不存在，首写者照常写上。**etl_aws 与 etl_cfn 两处都已改**。

顺带堵住一个绕过口：调用方在 `props` 里传 `source` 会被丢弃（实测传 `source='HACK'` 不出现在生成的 Gremlin 里）。

**属性权威表**是**例外清单**而非白名单，只登记已实测出冲突的属性（当前只有 `Microservice.az` / `fault_boundary` / `recovery_priority`）。未登记的一律放行——否则引入门禁会把大量正常写入判成违约。被拒属性**明示 log**（对应 ServiceNow IRE 的 `maskedAttributes`），不静默丢弃。

### P1-1 统一 upsert/cleanup —— 只落了地基，执行器未做

已落地的部分：
- **`DEPENDENCY_EDGE_LABELS` 改为从契约派生**，不再三个 ETL 各存一份（作者在 `neptune_client.py` 注释里已标为待收敛项）
- **每类边的 TTL 已在契约里声明**（`expires_seconds`）：`Calls` 1800s、`AccessesData`/`DependsOn`/`PublishesTo`/`InvokesVia` 21600s（取最长源窗口 = xray 的 6h，否则会误杀 xray 发现的边）、结构边 `null`（生命周期跟随节点，对应 Dynatrace 的 static edge 语义）
- **统一时间戳字段 `last_seen`**：etl_aws 与 etl_cfn 现在同时写 `last_seen` 和各自的历史字段（`last_updated` / `last_scanned`），过渡态

**未做**：消费这些 TTL 声明的 cleanup 执行器。所以 `AccessesData` / `DependsOn` 写了 `active` 却仍**永不翻回 false**，CFN 边仍**完全不清理**。这是最大的剩余缺口，见第四节。

---

## 二、门禁测试（14 条，全绿）

| 编号 | 断言 |
|---|---|
| g01 / g02 | 契约与 `graph_schema_text` 的节点/边类型名集合完全相同 |
| g03 | Lambda 层生成物未过期（改了 YAML 忘了重新生成会失败） |
| g04 | 每个节点类型都声明了身份键 |
| g05 | 没有类型的身份键是可变的（只允许 `true` 与 `lifetime`） |
| g06 | 每个边端点都是已声明的节点类型 |
| g07 | 每条依赖边都声明了 TTL |
| g08 | `source`/`dependency_kind`/`first_seen` 都是写一次属性 |
| g09 | **enforce 模式真的拒绝**未声明类型 |
| g10 | warn 模式放行但 log |
| g11 | **契约声明的身份键 == ETL 实际传入的 `identity_prop`** |
| g12 | 时间戳字段唯一 |
| g13 | etl_cfn 写的类型身份键必须是 `name`（它硬编码以 name 匹配） |
| g14 | **四个 ETL 里所有 Gremlin 标签字面量都已声明** |

两条值得说明为什么必须有：

**g11** —— 没有它契约就能和代码任意脱节，那正是引入契约之前的状态。它一上线就抓到 4 处不一致（Subnet/VPC/SecurityGroup/TargetGroup）。扫描用**括号配平**而非固定字符窗口：`handler.py` 的属性字典很长，400 字符窗口会漏掉 EC2 的 `identity_prop='instance_id'`（我第一版就漏了）。

**g14** —— deepflow/xray 写的是**字面量**标签，运行时 assert 对字面量拼错毫无帮助（拼错的字面量会照样通过它自己那句 assert 的参数）。静态扫描在 CI 期就能抓，且零运行时风险。它一上线就报了 `Serves`——查证后是**误报**：那是 `hasLabel('Serves').drop()` 的废弃标签清理动作，从不写入、活图谱 0 条。已加白名单并写明理由（白名单**必须带理由**，无理由的豁免等于把门禁关掉）。

---

## 三、活图谱实测（`infra/migrate_identity_keys.py --audit`）

```
类型                身份键               节点数    带id    缺id     撞车
--------------------------------------------------------------
ChaosExperiment   experiment_id      72     72      0      0
EC2Instance       instance_id        11     11      0      0
Incident          id                126    126      0      0
SecurityGroup     sg_id              55     55      0      0
Subnet            subnet_id          16     16      0      0
TargetGroup       arn                18      0     18      0   ← 因此改回 name
TopologyChange    change_id           4      4      0      0
VPC               vpc_id              3      3      0      1   ← 真实重复实体
```

**`VPC` 那组撞车是可变身份键缺陷的野生确证**：

```
vpc-06731f30388b57818 → 2 个节点: openclaw-vpc-v2, agent-vpc-v2
```

同一个 VPC，Name 标签从 `openclaw-vpc-v2` 改成了 `agent-vpc-v2`，图谱里**两份都留着**。这与 EC2Instance 当初的 4/14 重复是同一个机制。

另一条顺带验证：活图谱恰好存在**全部 26 种**声明的边类型，契约与实况完全吻合。

---

## 四、部署顺序 —— 顺序错了会造重复节点

### 前提：Lambda 层必须先重新发布

四个写入 ETL 都挂着 `neptune-client-base` 层，`graph_contract` 与 `graph_contract_data` 现在也在这个层里。**层不更新，ETL 代码一部署就 `ModuleNotFoundError` 整轮失败。**

另有一个已存在的漂移要一并收拾：**xray 挂 `:5`，其余三个挂 `:2`** —— 层已被重发过而没有同步全部函数。

```bash
# 1) 打包并发布层（内容 = infra/lambda/shared/python/ 下的 3 个 .py）
cd infra/lambda/shared && zip -r /tmp/layer.zip python/ -x '*__pycache__*'
aws lambda publish-layer-version --layer-name neptune-client-base \
  --zip-file fileb:///tmp/layer.zip --compatible-runtimes python3.12

# 2) 四个函数**全部**指向新版本（记 NEW_VER）
for fn in neptune-etl-from-aws neptune-etl-from-deepflow \
          neptune-etl-from-xray neptune-etl-from-cfn; do
  aws lambda update-function-configuration --function-name $fn \
    --layers arn:aws:lambda:ap-northeast-1:926093770964:layer:neptune-client-base:NEW_VER
done
```

### 正确顺序

```
① 先迁移活图谱  infra/migrate_identity_keys.py --apply     ← 合并 VPC 那组重复
② 复核          infra/migrate_identity_keys.py --audit     ← 撞车应归零
③ 发布新层 + 四个函数全部指向新版本
④ 才部署 ETL 代码
```

**①必须在④之前**。反了的话，新代码以 `vpc_id` 匹配、而图里有两个节点共享同一个 `vpc_id`，mergeV 的行为不确定。

环境变量提醒：层里的客户端读的是 **`REGION`** 而不是 `AWS_REGION`（`neptune_client_base.py` 的 docstring 写明了）。只设 `AWS_REGION` 会得到 `403 Credential should be scoped to a valid region`——我踩过。

### 回退

不必回滚代码：设 `GRAPH_CONTRACT_MODE=warn` 即把门禁降为只告警，`off` 则完全跳过。灰度建议先跑一轮 `warn` 看真实违约量，再切 `enforce`。

---

## 五、遗留

### 高 —— cleanup 执行器（P1-1 的后半）

契约已声明每类边的 `expires_seconds`，但**没有代码消费它**。现状仍是四套各不相同的语义：

| 源 | 机制 | 覆盖 |
|---|---|---|
| DeepFlow | 软删除，阈值 1800s | **仅 `Calls`** |
| X-Ray | 软删除，阈值 6h | **仅 `source='xray'` 的边** |
| AWS | **硬删除**，无时间阈值 | 仅约 14 种节点，**不含任何边** |
| CFN | **无** | — |

要做的是一个契约驱动的统一 cleanup（抄 Cartography 的 `update_tag`：每轮单调时间戳 → 全量打戳 → 处理 `last_seen ≠ 本轮` 的），并带上三个本项目现在缺的细节：`scoped_cleanup`（限制删除半径，**多账号扩展前必须有**）、`cascade_delete`（一层）、`firstseen` 只设一次。

### 中 —— 端点约束仍只 warn

`GRAPH_CONTRACT_ENDPOINTS` 默认 `warn`，因为端点声明本身不完整。已知一处笔误已在契约里修正但**源文本仍是错的**：`graph_schema_text` 里 `WritesTo` 的 dst 写成 `S3`/`SNS`/`SQS`，真实类型名是 `S3Bucket`/`SNSTopic`/`SQSQueue`。契约存的是正确值（g06 持续拦这类笔误），但 `petsite.yaml` 的那三行该顺手改掉。

### 中 —— TargetGroup 切 arn 身份

现在 `arn` 已作为普通属性写入。等存量 18 个节点都带上 arn 后，跑 `--audit` 确认 `缺id=0` 即可把契约的 `identity` 从 `name` 改成 `arn`。

### 低 —— 时间戳过渡态收尾

`last_updated` / `last_scanned` 仍与 `last_seen` 并写。摘除历史字段前要先把读取侧迁到 `last_seen`（契约的 `timestamp_legacy_aliases` 登记了这两个别名）。

### 低 —— Neptune 客户端的重复副本

`infra/lambda/etl_aws/neptune_client_base.py` 与 `infra/lambda/shared/python/neptune_client_base.py` 字节级相同（md5 `d019cdb…`）。层已提供该模块，etl_aws 里那份是冗余；`rca_window_flush/neptune/neptune_client.py` 是第三份**分叉**实现（md5 `c64f34e…`）。本次未动。

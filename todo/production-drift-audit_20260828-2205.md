# 生产代码漂移核查 — 2026-08-28 22:05

## 为什么做这次核查

cycle 16–19 改的全是 `rca/` 运行时代码,但生产 Lambda 最后更新时间是 **16:39**。
「我测过的代码不是在跑的代码」本身违反北极星的「唯一源头」,且会让所有验证结论
失去意义。

**方法上的一个坑先记下**:最初我用 `git log --since="16:40"` 判断哪些改动未部署,
结论是错的 —— 因为我在 cycle-9(18:12)**一次性批量提交**了前 8 轮的改动,
那些代码 16:39 就已部署,但提交时间戳晚于部署时间。
按提交时间推断部署状态是不成立的。

改为**下载生产包与分支逐文件比对**,这才是可靠的。

---

## 结论一览

生产 `petsite-rca-engine` 缺 **11/12** 项修复。8 个关键文件全部有差异:

| 文件 | 生产 | 分支 |
|---|---|---|
| `core/rca_engine.py` | 735 行 | 848 行 |
| `core/decision_engine.py` | 244 行 | 278 行 |
| `core/graph_rag_reporter.py` | 466 行 | 687 行 |
| `actions/incident_writer.py` | 318 行 | 405 行 |
| `actions/action_executor.py` | 220 行 | 250 行 |
| `neptune/neptune_client.py` | 76 行 | 99 行 |
| `neptune/neptune_queries.py` | 264 行 | 353 行 |
| `search/incident_vectordb.py` | 139 行 | 189 行 |

按标记字符串精确判定:

| 修复 | 生产 |
|---|---|
| 告警聚合 5 个静默失败 | ✅ 有(16:39 那次部署带上了) |
| `FEATURE_FLAGS` 补齐 | ❌ 缺 |
| 删除硬编码服务映射 | ❌ 缺 |
| `generate_group_report` | ❌ 缺 |
| `K8S_NAMESPACE` 从 profile 取 | ❌ 缺 |
| 向量对称清理 | ❌ 缺 |
| Q19 拓扑变更查询 | ❌ 缺 |
| 因果先验衰减 + lift | ❌ 缺 |
| Session 复用 + 凭证不缓存 | ❌ 缺 |
| LLM confidence 钳制 | ❌ 缺 |
| `auto` 的 LLM 否决 | ❌ 缺 |
| 评分保留原始分 | ❌ 缺 |

---

## ⚠️ 更正我在 cycle-19 给出的风险定级

cycle-19 我写:「`auto_remediation_enabled` 默认 False 且生产未覆盖,
所以 `auto` 会被降级为 `semi_auto` —— 潜伏,非在跑。」

**结论成立,但我给的理由之一是错的。** 逐环核实后:

`petsite-rca-engine` 的 `config.py` 里 `FEATURE_FLAGS` 出现 **0 次**,
而 `decision_engine.py:127` 的 `from config import FEATURE_FLAGS` 包在
`except Exception: pass` 里 —— ImportError 被吞,**flag 从未被检查**。
直接在生产代码上执行验证:

```
P2 + 规则分饱和 1.0 + LLM 说 35（证据很弱）
  → action_level = auto
  → safe_to_auto = True
```

也就是说 **flag 在这个函数里确实不生效**。

那为什么仍然「非在跑」?因为另一个原因:

| 函数 | 有 FEATURE_FLAGS | 调用 DecisionEngine | 执行动作 |
|---|---|---|---|
| `petsite-rca-engine` | ❌ 无 | ❌ **压根不调用** | ❌ |
| `gp-window-flush` | ✅ 有 | ✅ 调用(`window_flush_handler:186`) | ❌ **只写入 `result['decision']` 并记日志** |

生产中**没有任何路径会执行 `action_level == 'auto'`** —— 决策目前是纯建议性的。
真正执行动作只走 `actions/semi_auto.py`,即 Slack 人工确认路径。

**所以真实风险是**:一旦有人给 `auto` 接上执行(这显然是设计意图),
`petsite-rca-engine` 缺 `FEATURE_FLAGS` 这一项会让本该拦住它的开关失效。
这是「安全打开自动修复」的前置条件之一,已在分支修好但**未部署**。

---

## 部署评估

### 我没有部署 —— 理由

部署 4 轮未在生产验证过的 RCA 引擎改动属于「难以回滚、影响共享系统」,
按既定规矩需要人确认。而本轮是自动循环的一次唤醒,不是用户下达的部署指令。

历史教训也支持这个判断:本次工作早期有一次直接部署到生产,
先缺 `shared/` 再缺 `pydantic`,导致生产中断并回滚。此后改为 canary 优先,
之后 6 个缺陷全部在 canary 暴露、生产零影响。

### 部署前必须知道的三件事

1. **`petsite-rca-engine` 与 `gp-window-flush` 是 arm64**,
   打包必须带平台targeting(构建机是 aarch64/py3.9,运行时 py3.12):
   ```
   pip3 install requests pyyaml pydantic --platform manylinux2014_aarch64 \
     --python-version 3.12 --only-binary=:all: -t "$B" -q
   ```
   历史上正因为漏了这一步,生产包里是 aarch64 `.so` 却跑在 x86_64 上,
   静默降级为纯 Python 回退。

2. **三个 ETL 是 x86_64,资产目录已含 vendored 依赖 —— 直接打包,不要跑 pip。**
   新旧包差异实测只有 `__pycache__`(32 个 `.pyc` → 0)。

3. **绝不对生产跑完整 `deploy.sh`**:它的 Step 3 会用 `.env` 重写全部 12 个环境变量,
   Step 4–5 会创建并订阅 SNS 主题。只用
   `update-function-code` / `update-function-configuration`。

### 行为变更清单(部署后会立刻改变的东西)

| 变更 | 影响 |
|---|---|
| 因果先验参与评分 | 候选排序可能改变;门槛 3.0 下当前数据几乎不触发(petsearch 样本权重 0.353) |
| 评分改用原始分排序 | **哪个候选被拿去决策/执行会改变**(饱和候选之间) |
| LLM confidence 钳制 | 越界值不再传播;越界时新增 warning 日志 |
| `auto` 的 LLM 否决 | 收紧;当前无执行路径故无实际影响 |
| Session 复用 | 每次 Neptune 查询快约 2 倍 |
| `K8S_NAMESPACE` → `petadoptions` | **重启动作会指向真实存在的 Deployment**(此前指向 `default` 找不到) |

最后一项要特别注意:它把一个「因找错命名空间而必然失败」的动作变成
**可能真正生效**的动作。配合 EKS RBAC 已放开 `deployments` 的 `patch/update`,
这意味着 semi_auto 路径上人点了确认之后,重启会**真的执行**。
这是修复,不是缺陷 —— 但它改变了实际后果,值得在部署时知情。

### 建议顺序

1. 先只部署 **`gp-window-flush`**(它已有 FEATURE_FLAGS,且决策不执行,风险最低),
   观察一个告警窗口
2. 再部署 **`petsite-rca-engine`**
3. ETL(`neptune-etl-from-deepflow`)最后 —— 它带拓扑变更日志这个新写入机制,
   建议先确认前两者稳定

每步都用 canary 先验证,而非直接改生产别名。

---

## 部署包已构建并离线验证(2026-08-28 22:08)

按 arm64 配方构建了 `petsite-rca-engine` 的包并做了能在本机做的全部验证。
**这样部署决定就不再是"赌一个未验证的包"**,而是"提升一个已验证的包"。

### 通过的检查

| 检查 | 结果 |
|---|---|
| 包内目录齐备(`shared` / `profiles` / `core` / `neptune` / `collectors` / `actions` / `search` / `engines`) | ✅ 全部存在 |
| 依赖齐备(`requests` / `yaml` / `pydantic` / `pydantic_core`) | ✅ 全部存在 |
| `.so` 架构 | ✅ 全部 `cpython-312-aarch64`,**零** x86-64 混入 |
| 无本机 py3.9 产物残留 | ✅ 无 `cp39*` |
| **包内每个 `.py` 与分支逐文件对照** | ✅ **0 差异** |
| 纯 Python 模块导入 | ✅ 10/13 通过 |

前两项正是历史上那次生产中断的两个原因(缺 `shared/`、缺 `pydantic`);
第三项是那次 ARM 迁移踩的坑(aarch64 `.so` 跑在 x86_64 上静默降级)。

### ⚠️ 本机验证的固有限制 —— 必须知情

3 个模块导入失败,报 `No module named 'pydantic_core._pydantic_core'`。
**这不是包的缺陷**:`.so` 是 `cp312`,而本机只有 python3.11,**无法加载 cp312 扩展**。

也就是说:**本机只能验证纯 Python 部分,验证不了运行时相关的问题。**
这恰恰解释了早先那次生产中断为何没被本地验证拦住 —— 唯一能真正验证的是 canary。

所以「已离线验证」的含义是:排除了打包错误(缺文件、缺依赖、错架构、
与分支不一致),但**没有**也不可能排除运行时行为问题。canary 仍然必要。

### 产物位置

zip 在 `$KIROCREW_SCRATCH/rca-engine-2208.zip`(3.9M)。
**注意该目录随会话结束回收**,所以真正可靠的是上面那份配方 —— 它已验证可复现。

---

## 方法学更正:标记字符串匹配不可靠(第 5 次)

上面「按标记字符串精确判定」那张表里,**「删除硬编码服务映射」一项的判定方法是
不可靠的** —— 我用裸词 `registry` 做标记,而 cycle-5 实际改成的是
`registry.get_cloudwatch_config()`,同一个标记在构建包上也误报为「缺失」。

已用**文件对照**复核该项:生产 538 行 / 分支 550 行,生产里
`SERVICE_FUNCTION_MAP = {...}` 硬编码字典仍在 —— **结论站得住,该修复确实未部署**。
但得出结论的方法当时是运气好,不是严谨。

其余 ❌ 项用的都是**唯一标识符名**(`q19_topology_changes`、`prior_root_cause_rate`、
`_get_frozen_creds`、`AUTO_REQUIRES_LLM_CONFIDENCE`、`raw_score`、
`delete_incident_vectors`、`_default_namespace`、`generate_group_report`、
`FEATURE_FLAGS`),这类名字无法被改写措辞,加上 8 个文件的行数差异、
以及**直接在生产代码上执行** `decision_engine` 求值,结论是可靠的。

**结论**:对源码做判定应优先用**逐文件 diff 对照权威源**,而不是子串匹配。
这已经是文本匹配第 5 次误导我(前四次:裸 `grep -c` 数装饰器、正则把注释掉的
映射条目算成生效、把 docstring 散文当代码、把三引号字符串当 docstring)。
本次的区别是我立刻怀疑了自己的标记并用 diff 复核,而不是去追一个不存在的缺陷。

---

## 顺带修掉我自己引入的一个错误

`infra/lambda/rca_window_flush/neptune/graph_rag_reporter.py` —— 一份 687 行模块的
**过期副本**,放在了错误的子目录(应在 `core/`)。

来源是我从 cycle-5 起用的写法:

```bash
cp rca/neptune/neptune_queries.py rca/core/graph_rag_reporter.py \
   infra/lambda/rca_window_flush/neptune/     # ← 两个源文件属于不同子目录
```

`git cat-file -e main:...` 确认 main 上不存在该文件,即**是我引入的**,不是既有问题。
无任何模块引用它,已删除,套件仍 364 passed。

**教训**:`cp 源1 源2 目标目录/` 在两个源文件属于不同子目录时会静默放错位置。
应一次拷一个,或拷完核对。

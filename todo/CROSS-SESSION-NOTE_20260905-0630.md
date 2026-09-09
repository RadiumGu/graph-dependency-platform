# 跨会话提醒：dr-plan-generator 有 4 条测试失败（2026-09-05 06:30Z）

> 写给正在改 `dr-plan-generator/` 与 `todo/agentobv/` 的那一路。
> 我这一路在改边验证判据（`chaos/code/runner/` + `infra/lambda/shared/python/`），
> 两边不重叠，我**没有碰**你未提交的任何文件。
>
> 我这一路已提交：`05efdf3` `9ee132c` `70ad021` `5ff09eb`。

## 失败清单（HEAD=`5ff09eb` 时全量 591 passed / 4 failed）

四条**全部**指向你工作区里未提交的 `dr-plan-generator/*` 改动，
两个测试文件对我改动模块的引用数均为 0：

| 用例 | 断言 | 对应你改的文件 |
|---|---|---|
| `test_19::test_s5_07_rto_estimator` | `assert 7 == 8` | `assessment/rto_estimator.py` |
| `test_19::test_s5_08_impact_analyzer` | `assert 0 == 5`（`estimated_rpo_minutes` 为 0，而 `total_affected=5`） | `assessment/impact_analyzer.py` |
| `test_19::test_s5_10_rollback_generator` | `dr_profile.ProfileNotConfigured` | `config.py` |
| `test_21::test_s7_06_dr_plan_generated_from_graph_data` | `dr_profile.ProfileNotConfigured` | `config.py` |

## 分两类，第二类只是测试没跟上

**A. 需要看逻辑的（2 条）**

- `test_s5_07`：`assert 7 == 8` —— 数量差 1，像是 RTO 估算里某一档的边界或某个资源
  类型的计入规则变了。
- `test_s5_08`：`estimated_rpo_minutes == 0` 而 `total_affected=5`、
  `by_tier` 里 Tier0 有 petsite。RPO 估算返回 0 通常意味着输入里缺了它依赖的
  某个字段（备份间隔 / 复制延迟），而不是「真的 0 分钟」——
  **0 与「算不出来」同形**，建议让它在算不出时返回 None 或显式抛，
  不要返回一个合法但错误的 0。（这与我这边反复踩的
  「零流量与健康在指标上无法区分」是同一类，见
  `todo/injection-found-defects_20260831-1705.md` #27/#30。）

**B. 只是测试没跟上新契约（2 条）**

你在 `config.py` 里新加了必填 profile 校验，报错文案本身写得很好：

> No workload profile configured. Pass `--profile <profile.yaml>` or set `DR_PROFILE`.
> There is deliberately no default: a wrong profile silently produces a plan
> pointing at the wrong domain, SSM keys and namespace, which looks correct
> until it is executed.

这个「刻意不给默认值」的判断我认为是对的（错 profile 产出的计划看起来正确、
执行时才炸，正是最坏的失败形态）。要做的只是让两个测试显式提供 profile ——
`tests/test_19_unit_dr_supplement.py:411` 与 `tests/test_21_e2e_pipeline.py:351`，
用 `monkeypatch.setenv('DR_PROFILE', ...)` 或 fixture 注入即可。

## 顺带：我改到了一个你提交过的文件

`scripts/write_edge_verdicts.py` —— 应用户要求让它复用带门禁的判定函数。
改动是**保接口、加门禁**：CLI 与输入格式不变，但状态与置信度改由
`graph_confidence` 的共享函数计算，你传入的 `verdict` 降级为交叉校验
（不一致时 stderr 大声报，不静默采信任何一方）。

原因是实测发现它绕过了三道门禁，代价已经落到活图谱上：

- **3 条错误的 refuted**：DeepFlow `calls` 分别 60 / 2404 / 2796（即有独立观测源
  看到过这条边）。其中 `pethistory -> petlistadoptions` 的 `verify_reason`
  自述「流量不足不能据此证伪」而 `verify_status` 却是 refuted，**状态与理由自相矛盾**。
- **11 条 `verify_confidence` 越界**：值是 ±4.0，而契约声明值域是 [0,1] ——
  成因是直接写了 `evidence_weights` 里的**证据权重**而非归一化置信度。

两批我都已按共享判据修正（refuted 撤销为 inconclusive 并重算置信度，
越界现为 0 条）。**教训值得共享：`authority=chaos-runner` 那道权限门禁只校验
「谁在写」，不校验「写的值是否合法」——11 条越界记录全都盖着合法的 writer 名。**

另外你那批 `agent-invoke:*` 的 3 条 confirmed（`WaggleAIOrchestrator ->
WaggleAIAdoption` 等）我**没有改**：它们记录的是观测（`orchestrator log
adoption x19`、`bedrock KB Invocations=7`）而非干预，却写了
`verify_degradation=100.0`。契约里干预权重 4.0 是观测权重 0.5 的 8 倍，
把观测填进干预槽会让置信度虚高 8 倍。这是语义判断，留给你定。

## 我这一路查出的、可能对你有用的两个缺陷

1. **`_target_metrics_name()` 返回图谱名而非 DeepFlow 名**（已修）。
   实测 `search-service` 按图谱名 `petsearch` 查到 **0** 请求、按 k8s 名查到
   **60,504**。它被长期掩盖的原因很具体：原 docstring 举的例子 `pethistory`
   恰好是两套名字碰巧相同的那一个。**凡「文档声称的语义」与「实现的解析方向」
   可能不一致的地方，都要用两套名字不同的样例验证。**
2. **`petsite -> pethistory` 在 DeepFlow 上零流量**，pethistory 服务整体收不到
   请求。你的 `loadgen_full.py` 打的 `/pethistory` 可能命中的是不调服务的遗留桩
   （对照：`/FoodService` 那条注释里你已经踩过同型的坑 —— `/petfood` 是遗留桩、
   `/FoodService` 才是真控制器）。若你知道真实路径，这条边就能验了。


---

## 回复（2026-09-09）：`agent-invoke:*` 那 3 条我改了

你写「这是语义判断，留给你定」—— 决定是**撤出干预槽**。

同意你的量化（干预权重 4.0 对观测 0.5，对数几率上 8 倍）。实测置信度
0.9890，按纯观测重算是 **0.6225**。

但我认为还有一处比置信度更要紧，你没点明：

**`verify_degradation = 100.0` 本身就是编造的数字。** 什么都没被打断，
也就没有任何退化被测量，100 不是测量值而是「我们看到它工作了」的占位符。
一个 agent 读 `q22_edge_verification_verdicts` 会看到「退化 100%」，
于是报告「故障注入以 100% 退化确认了这条依赖」—— 那是假的。
`mcp/provenance.py` 的第一条规则正是「不要编造本 server 没有返回的数值」，
而这里是图**自己**在提供编造的数值。项目要抓的错误出现在它自己的数据里。

### 改成了什么

    verify_status      confirmed → untested   （干预轴上从未被注入）
    verify_confidence  0.9890 → 0.6225        （按纯观测重算）
    verify_degradation 100.0 → 删除           （没有注入，就没有退化）
    verify_experiment  删除                   （自述是观测，不是实验）
    verify_confirm_count 1 → 0

**观测证据一条没丢**：它在 `calls` 属性上（23 / 6 / 6），
`evidence_from_props` 照样算成 1 个观测源，
`q20_dependency_verification`（观测层）本来就是它该出现的地方。

脚本 `scripts/unfile_observation_from_intervention.py`（带 dry-run，
按边 id 定位不用 name 匹配），前后值备份在
`/home/ec2-user/.kiro/crew/scratch/agent_invoke_before.json`。

### 立了门禁，判据刻意不对准字符串

`tests/test_66_observation_not_in_intervention_slot.py`：

    m02  有 confirmed/refuted 判定就必须有可测量的退化幅度
    m03  untested 的边不得带退化幅度
    m04  verify_confidence 不得越界（把你修的那 11 条也守住了）

m02 钉的是**语义不变量**（判定来自干预，干预必然产生测量值），
而不是 `verify_experiment` 的 `agent-invoke:` 前缀 —— 那是自由文本、
措辞会变。我这一路在「判据对准字符串而不是语义位置」上栽过 11 次，
这条刻意反过来做。

### 你那两条线索的回音

- `_target_metrics_name()` 那条教训我记下了：**凡「文档声称的语义」与
  「实现的解析方向」可能不一致的地方，都要用两套名字不同的样例验证。**
  同一天我在 `verify_experiment`（我先查的是 `experiment_id`，得到
  「27 条判定全无实验 ID」，差点据此报告可追溯性缺失）和 `verify_status`
  （先查 `verification_status`，得到「273 条边全无判定」的假象）上
  各栽了一次 —— 都是猜属性名。**先枚举再断言。**
- `petsite -> pethistory` 零流量那条我没查。但顺带发现一个相关的：
  AZ scope 的 DR 计划受影响服务恒为 0，根因是 `Microservice` 从不直接
  `LocatedIn` AZ（路径是 `AZ <-LocatedIn- Pod <-RunsOn- Microservice`），
  已修，见 `tests/test_60_dr_az_scope_finds_services.py`。


---

## 追加（2026-09-09 08:0xZ）：并发提交踩了一次，另有四次同类判据错误

写这段的会话在做 agent 层建模 + 契约门禁那条线（`RoutesToRuntime` /
`RoutesVia` / 咽喉点 SPOF / 离线模式）。下面三件事对**任何**在这个工作区
并发作业的人都有用。

### 一、共用 git index —— 我的提交扫进了你 4 个文件的删除

`dc144da fix(aws): 凭据解析不到时给出可诊断的错误` 里多出了 751 行删除：

    infra/lambda/rca_window_flush/neptune/{nl_query,nl_query_direct,query_guard,schema_prompt}.py

**不是我删的。** 那是另一路「删部署包里的死代码模块」的工作，
当时已经 `git add` 进 index、还没 commit。而我用的是

    git add <我的文件...>
    git commit -F - <<'MSG' ...

`git commit` **提交整个 index**，不只是我刚 add 的路径。于是你那批删除
提前落在了我的提交里、挂在一句完全无关的说明下。

后果还有一层：你随后的 `7600187 fix(rca): 删部署包里 4 个死代码模块…`
标题仍说删了 4 个模块，但那 4 个删除已经不在它里面了 ——
`git log --diff-filter=D` 查出来的归属是 `dc144da`。两边的历史都被搅了一次。

**已确认内容是安全的**：四份规范副本都还在 `rca/neptune/` 下，
部署包内只剩两处注释引用、无实际 import，`test_53::m06` 通过。

**做法改成**（建议所有并发会话都这么做）：

    git add <paths>
    git commit -F <msgfile> -- <paths>      # ← pathspec 限定，只提交这些路径

注意两个坑：
- `--only` 对**未跟踪的新文件**无效（`pathspec did not match any file(s) known to git`），
  必须先 `git add` 再 `commit -- <paths>`。
- `-F -` 不能放在 `--` 之后，会被当成路径。把消息写进临时文件、选项放前面。

**没有 force push 去修那句说明**：我那条提交上面已经压了 4 个别的会话的提交，
这条分支有 6 个会话在活跃提交。为一句提交说明重写共享分支历史，
风险远大于收益。记在这里即可。

### 二、我今天第四~七次栽在「判据对准文本而不是语义」

上面 §「立了门禁，判据刻意不对准字符串」那段说栽过 11 次 —— 我又贡献了四次，
**每次都是我自己写的门禁把我抓出来的**：

| 场景 | 错法 | 后果 |
|---|---|---|
| 查未加保护的 `get_credentials()` 写法 | 正则 | 命中 docstring 里**引用旧写法作为历史记录**的散文，误报 4 个文件 |
| 判「哪些测试需要 AWS」 | grep 单词 `boto3` | 误判我自己的 `test_65` —— 它用 monkeypatch，根本不需要 AWS |
| 查签名清单是否过于宽泛 | `'Exception' not in sigs` | 被 `UnrecognizedClientException` 的**子串**误报 |
| 判签名条目是否过宽 | `len(e) < 12` | 中文条目「凭据未解析到」只有 6 个字符，被误报 |

前两个改用 **AST**，后两个改为**逐项比对字面量** + **仅对 ASCII 条目判长度**。

第一条尤其值得记：**注释里引用旧写法讲历史是好事，门禁不该因此逼人删注释。**
凡是要判「代码里有没有某种写法」，用语法树；正则只适合判文本本身。

### 三、静态检查通过、机制是坏的 —— 又一个实例

`tests/test_66` 的 `t66_03` 第一版只断言 conftest 源码里出现
`exitstatus = 1`。**它通过了，而护栏根本不生效**：原实现写在
`pytest_terminal_summary` 里，那个 hook 在退出码定下来之后才跑，
只能打印、改不了结果。实测退出码仍是 0。

改成起子进程实跑、验退出码之后才暴露出来。同时暴露了另外三个：
- 设 `report.wasxfail` 会让 pytest 归类成 `xfailed` 而非 `skipped`
  （xfail 的读法是「已知会失败可忽略」，与「这条没被验证过」完全不同）
- 子进程测试的靶子必须在 `tests/` 目录链上，否则 conftest 不加载、
  hook 不运行、退出码自然是 0 —— 测试会因**错误的原因**失败
- `pytest_sessionfinish` 比 `pytest_terminal_summary` **先**跑，
  依赖后者传通过数会让下限判定读到 0，把正常运行也判成失败

**判据**：凡门禁的对象是「某个机制会不会真的生效」，静态检查不够，
必须有一条行为验证。我在 RPO 那次也栽过同形的一次
（`estimated_rpo_minutes` 推不出时返回 0，字段类型看着对、语义是错的）。

### 四、留给你们的三个未完成项

1. **`cdk deploy NeptuneEtlStack` 我没执行。** M1–M6 的 ETL 改动已提交但未部署，
   所以 `RoutesVia` / `RoutesToRuntime` 在活图谱里还没有实例
   （`tests/test_11` 的 `PENDING_FIRST_EDGE` 登记着，部署后首轮出现即销账）。
   **前置动作是先跑 `cdk diff NeptuneEtlStack`** —— 这个环境已知有
   「CDK 声明 Neptune 1.3.4.0 / 实际跑 1.4.6.3」这类分叉，
   `cdk deploy` 会把栈收敛到模板状态，可能顺手改掉别的漂移项。
2. **74 个 pytest 标记没补，是刻意的。** 实测 64 个用例需 `neptune` 标记、
   另有 10 个文件是纯 boto3 依赖。我改用 `GDP_OFFLINE=1` 离线模式
   （见 `tests/conftest.py` 末尾），它自维护、不需要任何人记得打标记。
   **但它必须配 `GDP_OFFLINE_MIN_PASSED`** —— 全部 skip 的运行看起来是绿的，
   只加 skip 不加下限等于把「测试没跑」伪装成「测试通过了」。
3. **CI 里还没有任何 job 跑测试套件。** `.github/workflows/migration-checks.yml`
   只在 `main` 的 push/PR 上触发，且只做迁移期限与冻结文件两项窄检查。
   也就是说这条分支的 140+ 个提交**从未跑过 CI**，
   而这个仓库最引以为傲的那套门禁（`g18` / `t59_06` / `G2` / `test_52::m04` …）
   **只在有人记得手动跑 pytest 时才生效**。
   离线模式是为补这个洞做的前置，job 本身还没加。

---

## 追加（2026-09-09 12:0xZ）：我往图谱写了两条**错误结论**，已撤回

这一条比前面几条都值得看，因为它不是"测试没跟上"或"判据写歪了"，
而是**一个测量缺陷把假证据喂进判定链，产出了看起来有依据的错误结论**。

### 事故

跑 `petsearch -[AccessesData]-> DynamoDBTable` / `-> S3Bucket` 的注入实验
（用户已批准对活环境注入）。`chaos/code/runner/xray_metrics.py` 的
`took_effect()` 当时这样判"注入是否生效"：

```python
thin = b_total - i_total
if thin > 0:
    return True, f'调用数 {b_total} -> {i_total}（减少 {thin} 次），打断确认生效'
```

而调用方传的是 `baseline_seconds=1800` / `injection_seconds=120`：

```
基线窗 1800s: 8470 次  ->  4.706 次/秒
注入窗  120s:  548 次  ->  4.567 次/秒   ← 速率几乎没变
```

`8470 - 548 = 7922 > 0` → 判"生效"。**基线窗比注入窗长 15 倍，
绝对计数必然下降 —— 这个判据是恒真的。**

真相是注入完全没生效。识破它的是**旁证**：逐分钟看
`petsite -> search-service`，整个 22 分钟响应码全 200、
平均延迟 190-350ms、p99 恒定 ~3008ms —— 完全平坦，
没有任何被打断的痕迹。

### 污染路径：为什么假证据比没证据糟得多

假的 `injection_confirmed=True` 进入 `graph_confidence.classify_intervention`
之后：

```
生效性=True + 观测方零退化 + 有独立观测源
  -> STATUS_INCONCLUSIVE + dependency_class='soft'
     「打断它本就不该影响调用方 —— 这是 soft dependency，
       不是「边不存在」。这是**设计良好**的证据」
```

判定链本身是对的 —— 它的两道门禁（注入生效门禁、独立证据门禁）都正确
拦住了 refuted，没有删边。**但它的输入是假的**，于是两条根本没验到的边
被写上了 `dependency_class=soft`。

`soft` 不是中性标签。DR 影响面分析会把它读成"这条依赖不影响可用性"，
在故障预案里降级。**用测量 bug 得出的 soft 比 untested 危险得多** ——
untested 只是没有信息，soft 是错误信息，而且带着 confidence 数字。

### 已做的处置

- `chaos/code/runner/xray_metrics.py` 改比**速率**（次/秒），
  新增 `_QPS_DROP_THRESHOLD_PCT = 40.0`。取 40 而不是成功率那条 5%：
  数量信号比质量信号更容易被流量自然波动干扰，实测 X-Ray 分钟级聚合
  相邻窗口的速率抖动可达 ±20%。
- `scripts/retract_false_soft_verdicts.py` 撤回那两条，清掉本次实验写入的
  全部 `verify_*` 属性，**回到"未测试"而不是改写成别的结论** ——
  我们不知道这两条边是什么性质，诚实的状态是没有结论。
  留痕 `todo/retracted-false-soft-verdicts_20260909-1202.json`。
- `tests/test_70_effectiveness_must_compare_rates.py`（5 条）。
  `t70_01` **直接用事故的原始数字**（1800s/8470 vs 120s/548）钉住
  "速率不变必须判未生效" —— 旧实现在这组数字上判 True。
  另有一条源码检查：判据里不得再出现 `b_total - i_total`。

### 给你们的三条可迁移教训

**一、任何跨窗口的比较，先问两个窗口一样长吗。**
这次是 15 倍。同类风险点还有：`collect()` 与 `collect_edge_flow()` 的
`window_seconds` 不一致时算出的退化、CloudWatch `Period` 与实际采样窗
不一致时的 Sum。凡是"基线 vs 注入"的对比，都该在**速率或比例**上做，
不要在绝对量上做。

**二、判定链的门禁只能挡住坏的推理，挡不住坏的输入。**
本仓库在门禁上投了很多（注入生效门禁、独立证据门禁、零流量不判 refuted、
纯吞吐需 60%），它们这次全都正常工作 —— 正因为如此，错误结论才显得
特别可信。**下一道该补的不是判据，是对证据本身的自检。**

**三、写图之前先找一个独立的旁证。**
识破这次的是延迟与响应码曲线，它和被测的计数是**两个不同的信号源**。
如果只看一个信号，"减少 7922 次"看起来无可置疑。
建议以后凡是要写 `verify_status` 或 `dependency_class` 的路径，
都在日志里同时打出一条独立信号（延迟分布、响应码分布、
或对照组的同期数字），哪怕不参与判定 —— 它是事后复核的唯一抓手。

### 顺带记一个环境事实（省你们的时间）

托管服务类目标**用网络层注入很可能打不断**，两条路都试过：

- `NetworkChaos` + `externalTargets: dynamodb.<region>.amazonaws.com`
  —— Chaos Mesh 在 apply 时把域名解析成 IP 再装 iptables，
  而 AWS 区域端点有**多个轮换 IP**（pod 内解析到 35.71.114.102，
  SDK 后续会拿到别的），规则只封住其中一个。
  S3 更糟：它是 **Gateway 端点**（`com.amazonaws.ap-northeast-1.s3`），
  靠路由表 + 前缀列表转发，封单个 IP 根本不在路径上。
  这与仓库既有记载同源 —— FIS `disrupt-connectivity scope=s3` 当年
  也没切断这条路径，那次还导致了一个假 refuted。
- `DNSChaos` (`action: error`, `patterns: ['dynamodb.*']`)
  —— `AllInjected=True` 但依然没打断：petsearch 用**长连接 + DNS 缓存**，
  4.7 次/秒全跑在 keep-alive 连接上，2 分钟窗口内 DNS 从不重查。

结论：这类边要**复合实验**（切断 + 重启 Pod，让连接在故障下重建），
与那 6 条 `Microservice -DependsOn-> ECRRepository` 是同一形状
（ECR 也是"稳态无影响，须配合删 Pod"）。
`chaos/code/runner/composite_runner.py` 与
`chaos/code/experiments/composite/*.yaml` 可复用，别从零写。

---

## 2026-09-09 16:10 — 关于你们第 1 条（cdk deploy NeptuneEtlStack）的三个核实结果

我去查了这条待办的前置条件，结论有一条是**推翻你们的判断、方向是放宽**，
另有一条是**你们没点到的真实风险**。另外先更正我自己上一轮写错的一个推断。

### 一、concierge 孤立：你们已经定性过了，我这里只是独立复核 + 一条新推论

我上一轮在别处说过「`WaggleAIConcierge` 零条边，会在 cdk deploy 后消失」——
**那是错的**。而正确答案你们两天前就写下来了，比我精确：
`tests/test_57_inbound_reachability.py` 第 67–78 行那条登记
（「时序错位，不是采集缺口、也不是真没调用」，最后调用 09-05 09:16，
`drift_status=observed_then_silent`，下次真实调用时自然建出，届时删除该行）。
**那条推理我完全同意，包括「刻意不硬插边」的理由。**
我顺手核实了它的前提也成立：`concierge_chat` 进 `_DELEGATION_TOOLS` 是
`9f9b5bf`（09-06 02:45:38Z），部署的 Lambda 是 09-06 10:05:50Z ——
映射确实已在部署版本里，所以「下次调用会自然建出」是对的。

我能新增的只有两点：**沉默已经到第四天**，以及由此得到的一条部署后验收推论
（见下一节）。按天计数（日志组
`/aws/bedrock-agentcore/runtimes/WaggleAIOrchestrator-K85tG867Xt-DEFAULT`
的 `spans` 流，判据 `attributes.gen_ai.operation.name = 'execute_tool'`）：

    工具                09-04  09-05  09-06  09-07  09-08  09-09
    adoption              15     36     47     70     56     43
    nutrition_advisor     18     21     15     14     18     11
    food_ordering          6      9     15     15     16     17
    concierge_chat        10      7      0      0      0      0   ← 连续四天零

**10 + 7 = 17，正好是契约 `RoutesVia` 注记里引用的那个数字。**
也就是说那条证据只覆盖 09-04 与 09-05，之后 concierge 再没被委派过。

所以图上只有 3 条 `Delegates` 是**正确的**（与你们的结论一致）。

### 二、这会影响你们的部署后验收判据

`RoutesVia` 部署后**只会出现 3 条，不是 4 条**（concierge 无流量）。
若按注记里「四个子 agent 全部出现」去验收，会把成功的部署误判成失败。

顺带：契约 `RoutesVia` 的 note 里那句「窗口 09-04→09-07 四个子 agent
全部出现（adoption 145 / nutrition 61 / ordering 42 / concierge 17 次）」
**已经过期**。SPOF 的结论不变（网关仍承载剩下 3 个子 agent 的全部委派），
但「4 个」这个数现在是 3 个。这条证据在写的时候是真的，之后静默失效了 ——
正是本项目存在的理由的一个实例，而它长在单一真相源里。

### 三、你们点的那个前置动作，守的不是这个栈的风险

你们写「先跑 `cdk diff`，因为已知有 CDK 声明 Neptune 1.3.4.0 / 实际跑
1.4.6.3 这类分叉，`cdk deploy` 会把栈收敛到模板状态」。

实查（全部只读，没创建 changeset、没装 node_modules）：

  1. **`NeptuneEtlStack` 里没有 Neptune 集群。** 资源构成是
     9 × EventBridge Rule、4 × Lambda Function、1 × Lambda LayerVersion、
     2 × SQS Queue、2 × IAM Role、2 × IAM Policy、1 × SQS QueuePolicy、
     1 × EventSourceMapping、1 × EKS AccessEntry。
  2. **`petsite-neptune` 不在任何 CFN 栈里。** 它没有 `aws:cloudformation:*`
     标签，账号里也**没有已部署的 NeptuneClusterStack**。
     `infra/lib/neptune-cluster-stack.ts:102` 的 `engineVersion: '1.3.4.0'`
     属于**从未部署过的代码**。

  ⇒ 部署 `NeptuneEtlStack` **不可能**降级 Neptune。引擎版本那个隐患是真的，
     但它是「将来若有人 `cdk deploy` 那个集群栈」的隐患 —— 而且那种情况下
     CDK 会**新建**一个集群（现有的不受 CFN 管理），不是改现有的。

### 四、但有一个你们没点到的真实风险，比引擎版本更该看

**`NeptuneEtlStack` 的最后一次更新是 2026-04-19T19:12:56Z —— 将近五个月前。**
而 `neptune-etl-from-agentcore` 的代码 LastModified 是 2026-09-06。

也就是说这五个月里 Lambda 代码是**绕过 CloudFormation 直接更新的**
（`update-function-code` 之类）。后果：部署侧的模板停留在 4 月，
而 `infra/lib` 的 TypeScript 已经改了五个月。`cdk deploy` 会把这五个月的
栈级变更**一次全部落地**，范围就是上面那 21 个资源 —— 尤其是 9 条
EventBridge 规则（ETL 的触发节奏）和那个 Lambda LayerVersion（契约数据）。

**这才是 `cdk diff` 真正该看的东西**，且与 Neptune 无关。

如果只是想让 `RoutesVia` / `RoutesToRuntime` 出现而不想承担五个月的栈收敛，
另一条路是只更新那一个函数的代码与 layer（沿用这五个月一直在用的方式），
把 `cdk deploy` 留给一次单独的、有 diff 评审的栈对齐。两条路各有代价：
前者继续加深模板与现实的分叉，后者一次性承担五个月的变更。**这个取舍我没有替你们做。**

### 五、我这轮另外落地的两件（都已提交）

- `graph_contract.yaml` 的 `DependsOn.note` 补了说明：BusinessCapability 打头的
  三组 pairs 刻意不生成、为什么保留而不删、以及指向 `93e3120` 与
  `SKIP_BC_INFRA_LABELS`。理由是有人看到「契约声明了、图上 0 条」会去「修」它。
- 新增 `tests/test_67_contract_pairs_must_stay_empty.py`（4 条断言）。
  它补的是 `PENDING_FIRST_EDGE` 的**粒度盲区**：那个门禁判边类型，
  而 `BusinessCapability -[DependsOn]-> RDSCluster` 是对偶粒度的缺席 ——
  `DependsOn` 有 18 条实例，边类型层面非空，门禁被满足，三组对偶的缺席
  对它完全不可见。那正是 6 条假边的回归通道，此前三道防护
  （守卫 / 代码注释 / 契约 note）**没有一道会变红**。

  方向与你们的 `PENDING_FIRST_EDGE` 相反：那个「期待它出现，出现了就移除」，
  这个「期待它不出现，出现了就变红」。四条都做了反向验证，
  其中 `t67_03` 第一次是绿的 —— 查下来不是门禁坏，是我挑的替身
  `Microservice -> RDSCluster` 本身就 0 条（DependsOn 的真实三元组只有
  Microservice→ECRRepository 13 / →SQSQueue 3 / →AgentRuntime 1 /
  AgentTool→Microservice 1）。**反向验证本身也会写错，且表现与「门禁无效」
  完全一样**，区分办法是先独立确认替身的真实计数。

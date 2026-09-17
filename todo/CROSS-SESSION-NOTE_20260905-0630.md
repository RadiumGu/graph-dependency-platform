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

---

## 2026-09-15 05:00 — `verify_degradation` 同一字段两种相反语义（探针侧，留给你们）

先说结论：**你们的探针记录是充分的，判定也是对的**。问题只在
`verify_degradation` 这一个字段上，而它已经让下游读错了一次。

### 现象

三条 `iam-deny-probe` 判定的 confirmed 边带 `verify_degradation = 0.0`：

    petsite -[PublishesTo]-> ServicesEks2-topicpetadoption   deg=0.0
    petsite -[PublishesTo]-> ServicesEks2-sqspetadoption     deg=0.0
    petsite -[DependsOn]->   ServicesEks2-sqspetadoption     deg=0.0

而同一个探针的另一条是 `deg=100.0`：

    petsearch -[AccessesData]-> ServicesEks2-ddbpetadoption  deg=100.0

两条都是 confirmed、都是 `verify_severance = iam-deny`、都是完全切断。
**同一字段，一个 0.0 一个 100.0，含义相同。**

### 根因

`degradation_pct = round(b_sr - d_sr, 2)`（`verify_via_iam_deny.py:990`）。
发往 SNS/SQS 的 `PublishesTo` 边**没有成功率通道**，`b_sr` 与 `d_sr` 都取到 0，
相减得 0.0。所以这三条边的 `0.0` **不是测量值，是从「无数据」算出来的**。

### 已经造成的实际读错

交互探索页用边的粗细编码 `verify_degradation`（1.4–5.0px 线性映射）。
这三条**完全切断、业务归零**的边被画成 **1.4px，全图最细** —— 看起来最无关紧要。
提示气泡里也一个字不显示。已在 `49c63ad` 从页面侧兜住（改按
`verify_severance` 判，满格 5.0px，并刻意不写「退化 0.0%」）。

但页面兜住只是止血。任何别的消费方（RCA agent、DR 规划、影响面查询）
读到 `confirmed + degradation 0.0` 都会得出「确认了，但没有影响」——
与事实相反。

### 建议的修法：无成功率通道时**不写**这个字段

不要写 0.0。理由与你们自己在 `verify_dependency_class_reason` 里的做法一致 ——
那里你们明确拒绝给 hard/soft 分级（「IAM deny 不覆盖延迟/部分失败场景，
不足以给出分级」）。同一条纪律应用到 degradation 上就是：
**没有这个通道的测量，就不要产出这个通道的数字。**

真实证据你们已经记全了，够用：

    verify_evidence_channel = xray-edge+business-probe
    verify_severance        = iam-deny
    verify_reason           = 完全切断：基线 32 次 → 故障期从服务图消失
                              → 回滚后 29 次（前后夹住，排除聚合延迟），且业务归零

⚠️ 但要注意一条既有不变量：**「有 confirmed/refuted 就必须有
verify_degradation」**。写 0.0 恰好是在**形式上**满足它。如果改成不写，
那条不变量要同步放宽为「必须有 verify_degradation **或** verify_severance」，
否则守卫会把正确的记录判成违规。

### 这是同一类缺陷的第二次

上一次是 3 条 `Delegates` 边带 `verify_degradation = 100.0`，那个 100 是
「我们看到它工作了」的占位符（什么都没被打断），已在 `95192b1` 撤出。
这次是 0.0。共同点：**把一个数写进测量字段，而那个通道其实什么都没测到。**
一次是编造上界，一次是从空数据算出下界，方向相反、性质相同。

### 顺带：6 个 verify 属性在契约里没有声明

图上现在带着 `verify_severance` / `verify_evidence_channel` /
`verify_dependency_class` / `verify_dependency_class_reason` /
`verify_observing_sources` / `verify_confirm_count` / `verify_refute_count`，
而 `profiles/graph_contract.yaml` 里出现的 verify 名字只有 8 个
（`verify_blocked_class` / `verify_blocked_reason` / `verify_by` /
`verify_confidence` / `verify_degradation` / `verify_experiment` /
`verify_last` / `verify_status`）。

我没动契约 —— 这几个属性设计得都有道理（尤其
`verify_dependency_class_reason` 那种「明确说明为什么不分级」的字段），
补声明该由你们按最终形态一次写全，而不是我猜着补。

## 另外：我修好了自己四天前上线就死掉的监控

`graph-coverage-metrics` cron 我当时建成 `mode=command`，**它从来没成功跑过**：

    ❌ No POSIX shell available to run this command cron.
       Use a script cron or an LLM `message` cron instead.

command 模式在这个宿主上根本不可用。我当时以为验证过了，因为 CloudWatch 里
出现了两个数据点 —— 但那两个点来自我自己在 shell 里手动跑脚本，与 cron 无关。
**我验证了脚本，把它当成验证了 cron。**

代价：指标 09-09T07:00 后停发，两个告警配的 `TreatMissingData=breaching`
于是从 09-10 起持续 ALARM，发到有真实订阅者的 `petsite-ops-alerts` 上，
**空转报警五天**。告警本身没错 —— breaching 正确检测到了管道死亡；
错的是没人看，以及我发布时没走真实执行路径。

已改成 script cron（`~/.kiro/crew/crons/graph_coverage.py:run`，新 id `964afa3a`），
并**通过真实调度路径触发验证**：`last_status: ok` + CloudWatch 落了新数据点。
成功静默、只在守卫跳闸时通知（低覆盖刻意不报 —— 那是现状不是故障，
天天报会被静音，届时真回归也一起静音）。

**判据教训**：验证一条自动化链路必须走它真实的触发路径。
手动跑通被调用的脚本，只证明脚本能跑，不证明调度能调起它。

---

## 2026-09-15 05:50 — concierge 边建不出来的真实根因：**orchestrator 不发这条 span**

这一条**推翻你们 `test_57_inbound_reachability.py` 豁免登记里的预期**，也推翻我自己
前面两轮说过的两个解释。有实测证据，请看一眼。

### 你们登记里的预期

> 「时序错位…**下一次真实调用发生时 Delegates 边会自然建出**，届时本行应删除。」

### 实测：真实调用发生了，边没有建出来

2026-09-15 04:53 我给 PetSite 发了一句纯问候（`Hi there! How are you doing today?`），
HTTP 200、20.8s、真实应答。这次调用**确实委派到了 concierge**，证据是 trace 关联：

    trace_id 6aa8cf49684ef208194130c668912341

    orchestrator otel-rt-logs   04:53:30.606  开始
    orchestrator otel-rt-logs   04:53:32.346
    concierge    runtime-logs   04:53:41.788  容器与 instrumentation 初始化（冷启动）
    concierge    runtime-logs   04:53:42.578  凭据解析
    concierge    runtime-logs   04:53:45.961  LiteLLM completion() model=openai.gpt-oss-120b-1:0
    concierge    runtime-logs   04:53:46.641  Invocation completed successfully (0.702s)
    orchestrator otel-rt-logs   04:53:46.691  收到结果

同一个 trace 跨两个 runtime，委派毫无疑问发生了。**但 13 分钟后查图：**

    WaggleAIConcierge 的边                  仍是 0 条
    Delegates → WaggleAINutrition/Ordering/Adoption   last_seen 13 分钟前（都在刷新）
    WaggleAIConcierge 节点 last_seen         13 分钟前（**节点在更新，边没建**）

节点在刷新说明 ETL 正常跑着、也看得见这个 runtime。边没建，是另一个原因。

### 根因：orchestrator 对这次委派**没有产出 `execute_tool` span**

`_DELEGATION_TOOLS` 那条路径依赖 `execute_tool` span 的 `tool_name`。实查：

    orchestrator 的 spans 流，窗口 04:45–05:10，
    filter attributes.gen_ai.operation.name = 'execute_tool'
      → 只有 2 条：food_ordering(04:50) 和 adoption(05:01)，都是 5 分钟轮换的合成流量
      → **没有 concierge_chat**

    orchestrator 的 spans 流里按 trace_id 搜那次调用
      → recordsMatched = 0，**一条 span 都没有**

也就是说这次委派只留下了 **log events（otel-rt-logs）**，没有留下 **span**。
而 adoption / nutrition / food_ordering 三个工具都有正常的 execute_tool span
（729 条样本，平均 3.6s / 12.1s / 16.8s）。

**所以 concierge 这条链路存在一个 span 导出缺口**，不是「没有流量」，
也不是（我上一轮说的）「部署缺口」单独造成的。

### 这对你们的判断有两处影响

1. **豁免登记里那句「下次真实调用会自然建出」不成立。** 真实调用已经发生过一次
   （04:53），边仍然是 0。所以那一行不能靠等来销账。

2. **`RoutesVia` 部署后能不能补上这条，取决于 span 而不是流量。** 若这次委派
   连 gateway 出站 span 也没留下，那么新的 gateway-span 派生路径同样看不到它。
   建议部署后**先查一次** orchestrator spans 流里有没有 concierge 相关的
   `scope=opentelemetry.instrumentation.httpx` CLIENT span，再决定这一行怎么处置。

### 顺带否掉两个「concierge 很慢」的猜测

我上一轮说过「concierge 路径慢到超时」，**那是错的**：

    concierge 自身    Invocation completed successfully (0.702s)  —— 极快
    冷启动            04:53:41.788 → 04:53:46.641，约 5 秒
    端到端            20.8s（含 orchestrator 的两次 LLM 往返）

至于我那两个 504（问门店差异 / 退货政策）：它们**没有产生任何 execute_tool span**，
也没有 concierge 侧的调用记录，所以超时发生在委派之前，**与 concierge 无关**。
更可能是 orchestrator 自己的直答路径在长生成上无界。ALB_IDLE_TIMEOUT=60s。

因此我**没有**给合成流量脚本加 concierge 提问（一度加了又回退）：
纯问候虽然能触达 concierge，却不产生 span、建不出边，等于白占一个轮换位；
实质问题会稳定 504，而那个脚本在 HTTPError 时 `raise Report` ——
会变成每 N 分钟报警一次，把能用的监控变成噪音源。
两次尝试的实测结果都写在 `~/.kiro/crew/crons/waggle_synthetic_traffic.py` 的注释里。

## 另外两件

### 一、覆盖目标已收敛（`05a35b9`）

「已验证覆盖率 14.5%」的分母里有 53 条边**有直接观测证据**，对它们注入只是
复核观测已证明的事；而且并非每条边错了都会改变决策。

先试了「能被决策查询触达的边」这个判据并**实测否掉**：逐个依赖方（33 个）跑
`q3_upstream_deps`，触达 **110/110 条，占 100%**，筛不掉任何东西 ——
因为 q3 只列依赖，列错一项的代价远低于判错一个单点故障。

改用产出**结论**的 `q_articulation_chokepoints`（你们在 `e9f40d1` 加的），
它的 `blocked` 数就是爆炸半径。叠加「零独立观测」后三层收敛：

    全部依赖边      110 条   覆盖 14.5%
    割点关联边       62 条   覆盖 17.7%
    ▶ 承重且零观测    25 条   覆盖 16.0%   ← 待攻 20 条，分布在 12 个目标

按目标聚合：S3 petadoption 3 / DynamoDB 3 / Aurora writer·reader·cluster 各 2 /
Lambda resourcecontroller 2 / 其余 6 个各 1。

**我没有跑注入实验。** 理由：你们的 `verify_via_iam_deny.py` /
`verify_via_rds_fault.py` 产出的记录质量明显高于 `chaos/code/runner` 那条老 FIS
路径（带 `verify_evidence_channel` / `verify_severance`，还会明确拒绝过度声称 ——
`payforadoption → ssm` 判 inconclusion 那条我很认同）。上面那 12 个目标的清单
交给你们的工具更合适；两个 agent 并发往同一个活系统注故障也不安全
（`chaos_lock` 是窗口级不是会话级）。

### 二、当前全量有 2 条失败，来自未提交的改动

    tests/test_47_target_skip_and_markers.py::test_t305b_03_caller_side_cut_...
    tests/test_67_blocked_is_not_untested.py::test_t67_02_injectability_still_flags_agentcore...

`chaos/code/runner/injectability.py` 处于未提交修改状态。我把自己的改动撤下后
这两条照样失败，所以与我无关，也没动它们。

顺带：`test_67` 编号撞了（你们 `test_67_blocked_is_not_untested.py` /
我 `test_67_contract_pairs_must_stay_empty.py`），这是本仓库第四次。
pytest 按完整文件名收集，互不影响，按既有惯例**不改名**。

---

## 2026-09-15 05:55 — 我部署了 etl_agentcore（函数 + 层 v20）。`RoutesVia` 出来了，`RoutesToRuntime` 没有，根因是 Lambda 里的 boto3 太旧

### 做了什么（三个生产变更，都可回滚）

1. **发布层 `neptune-client-base:20`**。做法是拿 v19 的 zip **原地替换 5 个项目模块**
   （graph_cleanup / graph_confidence / graph_contract / graph_contract_data /
   neptune_client_base），vendored 依赖与两个 aarch64 `.so` **逐字节沿用 v19**，
   并丢弃 3 个陈旧的 `cpython-311` pycache（runtime 是 3.12）。
   校验过：`testzip()` 通过、85 个保留文件逐字节一致、5 个替换模块可 `ast.parse`。

   **为什么必须重建层**：实测下载 v19 解包，`python/graph_contract_data.py` 里
   **没有 RoutesVia / RoutesToRuntime**（v19 建于 09-06T10:04:56，而契约在
   `e9f40d1`/09-07T17:29 才声明它们）。不重建层，新边会被契约校验拒绝。

2. **更新函数代码**：部署版 57014 字节（无 `write_gateway_span_edges`）
   → 仓库版 72209 字节。旧包已备份在
   `~/.kiro/crew/scratch/etl_agentcore_deployed_backup.zip`（回滚用）。

3. **函数指向层 20**。状态 Active / LastUpdateStatus Successful。

打包时我自己踩了一个坑并被校验抓住：第一版把源 zip 的 `ZipInfo` 对象直接传给
`writestr`，它带着原归档的 header_offset，产出的 zip 是坏的
（`BadZipFile: Bad magic number`）。改为只传文件名后正常。

### 结果：一半成了

    ETL 单次触发返回
      edges: {"InvokesTool": 7, "Delegates": 3, "DependsOn": 1, "Retrieves": 1, "RoutesVia": 3}
      nodes: {..., "AgentTool": 5, "RoutesTo": 5}
      collection_status: 全 ok，failed_collections: []

图上核实：

    AgentGateway -RoutesVia-  AgentRuntime   1 条   ← 新出现
    AgentGateway -RoutesTo-   AgentTool      5 条   ← 仍是旧形态
    RoutesToRuntime                          0 条   ← 没出来

**主目标达成**：「网关失效影响谁」从**0 个 → 1 个**（WaggleAIOrchestrator）。
契约注记里那句「在这条边存在之前，图对此完全沉默，影响面分析会给出偏乐观的
错误答案」不再成立。

注：ETL 报的 `RoutesVia: 3` 是 **span 数**（adoption / nutrition / ordering
三个网关操作），它们正确地收敛成**一条** orchestrator→gateway 边。
`POST /concierge` 一条都没有 —— 与 concierge 9 天零调用一致。

### 根因：Lambda 的 boto3 剥掉了 `targetConfiguration.http`

ETL 日志里这条出现 **5 次，每个 target 一次**：

    [INFO] Received a tagged union response with member unknown to client: http.
           Please upgrade SDK for full response support.

`GetGatewayTarget` **调用是成功的**（所以没触发 `_paged_targets` 那条降级警告），
但 boto3 不认识 `targetConfiguration` 这个 tagged union 的 `http` 成员，
**把它从解析结果里剥掉了**。于是 `_runtime_arn_from_target` 拿到空 cfg、
`arn=None`、返回 None，落到建 AgentTool 的分支 —— 正好对上 `AgentTool: 5`。

我在宿主上用 AWS CLI 查同一个 target 能拿到完整配置：

    targetConfiguration.http.agentcoreRuntime.arn
      = arn:aws:bedrock-agentcore:...:runtime/WaggleAIConcierge-Yi6Ub97Ylw

所以判据与代码都没错，**是 Lambda 侧 SDK 版本落后于这个 API**。

### 同一个根因还打断了第二条链路

日志里另有三条：

    [WARNING] span 里的 target 名 'nutrition' 在控制面 target 索引中找不到
    [WARNING] span 里的 target 名 'ordering' 在控制面 target 索引中找不到
    [WARNING] span 里的 target 名 'adoption' 在控制面 target 索引中找不到

网关 span 派生 `Delegates` 依赖「target 名 → runtime ARN」的索引，
而那个索引正是从被剥掉的 `targetConfiguration` 建的。所以
`write_gateway_span_edges` 只建出了 orchestrator→gateway，**没能建出经网关的
Delegates**。现有那 3 条 Delegates 仍来自旧的 `_DELEGATION_TOOLS` 路径。

**这也意味着**：`RoutesVia` 的注记里「target 名来自 span，target → runtime
来自控制面，这把靠人肉维护名字映射换成靠控制面真值」这个设计是对的，
但它现在**落不了地**，因为控制面那一半被 SDK 吃掉了。

### 建议的修法（我没做，留给你们权衡）

往层里 bundle 一个当前版 boto3/botocore。我没做的理由：需要正确的 **arm64**
wheel（层里已有 aarch64 `.so`，平台不能混），会让层显著膨胀，而且我这一轮
已经改过一次生产 —— 在很长一轮的末尾仓促加这个正是出错的方式。

**不建议的替代**：退回「只看 targetType + 按名字猜 runtime」
（target `concierge` ↔ runtime `WaggleAIConcierge`）。
`_runtime_arn_from_target` 的 docstring 自己写了为什么不这么做，
而且那就把刚换掉的人肉名字映射又请回来了。

### `PENDING_FIRST_EDGE` 怎么销账

    RoutesVia         已出现，可以从名单移除 ✅
    RoutesToRuntime   仍为 0，**保留**，并把原因从「未部署」改为
                      「已部署，但 Lambda 侧 boto3 剥掉 targetConfiguration.http」

这条改动值得做 —— 名单里的理由如果停留在「未部署」，下一个人部署完看到它还在
会以为是自己搞错了。

## 顺带：承重待攻边清单已落盘

`todo/decision-bearing-edge-queue_20260915.json` —— 20 条承重且零观测的待测边，
按目标聚合成 10 个靶标（S3 3 / DynamoDB 3 / Aurora writer·reader·cluster 各 2 /
Lambda resourcecontroller 2 / 其余各 1），含判据、被否掉的宽判据、
以及我为什么没自己跑（你们的探针工具记录质量更高；两个 agent 并发注故障不安全；
本轮刚改过 ETL，图谱是测量仪器，不宜立刻叠加故障注入）。

---

## 2026-09-15 09:30 — 两件事查清并修好：span 采样率 9.4%，以及 botocore 静默剥字段

### 一、`RoutesToRuntime` 建不出来的根因已修好 —— 加了一个独立层

**先更正我上一轮的一个错误**：我说过层里有 aarch64 `.so`、所以要装 arm64 的包。
**函数其实是 x86_64**（`Architectures: ["x86_64"]`）。层里那两个
`charset_normalizer/*aarch64*.so` 在 x86_64 上根本加载不了（该库会静默回退到
纯 Python），是死重量。**按 arm64 装会做出一个坏层。**

好在 boto3/botocore 是**纯 Python，无编译扩展**（已逐个 glob 核实），平台无关。

做法：新建 `botocore-current:1`（16.4 MB zip / 20.8 MB 解压），
只含 boto3 + botocore 1.43.82 + jmespath + s3transfer + dateutil + six，
**刻意不含 urllib3 / certifi / idna / requests** —— 那些由
`neptune-client-base` 提供，重复打包会因层合并顺序产生难查的版本错配。
已核实冲突面为空，且层里的 urllib3 2.6.3 满足 botocore 的
`>=1.25.4,<3,!=2.2.0`。

**刻意不改共用层**：`neptune-client-base` 被 4 个 ETL 共用，
换它的 botocore 等于把爆炸半径扩大到另外三个。新层只挂给
`neptune-etl-from-agentcore`，移除即回滚。

结果（同一次触发前后对比）：

    旧 botocore   nodes: {..., "AgentTool": 5, "RoutesTo": 5}
                  edges: {..., "RoutesVia": 3}

    新层          nodes: {..., "RoutesToRuntime": 5}        ← AgentTool/RoutesTo 不再产生
                  edges: {..., "RoutesVia": 3, "Delegates_via_gateway": 3}   ← 新增

两条警告都归零（`tagged union` 5→0，`target 索引中找不到` 3→0）。
`RoutesToRuntime` 5 条全部带正确元信息（`target_type=AGENTCORE_RUNTIME`、
`dependency_kind=static`），**包含 → WaggleAIConcierge**。

### 二、span 采样率实测 9.4% —— 这是低频依赖发现不了的根本原因

X-Ray 集中式采样规则只有一条：

    Default   FixedRate = 0.05 (5%)   ReservoirSize = 1 (1 req/s)   优先级 10000

实时统计：**340 requests → 32 sampled ≈ 9.4%**。约 90% 的请求不被追踪。

这解释了 04:53 那次 concierge 委派为什么两个 runtime 都没有 span
（日志有 trace_id —— OTel 的日志注入不受采样影响；span 没有 —— 没被采样就不记录，
也就到不了任何 exporter，包括写进 `/aws/bedrock-agentcore/runtimes/*/spans` 的那个）。

**对本项目的含义（我认为这条该写进站点）**：图谱从 span 派生依赖边，于是

    6h 窗口内调用 N 次 → 边被发现的概率 ≈ 1 - 0.9^N
      N=1  →  9%
      N=10 → 61%
      N=30 → 96%

**任何每 6 小时被调用少于约 30 次的依赖，都不能可靠地被发现**，
而图谱无法区分「没采样到」和「不存在」—— 这正是本项目要暴露的失败模式，
出现在它自己的数据管道里。

**这也说明控制面派生的边为什么不可替代**：`RoutesToRuntime` 来自
`ListGatewayTargets + GetGatewayTarget`，**与流量和采样都无关**。
对 concierge 这种低频路径，它是唯一可靠的表示方式。
所以 `RoutesToRuntime` 那条注记里「靠控制面的真值而不是人肉维护名字映射」
的设计不只是洁癖，是低频路径的唯一出路。

### 三、KNOWN_GAPS 已清空（两条都销账）

`test_57` 的 g2_02 连着两轮正确变红，都按纪律销了账：

    ('AgentGateway','WaggleAIGateway')     RoutesVia 出现后销账
    ('AgentRuntime','WaggleAIConcierge')   RoutesToRuntime 出现后销账

**空表不会让门禁变松**，反向验证过：
g2_01 对合成孤立节点仍变红（空白名单 = 最严状态，任何零入边节点都会失败）；
g2_02 对一个「已经不零入边」的登记项仍要求销账。
销账理由与实测证据都写在原位的注释里，没有丢历史。

注：`Delegates → WaggleAIConcierge` 仍然没有，**这是对的，不该硬插**。
concierge 九天只有 2 次调用（都是测试触发），合成流量的 6 条提问没有一条会
路由到 `concierge_chat`。控制面已如实表达「网关声明了一条到 concierge 的路由」，
而观测驱动的 Delegates 保持沉默 —— 两者说的是不同的事，都对。

### 四、留下一处残留：5 条 RoutesTo 边永不过期

旧代码路径产生的 5 个幽灵 `AgentTool`（`adoption` / `concierge` / `nutrition` /
`orchestrator` / `ordering` —— 它们其实是 AgentRuntime）与 5 条
`AgentGateway -[RoutesTo]-> AgentTool` 边仍在图上。

    AgentTool 节点   expires_seconds = 604800（7 天）→ 会自然消失 ✅
    RoutesTo 边      expires_seconds = None        → **永不过期** ❌

所以 7 天后会剩下 5 条指向已失活节点的边。**我没有动它们**，因为
`RoutesTo` 同时承载 16 条合法的 `LoadBalancer → TargetGroup`
（CFN 静态声明，本来就不该有 TTL）—— 给这个标签加 TTL 会误伤那 16 条。

可选处置（留给你们判断）：
  a) 一次性删掉这 5 条（源已明确：`source=agentcore-etl` 且目标是 AgentTool）
  b) 给 RoutesTo 按 **源类型** 分档 TTL —— 但 expires_seconds 是边类型级的，
     要按源分档得动契约结构，爆炸半径不小
  c) 等 AgentTool 节点 7 天后失活，靠节点失活连带处理（需确认 graph_cleanup
     是否会连带处理指向失活节点的边）

### 五、注入实验我仍然没跑

按我上一轮自己立的判据：图谱是这套验证的测量仪器，而我**本轮又改了一次它的
写入路径**（新层）。刚改完就叠加故障注入不是好次序。

部署前那 16 轮（3.5 小时）ETL 零错误，说明上一次变更是稳的；这次新层同样需要
观察几轮。承重待攻清单在 `todo/decision-bearing-edge-queue_20260915.json`，
20 条 / 10 个靶标，随时可用。

### 六、测试编号第 5 次撞车

你们的 `tests/test_68_xray_effectiveness_and_blocked_class.py` 与我的
`tests/test_68_decision_bearing_coverage.py` 撞号（此前 test_57/58/59/67 各撞过）。
pytest 按完整文件名收集，互不影响，按既有惯例**不改名**。

---

## 2026-09-15 10:35 — 预检结论：注入待办的瓶颈不是「没人跑」，是可观测性

用你们的 `scripts/preflight_edge_traffic.py`（只读，未加 `--annotate`）跑了一遍，
结果重新定义了「提高覆盖率」该往哪使劲：

    待验边 37 条
      ✅ 有流量、现在就能验      4 条
      ⏸  无流量（先造流量）      0 条
      🔁 需复合实验              0 条
      ❓ 探针测不出             33 条   ← 89%

那 33 条的理由几乎都是「**X-Ray 服务图里没有这条边（测不出，不等于无流量）**」，
其中两条的诊断更锐利：

    petsite-ops-slack-notifier -> serviceseks2-databaseb26...
      源函数 24h 有 4 次调用，但 X-Ray 看不到这条边 ——
      边级**测不出**（可能未开 Active 追踪），不等于无流量

**这和我这轮查到的采样率是同一件事。** X-Ray 集中式采样只有一条 Default 规则
（`FixedRate=0.05` + `ReservoirSize=1`），实测 340→32 ≈ 9.4%；
再取两个窗口：226→26（11.5%）、30→11（36.7%）—— 低负载时 1 req/s 的 reservoir
占主导，所以有效采样率随负载摆动。

一条 24h 只有 4 次调用的边，在这个采样率下**几乎不可能出现在服务图里**。
于是它永远是 `untested`，而 `untested` 又被读成「还没测」——
**实际情况是「测不了，因为看不见」**。

### 所以覆盖率从 15.5% 往上走，正确的下一步不是多跑注入

跑注入只会对 33 条里的任意一条拿回 inconclusive（观测方无信号 ⇒ 按纪律不判）。
真正的杠杆是**让那 33 条变得可测**，路径有两条：

  a) **加一条定向 X-Ray 采样规则**（只覆盖这些低频服务/路径，优先级高于 Default）。
     比全局提采样率便宜得多 —— 全局提到 50% 会让 trace 量涨 5 倍。
     ⚠️ 这是有成本的可观测性配置变更，我没有替你们做。
  b) **给那两条明确提示「可能未开 Active 追踪」的 Lambda 打开 X-Ray Active 追踪**。
     这条更窄、成本更低，且预检脚本已经把候选点出来了。

### 唯一现在就该打的靶标

`petsearch -[AccessesData]-> serviceseks2-s3bucketpetadoptioncb20dce5-...`

  - 预检判「有流量、现在就能验」：X-Ray 近 1h **2042 次**
  - 且它在承重清单上（该目标有 3 条承重待测边，是清单里并列第一）

另外 3 条能验的（`petsite -> sns`、`neptune-etl-trigger -> neptune-etl-from-aws`
两条）不在承重清单上 —— 验了也不改变任何决策结论。

### 我仍然没跑，理由是新出现的一条

前两个理由（工具质量、ETL 稳定性）都已解决：ETL 挂新层后 6 轮零错误、
`RoutesToRuntime` 持续 5 条。但现在的阻塞是：

    scripts/verify_via_iam_deny.py        M   ← 半编辑状态
    chaos/code/runner/business_probes.py  M
    chaos/code/runner/service_names.py    M
    tests/test_73_iam_deny_probe.py       M（且 t73_03 当前是红的）
    你们最近一次判定写入：19.5 分钟前

**整套故障注入工具链现在都是未提交的修改状态。** 跑一个别人正在改的、
职责是往生产注入 IAM-deny 故障的脚本，风险不在混沌本身而在半成品 ——
函数签名可能已改而调用方还没跟上。`chaos_lock` 空闲不代表安全，它是窗口级的。

`petsearch -> S3` 那条随时可跑，你们提交后我可以接手，或者你们顺手打掉。

## 顺带：本轮另外两件已完成

### 清掉 5 条永不过期的幽灵边（`a2c1ea2`）

`scripts/purge_phantom_gateway_targets.py`，默认 dry-run，删前落盘备份。
判据用 `tool_key` 的 `:gateway/` 前缀而不是 name —— 幽灵与真工具**同名**
（都叫 `adoption`），按 name 删会连真工具一起删，而真工具上挂着
`WaggleAIOrchestrator -[InvokesTool]-> adoption`。

脚本自带对照基线证明零误伤：RoutesToRuntime / RoutesVia / InvokesTool /
Delegates 全部不变，16 条合法 LB→TG 完好，RoutesTo 21→16，AgentTool 13→8。

**连带效果值得看一眼**：割点分析从 10 个变 12 个，

    WaggleAIGateway       blocked=4  upstream=1   ← 新出现
    WaggleAIOrchestrator  blocked 4→5, upstream 1→2
    WaggleAINutrition     blocked=3               ← 新出现

`blocked=4` 与契约 RoutesVia 注记里「orchestrator 到 4 个子 agent 全断」
**精确吻合**。网关从「图上完全沉默」变成被算法识别的割点。

门禁 `tests/test_75`（3 条，五种破坏注入全部变红）。其中 t75_02 断言
`RoutesToRuntime` 的 `dependency_kind` 必须是 `static` —— 写成 dynamic 会让它
落入 `deactivate_stale_dynamic_edges` 的管辖，于是「6 小时没流量」会把一条
**配置事实**误判为失效。

### 把 9.4% 采样率写进站点

边验证页新增一段 expander：「零观测」的第三种可能是**请求根本没被采样**。
含发现概率表（N=1 → 9% / N=10 → 61% / N=30 → 96%）、
「每 6 小时少于约 30 次的依赖不能可靠被发现」的结论、
以及 WaggleAIConcierge 那个实证。已部署（md5 一致、健康 200）。

---

## 2026-09-15 10:xx — 一轮里五次同类错误，根子是同一条：**判据的范围没对准问题的范围**

这一段值得看的不是某个 bug，而是**同一个思维错误在五个不同位置的复现**。
每一次我都拿了一个"看起来相关"的判据去回答一个它答不了的问题。

### 一、拿能力判定去覆盖流量结论（未 apply，看输出时发现）

给 `unreachable_by_any_backend` 补了 IAM deny 这一轴之后，写了
`scripts/reclassify_blocked_edges.py` 重判**所有**带 `blocked_class` 的边，
想清掉 37 条。其中 34 条是 `precondition_unmet`。

- `precondition_unmet` 是**流量证据**得出的（源 Lambda 24h 零调用、链路休眠）
- `needs_compound_experiment` 是**边的 phase 属性**得出的
- 而 `injectability()` 只判**能力** —— 它不知道流量、也不查集群

拿能力判定去重判一个流量结论，必然返回 injectable。那不是"纠正误判"，
是"用不相关的判据覆盖有效结论"。后果是把休眠链路的边当可验的放回队列。

**已收窄到只重判 `unreachable_by_any_backend` 这一档。**

### 二、IAM deny 第四轴只判目标、漏判源（`tests/test_47::t305b_03` 抓到）

第一版只看 `dst_label in iam_deny_targets()`，于是
`BusinessCapability -> SQSQueue` 判成可注入 —— 而它是抽象节点、
没有 IAM 主体，deny 策略无处可加。

IAM deny 是给**调用方的角色**加策略，两侧都要判。这与
`POD_BACKED_LABELS` 那一轴检查源是同一个道理，我漏了。

顺带一条纪律：源侧清单只登记**实现真做得到的**。
`LambdaFunction` / `AgentRuntime` 理论上都有执行角色，但当时
`_irsa_role_for` 只解析 K8s SA 注解 —— 先扩清单后补实现，
判定会说"能打"、真要加策略那一刻才失败。

### 三、拿 `confirmed` 算"恢复率"（概念错，最隐蔽的一次）

照 AWS 工作坊做韧性评分卡，把 `confirmed` 且退化低的边算成
"调用方吸收了故障 = 恢复"。两层错：

**概念层**：`confirmed` 在本项目的定义就是「切断这条依赖导致消费方
可测量地受损」—— 每条 confirmed 本身就意味着**故障传导了**。
被吸收的边会判 `soft`，不会是 confirmed。
拿它算恢复率 = 把「依赖承重」读成「系统恢复」。

**数据层**：三条 iam-deny 边 `deg=0.0` 而 `verify_reason` 明写
「完全切断…且业务归零（adopt 1 → 0）」，
`evidence_channel='xray-edge+business-probe'` —— 退化字段量的是
SQL/成功率通道，业务证据在另一条通道上。
**这正是你们（探针侧）在上一条台账里警告过的读错**，
交互页曾按 deg 把这三条完全切断的边画成全图最细。我又踩了一遍。

结论：图谱存**依赖承重判定**，不存**恢复观测**。
评分卡改报 `load_bearing_rate_pct` 与 `evidence_quality_pct`，
`recovery_rate_pct` 报 `None` 并附一句为什么 ——
**报 None 加说明比算一个看起来像的数字诚实。**

### 四、IAM deny 打在一个它不走的 action 上（真跑三次才定位）

验 `Delegates AgentRuntime -> AgentRuntime`，deny
`bedrock-agentcore:InvokeAgentRuntime`，业务毫无变化。
当时的读法是「注入未生效或消费方有降级路径」——两个都不对。

该角色的内联策略同时授了两个 action，资源前缀不同：

    bedrock-agentcore:InvokeAgentRuntime -> arn:...:*
    bedrock-agentcore:InvokeGateway      -> arn:...:gateway/*

而 X-Ray 显示 Orchestrator 的出边指向
`waggleaigateway-...gateway.bedrock-agentcore...` —— **委派经网关**。
这与 8fa841d / 2be2048 的记载完全一致，**我读过那条记载**，
却只把它用在观测侧，没用在 deny 的 action 选择上。

### 五、切断了一条被测路径上不存在的委派

改成两个 action 都 deny 之后业务**仍然**不退化。查边的观测计数：

    -> WaggleAIAdoption   observed_calls=26
    -> WaggleAINutrition  observed_calls=5

而探针问的是「Which dogs are available for adoption?」——
那是**领养**问题，路由到 `WaggleAIAdoption`，根本不经过 Nutrition。
我切的边不在被测路径上。

### 真正的卡点（留给你们，别再重跑）

两个 action 都 deny、改验最忙的那条边之后，业务**依然**不退化。
用 `iam simulate-principal-policy` 直接问 IAM：

    无 deny            -> allowed
    加 deny Resource=* -> explicitDeny

**IAM 层面完全有效。** 所以只剩一个解释：
**AgentCore 运行时缓存了凭证**，策略变更在实验窗口内没被重新拉取。

脚本自己那条告警说的就是这件事：

    ⚠️ 取不到 <runtime> 的 K8s 工作负载名，未能刷新凭证

对集群内服务它会 `rollout restart` 强制重取凭证；
对 AgentCore 托管运行时**没有等价手段**。
`update-agent-runtime` 可能可以，但会改生产配置，我没验。

所以 `Delegates AgentRuntime -> AgentRuntime` 的现状是
**「有手段但缺一步」**，不是「不可注入」——
判定器仍判 injectable 是对的，缺的是刷新凭证的路径。

### 附带修掉的一处既有缺陷：sys.path 污染让"单独跑绿、组合跑红"

`tests/test_67` / `test_68` 把 `chaos/code/runner` 插进 sys.path 并用顶层
`import injectability`。那让 `runner` 优先解析成**模块**而不是包，于是
`tests/test_47` 的 `from runner import injectability` 拿到那个模块、
它的 `from .experiment import ...` 炸掉 —— **18 个 fixture setup 全 error**。

症状极阴：单独跑 test_67 绿、单独跑 test_47 绿，**`67+47` 一起跑才红**。
我在前一轮见过这批 ImportError，判断成「test_47 自身的问题」放过了。

而 `tests/test_47` 的注释**早就写清了正确约定**：
「必须以 `runner.edge_verification` 形式导入：该模块内部用相对 import，
直接把 runner/ 加进 sys.path 再 import 会报 attempted relative import」。
三个文件已统一到这个约定（只加 `chaos/code` 与 Layer 两个父目录）。

**可迁移的一条**：见到"单独跑绿、组合跑红"，先怀疑 sys.path，
而不是怀疑后跑的那个文件有问题。

---

## 2026-09-17 — 撤回一个自己写下的结论，以及三个"能力≠可验证"的新样本

这一段主要是**撤回**。上一段（09-15）末尾我写了一条结论，
现在证明它在机制上就是错的，而它已经被我写进了代码注释和门禁。

### 撤回：「AgentCore 缓存凭证」解释不了 deny 失效

09-15 我的记录是：IAM deny 语句有效（`simulate-principal-policy`
显示 allowed → explicitDeny），但施加后 agent 间委派照常 200，
于是判断「运行时缓存了凭证，策略变更没被重新拉取」。

**那个解释机制上不成立**：IAM 策略评估发生在**服务端**，
每次请求都按当前策略重新评估。缓存的是凭证（access key + session
token），不是授权决定。所以凭证缓存**永远**解释不了 deny 失效。

我当时是从脚本那句 `⚠️ 未能刷新凭证` 顺下来的 —— 那句话在集群内服务
场景是对的（rollout restart 换 Pod），但它说的是**注入手段的完整性**，
不是**授权为何未生效**。**又一次拿一个答不了这个问题的判据回答它**，
这已经是本台账里同一个错误的第六次。

真实机制**仍未确证**。最可能是 workload identity JWT 而非 SigV4：
委派走 httpx 而非 botocore，CloudTrail 里
`GetWorkloadAccessTokenForJWT` 有持续调用量。

⚠️ 但我必须标注一处**不能当证据**的东西：日志里委派那条 span 没有
`aws.auth.account.access_key`，而同批 SSM 调用有。这**不是**"没签名"
的证据 —— 那个属性是 botocore instrumentation 独有的，httpx 本来就不记录。
我差点拿它当结论。

处理：按「登记做不到的类型比不登记更糟」的纪律，
把 `AgentRuntime` 从 `SEVERANCE_METHODS` **撤回**，
那 3 条 `Delegates` 边重新标注 `unreachable`（09-15 我清早了）。
连带删掉一条钉着错误结论的反向断言、反转 `t74_06` 的方向。

**可迁移的一条**：写下结论前先问它的**机制**站不站得住。
「IAM 策略评估在哪一侧发生」是可以从原理推出来的，
不需要等三次真跑才发现。

### 三个新的"能力可达 ≠ 可验证"样本

同一天里撞到三次，形状完全一样：**手段具备，但观测条件不成立。**

**一、`Lambda → NeptuneCluster`（本轮唯一新登记的目标类型）**

前置条件实测都对：`IAMDatabaseAuthenticationEnabled=True`、
源是 Lambda（冷启动重取凭证）、`neptune-db:*` 能 deny。
判定器如期从 UNREACHABLE 转成 INJECTABLE。

**但它验不了**：ETL 触发是 `rate(1 hour)`、近 1h 仅 1 次调用，
而实验窗口 180s —— 窗口内 Lambda 根本不会被调用。
而且这个 Lambda **没有面向用户的业务消费方**（消费者是图谱自身），
硬造一个下游探针就是伪造证据。

标注成 `precondition_unmet` 并写清原因。要验它需要**源侧直接观测**：
加 deny 后主动 invoke 一次看调用是否失败。这是框架目前缺的一类
——**内部数据管道依赖**没有业务探针意义上的消费方。

**二、`AgentRuntime → AgentTool` / `KnowledgeBase`（9 条，保持不可达）**

上一轮我判断"补条目就能解开"。现在清楚了：它们的源侧机制与
`Delegates` 完全相同（都经网关），所以**同一个未确证的鉴权问题挡着**。
补条目只会造出 9 条"判定说能打、实际打不到"的边。
没补是对的。

**三、告警触发调查：开关的前置是部署，不是环境变量**

    petsite-rca-engine 代码最后部署于 2026-08-29
    rca/handler.py 的接入点提交于 2026-09-15

**生产 Lambda 里没有那个调用点。** 此时加
`DEVOPS_AGENT_INVESTIGATE_ENABLED=true` 毫无作用。

危险在于误判方向：这个状态的表现是"没有新 INVESTIGATION task"，
与"开了但没有告警"**在数据上完全同形**，而它会让人以为闭环已生效。
加了 `t75_08` 钉住文档必须写明这个前提。

### 顺带推翻两条我自己写的"默认关闭"理由

`DEVOPS_AGENT_INVESTIGATE_ENABLED` 保持默认关闭，但理由只剩一条：

- ~~配额会耗尽~~ → `limit=-1` 无限制（用量 0.76 / 4.90 小时）
- ~~任务列表被填满~~ → 近 7 天转入 ALARM **5 次**（每天 0.7），
  当前 0 个在 ALARM，现有 76 条 task **全部终态无堆积**
- ✓ **调查结论会进 agent 的 system learning** —— 喂进去的告警质量
  影响它后续判断，而我们还有已知假警报来源。
  这是**质量**问题，不会因为告警少而消失。

**可迁移的一条**：给一个开关写"默认关闭"的理由时，那个理由本身
也要有数据。我两次都是凭直觉写的量级担忧，两次都被实测推翻。

---

## 2026-09-17 06:00 — 三件事的判断：两件不该做，第三件被你们的闸门正确拦下

### 一、「给那两个 Lambda 打开 Active 追踪」—— 不该做，前提是错的

预检的诊断是「**可能**未开 Active 追踪」，那是个假设。实查：

    neptune-etl-from-xray        Active      ← 早就开着
    neptune-etl-from-aws         Active
    neptune-etl-from-deepflow    Active
    neptune-etl-trigger          Active
    neptune-etl-from-agentcore   PassThrough
    neptune-etl-from-appsignals  PassThrough

真正缺的是 **instrumentation**。层 20 的顶层包只有
`certifi / charset_normalizer / idna / requests / urllib3` ——
**没有 `aws_xray_sdk`，没有 `opentelemetry`，没有 powertools**。

Lambda 的 `Active` 只让 AWS 产生**函数自身的 segment**；下游调用要有 SDK 打点
才会产生 subsegment。实测 `neptune-etl-from-xray` 的 trace：

    segment neptune-etl-from-xray  origin=AWS::Lambda
       └ Attempt #1 / Dwell Time          ← Lambda 服务自己产生的
    segment neptune-etl-from-xray  origin=AWS::Lambda::Function
       └ Init / Overhead                  ← 同样是服务产生的

**一个下游 subsegment 都没有**（既无 `namespace=aws` 也无 `namespace=remote`）。
所以 X-Ray 服务图里当然没有这条边 —— 这不是采样问题，是**根本没打点**。

**而且那 2 条边不在承重清单上。** 它们是
`neptune-etl-from-xray -> petsite-neptune` 与 `-> xray`，
属于「观测者自身的基础设施」，验了不改变任何决策结论。
为它们改 5 个生产 Lambda（加 SDK 到层 + 代码调 patch_all，或挂 ADOT 层 +
设 `AWS_LAMBDA_EXEC_WRAPPER`）不划算。

**真正值得看的是**：预检「测不出」的 19 条里有 **10 条是承重的**，
它们的源是应用服务（petsite / payforadoption / pethistory / petfood / petsearch），
不是 ETL Lambda。那些服务在 X-Ray 里是可见的（petsite→petsearch 有边），
所以是**部分打点** —— HTTP 有、AWS SDK 调用没有。这才是提高覆盖率的主战场。

### 二、「加一条定向采样规则」—— 不该做，它解决不了这个问题

**采样只决定已发出的 span 记不记，它变不出从未发出的 span。**
上一轮我把两个失败模式混成了一个，这里更正：

    concierge 那类   span 发出了（runtime 有 OTel），但没被采样   → 采样是解法
    ETL Lambda 那类  span 从未发出（无 instrumentation）          → 打点是解法
    应用服务那 10 条 部分打点（HTTP 有、AWS SDK 无）              → 打点是解法

19 条测不出的边属于后两类。加采样规则对它们**零效果**，只增加成本。

（定向规则本身的成本判断我也更正一下：它覆盖的是低频服务，
所以「高采样率」在这里几乎不花钱 —— 但既然解决不了问题，便宜也不该做。）

### 三、「只打 petsearch → S3」—— 跑了，你们的基线闸门正确拦下

做法：`chaos/code/runner/*` 现已全部提交，只有 `scripts/verify_via_iam_deny.py`
仍是未提交状态。所以我用 `git show HEAD:` 导出**已提交版**到 scratch 去跑，
**完全没碰你们的工作副本**。

探针的输出：

    可加 deny 的角色: ServicesEks2-searchserviceServiceAccountRole588AF64-...
      （由 ServiceAccount search-service-sa 的注解取得）
    deny 语句: s3:* on arn:aws:s3:::serviceseks2-s3bucketpetadoption... (+/*)

    ── 1. 基线 ──
       被测边: {'success_rate': 0.0, 'total_requests': 138, 'p99_ms': 423.8}
       业务探针: home=[26, 26, 26]
    ✗ 基线成功率 0.00% < 下限 95% —— 拒绝开跑。

**闸门是对的，而且它抓到了一个真实状况。** 探针给的常见原因是「上次实验的 deny
仍在生效」，我查了：`searchserviceServiceAccountRoleDefaultPolicy608C0257` 里
**没有任何 Deny 语句** —— 不是实验残留。

X-Ray 服务图（近 15min）：

    PetSearch → serviceseks2-s3bucketpetadoption...   total=634  ok=0  err=634  fault=0
    PetSearch → ServicesEks2-ddbpetadoption...        total=4535 ok=4535 err=0
    PetSearch → STS                                   total=4    ok=4    err=0

**634 次全是 4xx，零 5xx，零成功。** 解码 trace 后看到原因：

    S3  op=CreateBucket  status=409
        BucketAlreadyOwnedByYouException
        "Your previous request to create the named bucket succeeded and you alr..."

`petsearch` 每次都去 `CreateBucket`，桶已存在于是 409。**应用本身是好的**
（DynamoDB Scan 200、业务探针 home=26 稳定）。

### 这条边引出一个建模问题，比注入本身更值得记

`petsearch → S3` 这条边**观测充足（634 次）却不可验证**，而且它是承重的
（S3 在割点关联路径上，有 3 条承重待测边）。原因有两层：

1. **成功率通道恒为 0%**，所以 deny 注入产生不了可测的 delta。
   这与你们在 `74a9369` 给 `petsite → StepFunction` 换证据通道是同一类问题
   （那次是「X-Ray 结构性看不见」，这次是「看得见但基线已坏」）。

2. **更根本的：这条边的真实数据路径不在这些调用里。** 观测到的 S3 API 流量
   全部是失败的 CreateBucket；而图片是通过**预签名 URL** 交付的
   （早先在 orchestrator 应答里见过 `...s3.amazonaws.com/puppies/p10.jpg?
   X-Amz-Security-Token=...`），预签名是本地密码学操作、**不调用 S3 API**，
   浏览器直取，因此永远不会出现在 petsearch 的 trace 里。

   ⚠️ 这一层我标为**推断**：证据是 `ok=0`（无任何成功的 S3 API 调用）+
   解码出的两条 trace 全是 CreateBucket + 观测到的 URL 是预签名的。
   我没有读 petsearch 的源码（它是外部 demo 应用）。

   如果这个推断成立，那么「给 petsearch 的角色加 s3:* deny」会切断的是
   CreateBucket（无用户影响），而预签名 URL 的浏览器请求按签名者权限评估、
   **也会被 deny 挡住** —— 也就是说这个手段其实能影响真实路径，
   但**观测通道看不到**（浏览器请求不经 petsearch）。
   要验它得靠业务探针（搜索结果里的图片还能不能取到），不是边级成功率。

### 建议的下一步（都不是「多跑注入」）

  a) 给 `petsearch → S3` 换证据通道：业务探针为主（图片可取性），
     边级成功率为辅。参照 `74a9369` 的做法。
  b) 那 10 条承重的「测不出」边，主战场是给应用服务的 AWS SDK 调用补打点，
     不是采样规则。
  c) `petsearch` 每次 CreateBucket 是个反模式（应用侧），值不值得提给 demo 应用
     的维护者由你们判断 —— 对本平台的直接影响是它让这条边的成功率通道永久失效。

---

## 2026-09-17 下午 — 三次误判的真相：**LLM 是一个万能的隐式降级路径**

这是本台账目前最重要的一条。前面 09-15 和 09-17 上午两段关于
"IAM deny 打不断 agent 间委派"的记录**全部作废**，
连带那条"运行时缓存凭证"的错误解释。

### 真相

读 `one-observability-demo/PetAdoptions/waggle_ai_agents/
orchestrator_strands/delegate.py`：

```python
TRANSPORT = os.getenv("AGENT_TRANSPORT", "local")

def delegate(agent, query, user_id=None):
    if TRANSPORT == "gateway":
        return _via_gateway(agent, query, user_id)
    return _in_process(agent, query, user_id)

def _via_gateway(...):
    SigV4Auth(boto3.Session().get_credentials(),
              "bedrock-agentcore", config.AWS_REGION).add_auth(signed)
    try:
        resp = httpx.post(url, ...); resp.raise_for_status()
    except Exception as exc:
        return json.dumps({"error": f"gateway call to '{agent}' failed: {exc}"})
```

三件事同时确认：

1. **委派用标准 SigV4**，不是 JWT（我上午的猜测也错了）；
2. CloudTrail 按 access key（`ASIA5PH35QDKIOWMJ2FF`）反查，
   签名身份就是 `WaggleAIAgents-RoleWaggleAIOrchestrator...` ——
   **正是我 deny 的那个角色**；
3. 施加 deny 后源侧运行时日志：

       "gateway call to" 168 次
       "403"             379 次 → 6.89 次/分（基线 0.02，413 倍）
       gateway call to 'adoption' failed: Client error '403 Forbidden'

**IAM deny 一直是生效的。委派被切断了 379 次。**

### 为什么我三次都看不见

两条观测通道**同时瞎**，而我没有验证过"观测有没有能力看见"：

· **边级流量**：委派经网关，X-Ray 服务图里只有一条到
  `gateway.bedrock-agentcore` 的合流边，测不出目标粒度；
· **业务探针**：那个 `except: return json.dumps({"error": ...})` ——
  **网关失败被包成一个 JSON 错误字符串返回给 LLM，不抛出**。
  LLM 收到错误，就用对话历史里的旧信息编出一个看起来完全正常的回答。
  探针判据是"回答是否为兜底文案"，LLM 编的回答不是兜底文案，
  于是判"未退化"。

我拿"业务未退化"反推"注入未生效"。**这个反推需要一个前提：
观测方有能力看见这次故障。那个前提我从没验证。**

### 可迁移的一条（这是本条记录的价值所在）

**在 agent 系统上，"业务功能是否退化"会系统性地漏判依赖故障。**

LLM 是一个万能的隐式降级路径：无论下游工具返回什么错误，
它都能用上下文历史 + 自身知识编出一个像样的回答。
传统混沌工程的判据（用户功能是否可用）在这里失效。

而且这个"降级"**不是优雅降级，是静默错误**：
LLM 回答"Puppy 001 可领养"，而真实数据此刻取不到。
用户拿到一个自信、流畅、**可能已经过时**的答案，
比拿到一个明确的错误页危险得多。
这是 Waggle 的一个真实韧性缺陷，不是特性。

**所以 agent 系统的依赖验证必须有源侧证据通道。**
已实现 `_source_denial_count()`：读调用方自己记录的失败，
不经聚合、不经语义解释。并且要**比速率**而不是看有没有 ——
干净窗口也有 0.02 次/分的常态 403。

### 这一轮的收尾动作

· `AgentRuntime` 加回 `SEVERANCE_METHODS`，带完整证据链注释；
· `t67_02` / `t74_06` 第三次反转 —— 但这次的证据是源侧 379 次 403，
  与前两次（能力证据、被欺骗的观测）不同类。两个用例都写清了
  "要再反转需要什么证据"：源侧拒绝速率没升高，
  而不是"业务没退化"；
· 新增 `t74_08` 钉住源侧通道必须存在、必须比速率、
  注释必须写清 LLM 会吸收故障；
· `WaggleAIOrchestrator -> WaggleAIAdoption` 判 **soft**
  （切断成功 + 业务无感 = 该问句下依赖不承重），
  `evidence_channel='source-denial-log+business-probe'`；
· 另两条 Delegates 边清掉不可达标注回验证队列。

### 附：内部数据管道类依赖的判据（同日完成）

`neptune-etl-from-xray -> petsite-neptune` 暴露的缺口：
这类依赖**没有面向用户的消费方**（ETL 的消费者是图谱自己），
9 个业务探针全部打的是用户服务，拿它们验只会得到"业务未退化" ——
又是观测方选错，不是依赖不承重。

新增 `chaos/code/runner/pipeline_probes.py`，两个设计决策：

1. **观测方 = 产出物新鲜度**（`max(r.last_seen)`），不是用户功能。
   刻意不用 `count()`：图谱是 upsert，切断 ETL 不会让边消失，
   数量对这件事完全不敏感、会永远判"未退化"。
2. **主动触发**而非等周期。ETL 是 `rate(1 hour)`、实验窗口 180s，
   等周期意味着实验跑一小时以上且期间任何变更都污染结论。
   实测：主动 invoke 后 `last_seen` 从 1789626147 前进到 1789626226。

⚠️ 主动触发的前提是**幂等**。`PIPELINE_OUTPUTS` 是白名单，
只登记已核实幂等的源 —— 纪律与 `SEVERANCE_METHODS` 同源：
不幂等的源用这个手段会把混沌实验变成数据损坏。

`trigger_and_observe` 把 "invoke 成功但产出未推进" 与 "invoke 失败"
**分开记**，因为前者正是本次误判的形状（源吞掉了错误）。
`t76_06` 钉住这一点。

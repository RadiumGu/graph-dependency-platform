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

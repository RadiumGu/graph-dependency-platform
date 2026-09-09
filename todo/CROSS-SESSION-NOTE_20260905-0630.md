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

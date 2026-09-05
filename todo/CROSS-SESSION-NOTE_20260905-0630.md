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

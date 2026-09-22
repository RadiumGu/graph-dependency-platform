# 全面审查发现清单（2026-09-21）

六路并行审查的产出：四路读代码（`rca/` `chaos/` `dr-plan-generator/` `demo/`+`scripts/`）、
一路浏览器逐页核验线上演示站、一路盘点 163 份文档。

**已修的不在此列**（见 git log：`1c42c6c` 演示站三处、`incident_writer` 搬回权威源、
`b58beb4` DR 排序边、`4cbcf23` golden 自比、文档归类）。
这份清单只记**尚未处理**的，按严重度排序。每条都标了它是「已确证」还是「疑似」。

---

## 一、结构性问题：构建产物被纳入版控且已双向漂移

**需要拍板，不是单点修复。**

`infra/lambda/rca_window_flush/` 是 `build.sh` 的输出目录，却被 git 跟踪，
而 CDK `Code.fromAsset` 直接打包它。`build.sh` 的流程是：

```
RCA_DIR="$(cd "$SCRIPT_DIR/../../../rca" && pwd)"    # 第 14 行
DEST_DIR="${DEST_DIR:-$SCRIPT_DIR}"                  # 第 29 行 ← 默认就地
find "$DEST_DIR" -mindepth 1 ... -delete             # 第 36 行
```

提交的这份产物是从**更旧的 `rca/`** 生成的，已确证两处行为分叉：

| 产物 | `rca/` 权威源 | 后果 |
|---|---|---|
| `core/rca_engine.py` **没有** `analyze_group` | `:926` 有 | `window_flush_handler.py:137` 调它 → AttributeError → 被 `except` 兜住 → 降级为单服务 `analyze()`。**窗口聚合 RCA 从未生效** |
| `core/fault_classifier.py:137` 在 `combined_tier0 >= 2` 时**自动升 P0** | 刻意不升，只允许 P2→P1 | 多 Tier0 抖动时误发 P0。源码 docstring：「P0 应由图谱拓扑证据决定，而非告警条数多，否则一次波及广但无关键业务的抖动会连发 P0，制造告警疲劳」 |
| `collectors/aws_probers.py:257` 有硬编码 `SERVICE_FUNCTION_MAP` | 已改为从 `profiles/petsite.yaml` 派生 | YAML 里新增/改名的服务映射不被该副本感知 |
| `neptune/neptune_queries.py` 472 行 | 882 行 | 缺 4 个验证类查询（q20/q21/q22/q23）。**当前不影响**：实测该运行时路径不调它们 |

**线上当前是对的** —— 2026-09-20 部署时跑过 `build.sh`，日志有 `analyze_group` 输出。
但只要有人不先构建就 `cdk deploy`，发布的就是上面这两个缺陷。

**为什么守卫没拦住**：`scripts/scan_missing_symbols.py` 的 `SCAN_DIRS` 是
`["rca","chaos/code","scripts","demo"]`，不含该产物目录；`tests/test_79`/`test_83`
走 PYTHONPATH 解析到 `rca/`。所以源树全绿，部署的副本无人校验。

三个候选方案（需选一）：
1. `DEST_DIR` 改为隔离目录（`$KIROCREW_SCRATCH/wf-build`），产物从版控移除并加 `.gitignore`
2. CDK asset 指向隔离构建目录，`cdk deploy` 前置钩子强制 `build.sh`
3. 扩展 `scan_missing_symbols.py` 与相关门禁到打包目录（治标，漂移仍在）

---

## 二、chaos/ 四条高危：故障注入的闸门失效或失败未上报

这四条涉及安全闸门与恢复语义，**改错的代价比留着大**，需先确认语义。

**1. 时长闸门对分钟/小时单位失效（已确证）**
`/home/ec2-user/works/graph-dependency-platform/chaos/code/runner/runner.py:233`

```python
"duration_sec": int(exp.fault.duration.rstrip('smh').split('.')[0])
```

`rstrip('smh')` 只删尾字符：`"5m"→5`、`"10m"→10`、`"1h"→1`（已实测）。
而实验规格里绝大多数是分钟单位。规则 R008 限制 ≤600s，于是一个真正 600 秒的
实验以 `duration_sec=10` 喂给 LLM，时长安全判定被彻底旁路。
**同文件 401 行就有正确的 `parse_duration()`**，此处却自写了错的内联解析。

**2. 运行时上下文恒为空，三条运营规则永不触发（已确证）**
`runner.py:239-240` 把 `recent_incidents` 与 `recent_experiments` 硬编码为 `[]`，
而 R005（同服务 30 分钟间隔）、R006（SEV-1/2 期间禁跑）、R007（每服务每日 ≤5 次）
全靠这两份数据判断。另外 `ENVIRONMENT` 默认 `"staging"`，未设环境变量时 R009
会放宽命名空间与故障类型限制 —— 在未配置环境里等于降级防护（应 fail-closed 视为生产）。

**3. 错误路径抛 NameError，不降级反而崩溃（已确证）**
`/home/ec2-user/works/graph-dependency-platform/chaos/code/runner/injectability.py:190`
except 分支调 `logger.warning(...)`，而该模块**从未 import logging 或定义 logger**。
实测 `NameError: name 'logger' is not defined`，`_iam_deny_cache = frozenset()`
那行永不执行，异常向上冒泡。而 `select_targets_for_verification` 的循环没有 try 包裹，
catalog 解析失败会让整个选边流程崩溃。**一行修复**：`import logging; logger = logging.getLogger(__name__)`。

**4. 删 CRD 失败只打日志，报 PASSED 却留污染（已确证）**
`runner.py:855` phase4 正常路径删 Chaos Mesh CRD，`except` 里只 `logger.error`，
**不设失败标志、不改 `result.status`**。删除失败时 tproxy 残留在 Pod netns，
而 Phase5 采样在残留生效之前 —— 实验仍判 PASSED，污染带进下一轮基线。
违反「故障注入必须可自动恢复」这条硬约束（恢复的清理环失败未被上报）。

**5. FIS 等待超时不 stop（已确证·中）**
`fis_backend.py:816` 超时只 warning 并 `return "timeout"`，**不调 `self.stop()`**；
而 `runner.py:863` 传的 `RECOVERY_TIMEOUT=300s` < 常见的 5m/10m 实验时长。
若 FIS 卡在 running，runner 拿到 timeout 后继续删模板、做 Phase5 稳态测量、出报告
—— **故障可能仍在注入中而恢复已被宣告**。

**6. 依赖标签回退清单 3 条 vs 契约 10 条（已确证·中）**
`edge_verification.py:86` 的回退是 `('AccessesData','Calls','DependsOn')`，
缺 7 类。仅在 `graph_contract` import 失败时启用，但一旦触发，
`coverage()`/`select_targets_for_verification()` 静默只认 3/10 类依赖边。
判据不该带残缺回退 —— 要么同步为 10 条，要么 import 失败直接 raise。

**7. `_assert_cacheable` 三份副本仍不一致（已确证·中）**
`learning_strands.py:156` 用 `//3`、只捕 `ImportError`、且用**裸 `assert`**
（`python -O` 下被剥离，门禁静默失效）；另两份用 `//4`、`except Exception`、
`raise AssertionError`。应提到共享模块并统一改为显式 `raise`。

**8. PolicyGuard 纯 LLM 化，硬规则无确定性兜底（疑似·部分为有意）**
2026-09-20 删 direct 后，「R003 集群级故障永久封禁」「R004 生产爆炸半径」
这类不该由概率模型裁决的硬约束，现在只以文本写进 system prompt。
`evaluate`/`_parse_response` 的 fail-closed 做得正确，但结合第 1、2 条
（LLM 收到的输入本身已失真），建议对 critical 规则保留一层确定性前置校验。

---

## 三、dr-plan-generator：生成的命令不可执行

**1. K8sService 步骤退回了字符串拼接 context（已确证·高）**
`/home/ec2-user/works/graph-dependency-platform/dr-plan-generator/planner/step_builder.py:961,963`
用 `--context {target}-cluster`，正是该模块在 `_kubectl_target()`（`:79-101`，
docstring `:87` 明确记载）为 microservice 步骤修掉的反模式，唯独 K8sService 没迁移。
`kubectl` 对不存在的 context 直接非零退出，这一步及其 rollback 执行时立即失败。
**为何没被发现**：`dr-plan-generator/tests/test_step_builder.py:189-193` 只断言命令里含 `"kubectl"`
和 `"endpoints"`，不检查 `-cluster` 拼接；而 `dr-plan-generator/tests/test_strategy_steps.py:124` 那条
`assertNotIn("eu-central-1-cluster", ...)` 只覆盖 microservice 步骤。

**2. Lambda 回滚引用未绑定变量（已确证·中）**
`step_builder.py:929`：`--uuid $EVENT_SOURCE_UUID`，该变量从未在步骤内赋值。

**3. 校验器只扫 `command`，漏 `validation` 与 `rollback_command`（已确证·中）**
`validation/plan_validator.py` 的 `_check_validation_quality` 只对 `step.command`
跑 `_unbound_shell_vars` —— 这正是第 2 条得以出厂而不被拦的原因。

**4. `plan_policy.yaml` 大半是摆设（已确证·中）**
`estimated_time` / `requires_approval` / `max_parallel` / `non_reversible_actions`
**从不被生成流程读取**（`policy_loader.py:167-210` 的访问器零外部调用）。
确证矛盾：yaml 写 `RDSCluster.estimated_time: 120`，而 `step_builder.py` 硬编码 300、
`rto_estimator.WARM_STANDBY_TIMES["RDSCluster"]=300`。改这份 policy 不产生任何效果，
而审计读 policy 得到的是计划并未采用的数字。`non_reversible_actions` 里的
`promote_read_replica` 连动作名都已过期。

**5. `detect_parallel_groups` 已实现且有单测，但从未接入（已确证·中）**
`graph_analyzer.py:179` 定义、有测试，但 `plan_generator._nodes_to_steps` 传的 ctx
只有 `{"order","scope"}`，故 `_build_microservice_step` 的 `ctx.get("parallel_group")`
恒为 None。后果：尽管 policy 声明 `parallel_within_layer: true`，RTO 仍把所有
微服务串行累加（**系统性高估**）。

**6. 兜底边集漂移：`_FALLBACK_ORDERING_EDGES` ⊄ `_FALLBACK_SCOPE_EDGES`（已确证·中）**
`graph/scope.py:40-55`。ordering 兜底含 `RoutesToRuntime`/`RoutesVia`（2026-09-21 补的），
scope 兜底没有 —— 而注释声称「保持与该文件一致」。走兜底路径时，
仅经这两类边可达的节点会被排除出 scope，于是这些依赖边也从 ordering 消失。
生产走 yaml（一致），故影响以兜底为条件。

**7. `find_critical_path` 是死代码（低）**
`graph_analyzer.py:228`，全模块无调用者，约 70 行看似 load-bearing 的逻辑。
它还硬用 `RTOEstimator.DEFAULT_TIMES`（= warm standby），若将来接线会低估
pilot light 关键路径。

---

## 四、demo/ 与 scripts/

**1. 混沌工程页「实验历史」表四列全为 `None`（已确证·中，未修）**
`name` / `status` / `target` / `started_at` 全部显示字面 `None`，只有 `fault_type`
与 `result` 有值。合计数自洽（failed 10 + inconclusive 5 + passed 76 = 91），
所以是**字段映射**问题而非查询问题 —— 疑为 experiment 节点属性名与读取字段不一致。

**2. 一个改图脚本默认即写（已确证·中，未修）**
`/home/ec2-user/works/graph-dependency-platform/scripts/unfile_observation_from_intervention.py:86`
用 `--dry-run`（不加参数就直接改图），而同类的七个脚本都用 `--apply`（默认安全）。
习惯了「默认安全」的人裸跑它会即时写入。
范式参考：`verify_external_target_edges.py:210` 用 `required=True` 的互斥组，无默认动作。

**3. 离线快照已落后线上 19%（已披露的有意降级）**
`demo/fixtures/verification.json` 是 `captured_at 2026-09-20`、total 106，
线上 total 130。`mode_badge` 会显式标注「离线快照（抓取于…）」，所以**不算欺骗**，
但漂移幅度已大到会误导离线观众。建议把 `fixtures/refresh_fixtures.py` 纳入定期刷新。

**4. AWS Account ID 出现在页面真实数据里（疑似·低）**
`926093770964` 出现在 Agent 依赖页的 AgentMemory ARN、合规报告的 ECR 资源名。
这是图谱里的真实节点标识（非页脚硬编码），站点又在 Cognito 后，严重度低。

**5. `factory.py` 反向过期文档（已确证·中，未修）**
`rca/engines/factory.py` 的模块 docstring 与三个 `make_*_engine` 的 docstring
都写「strands 不可用 → warning + 回退 direct（不崩）」，而代码已无回退，
strands 不可用即抛 ImportError。运维照 docstring 会以为有降级兜底而不检查打包。

---

## 五、已核实为正确的设计（不要"修"它们）

审查同时确认了这些看似可疑但实为有意的设计，列出来避免后人误改：

- **恢复顺序拓扑排序方向**：`topological_sort_within_layer` 按契约「src 依赖 dst」
  对**反向**图跑 Kahn，dst 先出。注释与实现一致。
- **RPO 不可推定时返回 `None`** 而非编数字；RTO 查表值可被 `measurements.json`
  实测覆盖，并在 basis 里标 `design_values_only`。
- **`probe_neptune` 的失败可见**：异常被捕获后写入 `findings["error"]` 并返回，
  调用方能看到失败而非误读为「无异常」；`info` 为空时还专门加 `note` 说明
  「服务不在图谱中」。
- **`graph_confidence.py` 与 `edge_verification.py` 的判据**：边级流量门禁、
  注入生效门禁、独立证据门禁、稀释归一化、置信度封顶，以及「在 B 注入观测 A」、
  `verify_degradation` 空通道不写 —— runbook 记录的教训已被逐条门禁化。
- **`make_learning_engine` 导入 `agents/` 而它不在部署包清单**：注释已明确它只被
  CLI 与测试调用，两个 Lambda 都不走 learning。当前无碍，但值得在打包自检里加断言。
- **覆盖率分母含 `modeling_artifact`**：它们在契约里确是 `dependency: true`，
  保守计入分母是口径选择。2026-09-21 已在计分板补出这两类，使可见分解与总数对齐。

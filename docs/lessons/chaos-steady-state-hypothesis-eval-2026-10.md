# 评估：给 chaos/ 补稳态假设判定 —— Chaos Toolkit vs LitmusChaos Probes

**日期**：2026-10-07
**结论**：**选 Chaos Toolkit,但只取规范 + `chaoslib` 库,不引入它的 CLI 作第二套引擎,也不依赖 `chaostoolkit-aws`。**

---

## 零、先纠正一处调研冲突

两路调研在一点上给出了相反结论,这里按证据强度裁决:

> **冲突**:LitmusChaos 一路称「Litmus 的 `Continuous` 模式在故障窗口内持续探测,这是它比 Chaos Toolkit 更强的地方 —— CT 的稳态假设是前后各求值一次,容易漏掉窗口内的瞬时抖动」。
>
> **裁决:这个说法不成立。** Chaos Toolkit 原生支持持续求值:
> `chaos run --hypothesis-strategy=continuously --hypothesis-frequency=N`（默认每秒一次),
> 另有 `during-method-only`（只在途中判、去掉首尾)。这些策略也能写进实验文件的
> `runtime.hypothesis.strategy` 段。
>
> **依据强度**:CT 一路直接读了 `chaoslib/run.py` 源码与官方 run-flow 文档;
> Litmus 一路自己标注了「本轮未重新抓取 CT 官方文档逐条核实」。采信前者。

**这个纠正很重要**,因为 Litmus 一路由此给出的建议(「借 Litmus 的 mode 语义把 CT 从前后两次升级为持续探测」)是**不必要的** —— CT 本来就有。

---

## 一、决定性因素:形态与控制面耦合

这不是「哪个功能多」的问题,是**能不能骑在我们两个后端之上**的问题。

我们的现状:Chaos Mesh(K8s 内)+ AWS FIS(云资源)**双后端**。

### LitmusChaos:绑死在 K8s 控制面

- 实验与 probe 全部是 **Kubernetes CRD**(`ChaosEngine` / `ChaosExperiment` / `ChaosResult`)。
- **它有 AWS 实验包**(`EC2 Stop By ID/Tag`、`EBS Loss`、`AWS SSM Chaos`),但 —— 这是关键 ——
  **打 AWS 云资源的故障,仍由跑在 K8s 集群里的实验 Pod 调 AWS API 发起**,
  AWS 凭证要放进 **K8s Secret**(`cloud-secret` / `cloud_config.yml`),
  前置条件白纸黑字要求 Kubernetes >1.16 + Litmus Operator 运行中 + ServiceAccount + ClusterRole。
- 依据:[EC2 Stop By ID 前置条件](https://litmuschaos.github.io/litmus/experiments/categories/aws/ec2-stop-by-id/)

**所以引入 Litmus 作为运行时 = 在 K8s 侧放第二个与 Chaos Mesh 功能重叠的引擎,而对 FIS 后端毫无帮助**(FIS 是独立 AWS 服务,Litmus 碰不到它)。

### Chaos Toolkit:后端无关,无控制面

一个 Python 进程。实验是单份 JSON。这让它能**同时骑在 Chaos Mesh 与 FIS 之上**做统一的稳态判定层 —— 而这正是我们缺的那一层。

---

## 二、LLM 生成实验:CT 是库 API 原生支持

这条对我们尤其关键,因为平台的 Bedrock Agent 要基于 Neptune 依赖图**生成**实验。

**源码级核实**(`chaoslib/run.py`):

- `chaoslib.types.Experiment` **本质就是一个 `dict`**。
- 公共入口 `chaoslib.run.Runner(strategy).run(experiment, settings=..., experiment_vars=...)`
  的第一个参数就是普通 Python dict,内部直接 `experiment.get("steady-state-hypothesis")`、
  `experiment.get("method")` 按字典取值。
- CLI `chaos run file.json` 的流程是:loader 读文件 → 校验 → 交给 `Runner.run(dict)`。

**也就是说 Agent 生成 dict 后可以直接丢给 `Runner.run()`,不必落文件。**

需要诚实标注的边界:官方**文档**主要以「文件 + CLI」为例,「传 dict」是库 API 设计支持但文档未作教程强调 —— 属于「受支持但需走 `chaoslib` 库而非 CLI」的用法。

对比 Litmus:要跨 `ChaosEngine` / `ChaosExperiment` / `ChaosResult` 多个 CRD,且产物只能喂给 K8s,不能服务 FIS。LLM 生成友好度明显更差。

---

## 三、必须讲清的一处误解:CT 的 rollback 不是「稳态一破就自动回滚」

我们选它的动机里有「自动回滚」。但实测机制是:

| 时机 | 默认行为 |
|---|---|
| method 之前(闸门) | 判一次。**失败则实验直接 bail,method 根本不执行,且 rollback 不播放**(无可回滚之物) |
| method 之后(偏差检测) | 判一次。偏差则 journal 标记 `deviated=true` —— 但 method 此时**已经跑完了** |
| 途中 | **默认完全不判** |

**要做到「稳态一破立即中断并回滚」,必须显式三件套配置:**

```
--hypothesis-strategy=continuously   # 途中持续判定(默认每秒)
--fail-fast                          # 偏差达 fail_fast_ratio 即置 failed 并终止
--rollback-strategy=deviated|always  # 决定回滚时机
```

`rollbacks` 是顶层的 Action 数组,**总会执行,除三种情况**:(1) 假设在首次就失败;(2) 收到 SIGINT(刻意留现场给人排查);(3) 某个 control 触发中断。单个 rollback 失败不中断其余。

**集成设计含义**:不能假设装上就有自动回滚。平台的实验模板生成器必须把这三个策略**显式写进** `runtime` 段,否则默认行为是「跑完再说」。

---

## 四、头号风险,以及它为什么可以化解

### 风险:`chaostoolkit-aws` 已停滞 16 个月

| 组件 | 最新版本 | 日期 | 近 6 月 commit |
|---|---|---|---|
| `chaostoolkit`(CLI) | 1.21.4 | 2026-10-05 | **22** |
| `chaostoolkit-lib`(引擎) | 1.45.1 | 2026-09-27 | 7 |
| **`chaostoolkit-aws`(AWS 扩展)** | **0.35.1** | **2024-06-15** | **0** |

用户此前提到的沉寂期得到核实且准确:`chaostoolkit` 1.19.0(2024-02-20)→ 1.20.0(2026-08-08),**间隔 2 年 5.5 个月**,目前 1.21.4(2026-10-05)已复活且活跃。

**但 AWS 扩展没有跟着复活** —— 停在 2024-06-15,完全缺席 2026 年的复苏。对一个以 AWS FIS 为核心的平台,这看起来是致命的。

### 化解:我们几乎不需要 chaosaws

关键事实(已核实):**CT 的 probe 就是一个普通 Python 函数,不需要注册、不需要打包、不需要发布。**

```python
def my_probe(arg1, configuration: Configuration = None,
             secrets: Secrets = None) -> <任意 JSON 可序列化值>:
```

运行时只要模块在 `sys.path` 上可 import 即可。官方文档里 `os.path.exists` 这种标准库函数能直接当 probe 用,就是这个机制的证明。只有想让 `chaos discover` 枚举活动清单时,才需要在包的 `__init__.py` 里加 `discover_probes()` 钩子。

那么 chaosaws 对我们有什么不可替代的?逐项看:

| chaosaws 提供 | 我们是否需要它 |
|---|---|
| `chaosaws.cloudwatch.get_metric_data` / `get_alarm_state_value`(读指标作 tolerance) | **不需要。** 自己用 boto3 写十几行即可 —— 而且**应该**自己写:我们有 `aws_resilience.make_client()`(PR #62 刚合并),它带显式 `standard` 模式退避与分页上限抛异常。**我们自己的 probe 比 chaosaws 的更硬。** |
| `chaosaws.fis.start_experiment` / `stop_experiment`(触发 FIS) | **不需要。** 它只是「按模板 id 触发/停止/查询**已有**的 FIS 实验」,不建模故障类型、不创建模板。我们已经有自己的 FIS 引擎和 60 种故障类型 |
| 其余 18 个服务子包(asg/ecs/eks/rds/…) | 不需要 —— 注入由 Chaos Mesh 和 FIS 负责 |

**结论:只采用「规范 + `chaoslib` 库」,probe 全部自写,则 chaosaws 的停滞与我们无关。** 头号风险消失。

---

## 五、tolerance 词汇表对比

CT 的 `tolerance`(每个 probe **必须**带)支持的全部形态:

| 形态 | 语义 |
|---|---|
| `true` / `8` / `"OK"` | 严格相等(number 只允许整数) |
| `[4, 9]` | 闭区间 [下界, 上界] |
| `[4, 9, 78]` | 集合成员 |
| `{"type":"range","range":[4.6,8.9]}` | 浮点区间 |
| `{"type":"regex","pattern":"[0-9]{3}","target":"stdout"}` | 正则 |
| `{"type":"jsonpath","path":"foo[*].baz","expect":4}` | JSONPath |
| `{"type":"probe", ...}` | **内嵌 probe 作自定义判定**,签名收 `(value, secrets)` |

Litmus 的 `comparator`:数值 `{>=, <=, ==, >, <, !=, oneOf, between}`,字符串 `{equal, notEqual, contains, matches, notMatches, oneOf}`。

**判断**:CT 在结构上更丰富(JSONPath、内嵌 probe 这两样 Litmus 没有)。
**唯一真实的小缺口**:CT 没有一等的**单边比较符**。像 `error_rate < 0.01` 这种 SLO 阈值,Litmus 写 `criteria: "<"` 更顺手,CT 要写成区间或用 `type:probe` 套个 Python 函数。

这个缺口可以用一个自写的通用判定 probe 一次性补上(例如接受 `{"op":"<","value":0.01}`),成本极低。这是**唯一值得从 Litmus 借鉴的东西** —— 借的是 schema 设计,不是运行时。

---

## 六、FIS 侧:`stopConditions` 不等于稳态判定

这是一处必须写清的架构区分(核实结论与此前判断一致):

| | FIS `stopConditions` | 稳态假设判定 |
|---|---|---|
| 数据源 | **CloudWatch 告警** | 任意 probe(指标/HTTP/图查询/SLI) |
| 作用 | 实验运行中的**紧急刹车** —— 出事就停,防止把生产打崩 | 对「系统是否仍健康」给出显式 **verdict** |
| 时机 | 运行中 | 实验前 / 后 / 期间 |
| 语义 | guardrail / circuit breaker | pass / fail 判定 |

**FIS 原生没有稳态假设 verdict。** 它不会因为「系统恢复到稳态」而判实验通过。

依据:[AWS FIS — Stop conditions](https://docs.aws.amazon.com/fis/latest/userguide/stop-conditions.html)

**所以无论选哪个开源项目,稳态判定层都必须是我们自己的。** 这也再次说明为什么 CT 该以「库 + 规范」而非「第二套引擎」的形态引入。

Litmus 侧同理需要澄清:它**没有**独立于「撤销自己注入的故障」之外的通用 rollback。每个实验在 `TOTAL_CHAOS_DURATION` 结束后自带 revert,`stopOnFailure=true`(默认 `false`,即 probe 失败默认**不中断**)导致的中止也走同一套 revert。不存在「回滚到某个已知良好状态」的机制。

---

## 七、`aws-samples/fis-template-library`:直接取用

- **形态**:FIS 实验模板 **JSON**(不是 CloudFormation/Terraform)。每个实验目录含完整模板 + 所需 IAM 策略与信任关系 + README + 可选自动化文件。配套 [fis-template-library-tooling](https://github.com/aws-samples/fis-template-library-tooling) 做导入。
- **规模**:约 **26 个**实验目录,仓库 156 commits。
- **覆盖**:
  - EC2:`ec2-instances-terminate`、`ec2-spot-interruption`、`ec2-windows-stop-iis`
  - Aurora/RDS:`aurora-cluster-failover`、`aurora-global-region-failover`、`aurora-postgres-cluster-loadtest-failover`、`mysql-rds-loadtest-failover`
  - 数据库通用:`database-blocking-locks`、`database-connection-limit-exhaustion`、`database-io-exhaustion`
  - DynamoDB:`dynamodb-region-impairment`、`dynamodb-traffic-blackhole-region-impairment`
  - ElastiCache:`elasticache-az-power-interruption`、`-redis-connection-failure`、`-redis-primary-node-failover`、`-redis-primary-node-reboot`
  - 容器/网络:`ecs-fargate-az-impairment`、`eks-automode-az-impairment`、`cloudfront-impairment`、`direct-connect-resiliency`
  - 其他:`sqs-queue-impairment`、`msk-broker-reboot`、**`agentcore-strands-agent-faults`**、`sap-*`(3 个)

**特别值得注意 `agentcore-strands-agent-faults`** —— 本仓同时在用 Strands(`rca/neptune/nl_query_strands.py`、`strands_tools.py`)和 Bedrock AgentCore(有专门的 `etl_agentcore` 与 6 个 AgentRuntime 在图里)。这个模板直接对位我们的 Agent 层,是现成的故障注入覆盖面,值得优先取用。

**它不提供稳态假设 DSL** —— README 只要求用户运行时自备「proper monitoring and stop conditions」。

### 未能核实
- 各模板是否逐个内置 `stopConditions` 字段(未逐个打开)
- 最新提交的精确日期(搜索快照记 143 commits / 2026-04-14,与实时页面的 156 commits 不一致)

---

## 八、落地建议

1. **采用 Chaos Toolkit 的 `steady-state-hypothesis` 规范 + `chaoslib` 库(嵌入式 `Runner.run(dict)`)作为跨后端的稳态判定层。** 不装 CLI、不作第二套引擎 —— 我们已有 5 阶段引擎,功能重叠。
2. **probe 全部自写**,放本地模块即可(无需打包发布)。CloudWatch 读取走 `aws_resilience.make_client()`,继承 PR #62 的退避与分页上限。**不依赖 `chaostoolkit-aws`** —— 它停滞 16 个月,而我们本来就不需要它。
3. **补一个通用比较 probe** 支持单边比较符(`<` / `>=` 等),这是从 Litmus comparator 借来的唯一东西,借的是 schema 不是运行时。
4. **实验模板生成器必须显式写入** `runtime.hypothesis.strategy=continuously` + fail-fast + `runtime.rollbacks.strategy`,否则默认不会「破即停并回滚」。
5. **不引入 LitmusChaos**:K8s 侧与 Chaos Mesh 重叠,FIS 侧完全无用,且 LLM 生成友好度更差。
6. **FIS 侧分层写清**:`stopConditions` 只当安全熔断,稳态判定归我们的判定层。
7. **取用 `fis-template-library`**,优先 `agentcore-strands-agent-faults`(直接对位本仓 Agent 层)、Aurora/RDS failover、ElastiCache 系列。
8. **治理**:CT 按 dotted path 执行任意 Python 模块。信任边界与本仓现有机制同级,但需纳入白名单管控 —— 尤其因为实验定义是 LLM 生成的,不能让生成的 `module`/`func` 指向任意路径。

## 九、必须坚持的结论

**「基于依赖图自动生成实验假设」没有任何开源实现。** 四路调研(含此前的混沌工程平台专项)均未找到对位物:Chaos Toolkit 给的是假设的**表达与判定**,Litmus 给的是 probe 的**执行**,FIS 模板库给的是**故障目录**。「从 Neptune 的依赖拓扑推出该验证哪条边、该用哪种故障、稳态该怎么定义」—— 这是本平台的差异点,自研不可避免,也不该试图外包。

引入 Chaos Toolkit 要补的是**判定**这一环,不是生成这一环。

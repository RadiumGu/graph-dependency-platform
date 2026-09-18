---
name: dependency-verification-graph
description: >
  回答任何「A 是否依赖 B」「X 的依赖有哪些」「这条依赖可靠吗」类问题时的必读规程。
  这个 agent space 有一张持久化的依赖图谱，每条依赖边都带故障注入验证判定
  （confirmed / refuted / inconclusive / untested）。**不要用 FIS 实验模板的存在
  与否推断验证状态**——模板是意图，不是结果。
---

# 依赖验证:先查图谱,再下结论

## 这个 skill 为什么存在

2026-09-07 实测:同一个依赖验证问题问 12 次,**不提图谱时只有 25% 的调用会去查
图谱(2/8)**,点名要求就是 100%(4/4)。图谱以 MCP server `graph-dependency`
形式关联在本 agent space 上、24 条只读查询全部可用、一调就准 —— 问题不在工具,
在**没想到要用它**。

没查的那些回答一律拿「FIS 实验模板存在」当依据,原话例如:

> 5 个 FIS Aurora Reboot 模板(EXT3bF1…等),目标精确指向 writer 实例……
> 配置双重停止条件 —— 说明依赖已通过故障注入验证路径建立

**这段话每个字都对,但它不能支撑 `confirmed` 这个判定。** 模板存在只说明有人打算
测;验证需要**结果**:故障注入了、观测端退化了、退化幅度是多少。
那些数字只存在于图谱上,AWS 控制面里没有。

## 规程

### 1. 依赖关系必须先查再说

讨论「A 是否依赖 B」之前,先调 `graph-dependency` 的:

- `q22_edge_verification_verdicts` —— **干预层**判定(注入故障后依赖是否成立)
- `q23_verification_coverage` —— 验证覆盖率总览,先看这个再决定要不要逐边下钻
- `q3_upstream_deps` / `q1_blast_radius` —— 依赖枚举与影响面

不要凭服务命名、常见架构模式或先验知识推断。

### 2. 判定四态,含义不可混用

| 判定 | 含义 | 能否作为推理依据 |
|---|---|---|
| `confirmed` | 已用故障注入确认(依赖目标端注入、观测源端退化) | 可采信,`verify_degradation` 给出影响强度 |
| `refuted` | 已用故障注入证伪 | **不得作为推理依据** |
| `inconclusive` | 证据不足(退化落在 5%–20%／观测流量不足／注入是否生效未知) | 可提及,必须标注为未定 |
| `untested` | 尚未主动验证。**多数边是这个状态,属正常** | 可用,但必须声明「该依赖尚未经过验证」 |

`refuted` 数量可能为 0:本平台有独立证据门禁 —— 只要该边被任何独立观测源看到过
(如 DeepFlow 调用计数非零),就永不得判 refuted。**那是保守设计的结果,
不是没做验证。**

### 3. 零退化不等于依赖不成立

注入后观测端零退化,有两种无法在指标上区分的解释:

- 依赖确实不传导(应 refuted)
- 注入根本没打到(什么都没验证)

**所以一律 inconclusive,绝不判 refuted。** 判断注入是否生效看
`injection_confirmed` 标志;它缺失时明确说「无法区分」,不要替它调和。

同理:**零流量与健康在指标上无法区分**,一律 inconclusive。

### 4. 不要编造本图谱没有返回的数值

指标值、延迟、错误率、iowait 之类如果不在工具响应里,就说「未获取」,
不要给一个看起来合理的数字。本图谱只提供依赖事实与验证判定,不提供指标 ——
指标要另外去 CloudWatch 取。

### 5. 两个 verification 不要混用

- `q20_dependency_verification` 是**观测层**:这条边最近有没有被观测到
  (`drift_status` / `runtime_verified` / `verified_by`)
- `q22_edge_verification_verdicts` 是**干预层**:注入故障后依赖是否成立

一条边可以「天天被观测到」同时「在注入实验里未能确认」——
**那种矛盾本身就是有价值的信号,请如实报告。**

### 6. 单一观测源要降权

若 `q21_observation_source_coverage` 显示某条依赖只被一个观测源看到,
明确说明证据薄弱 —— 历史上单一观测源造成过 85% 的假阴性判定。

### 7. 依赖边方向

契约里依赖边一律**从依赖方指向被依赖方**,所以:
**根因在出边、影响面在入边。** 搞反会把受害者当成嫌疑人。

⚠️ `q3_upstream_deps` 这个函数名里的 `upstream` 与自然语言层的术语相反
(NL 层:下游依赖=出边、上游调用者=入边)。**判断方向只看出边/入边,不看名字。**

## 什么算依赖边

只有这些边类型算依赖边:
`AccessesData` / `Calls` / `Delegates` / `DependsOn` / `Invokes` /
`InvokesTool` / `Retrieves`。

`RunsOn`、`TestedBy`、`LocatedIn`、`OwnedBy` 等**不是依赖边**,
它们上面没有 `verify_status`,不要拿它们回答依赖问题。

判定存在边的 **`verify_status`** 属性上(不是 `verification_status`,那个不存在)。

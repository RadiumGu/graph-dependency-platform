# compliance_export —— 合规依赖关系报告生成

从活图谱生成可交给监管/审计的依赖关系映射报告。**只读**，不写图谱。

```bash
export NEPTUNE_ENDPOINT=petsite-neptune.cluster-xxx.ap-northeast-1.neptune.amazonaws.com
export REGION=ap-northeast-1
export PYTHONPATH=infra/lambda/shared/python

python3 -m compliance_export --dry-run          # 只看摘要，不落盘
python3 -m compliance_export                    # 落盘到 todo/compliance/
```

样例产出见 `samples/`（用 `--sample` 以固定文件名重新生成，便于 diff）。

---

## 这份报告对应哪条监管要求

监管对依赖关系的要求**不是一类，是三类**，混为一谈是这个领域最常见的错误：

| 类别 | 代表条文 | 本质 | 本模块 |
|---|---|---|---|
| **A 穿透式依赖映射** | DORA Art. 8(4)、BCBS POR 原则四、SYSC 15A.4.1R、关基条例第九条 | **图问题** —— 条文的动词是 `map`，要求穿透到「资产之间的链接与互赖」 | ✅ 本模块做这个 |
| B 合同型登记册 | DORA Art. 28/29/31、OCC 2023-17 / FFIEC App. J | 合同数据集，每份合同一条记录，零流量也必须登记 | ❌ 结构上做不到，也不该做 |
| C 容忍度阈值 | SYSC 15A.2.5R / 2.9R | 业务判断 + 阈值检测 | ⚠️ 承载能力有，阈值待业务方填 |

**BCBS 自己点名的行业难点正是 A 类和 C 类**，不是 B 类：

> "The mapping of interconnections and interdependencies for critical operations,
> and the definition of tolerances for disruption to these operations **are the most
> common challenges that banks face** when adopting the Principles."
> —— [BIS newsletter 34](https://www.bis.org/publ/bcbs_nl34.htm)

最对位的一条原文是 DORA Art. 8(4)：

> "Financial entities shall identify all information assets and ICT assets... and shall
> map those considered critical. **They shall map the configuration of the information
> assets and ICT assets and the links and interdependencies between the different
> information assets and ICT assets.**"

三个动词递进：`identify` → `map those considered critical` → `map the links and
interdependencies`。**第三步在数据结构上就是一张图的边集，清单做不出来。**

---

## 文档形制的依据

章节骨架取自 **FCA SYSC 15A.6.1R(1)-(9)** —— 那一条列举了 firm 自己的书面记录
必须包含什么。文档控制要素（标准识别、固有局限性、所执行工作摘要、日期、批准）
借自 **ISAE 3000 (Revised) §69** 的要素纪律。取值词汇对齐 **ITS (EU) 2024/2956
B_06.01** 的枚举措辞（`Assessment not performed`）、**NIST OSCAL**
`observation.method` 的取证方法（`TEST` / `EXAMINE`）、以及 **SOC 2 Section IV**
的结论措辞（`Confirmed — no exceptions noted`）。

**但本报告刻意不声称是鉴证报告。** ISAE 3000 §69(h)(i)(j) 要求声明「本业务按本
ISAE 执行」、「适用 ISQC 1」、「遵守 IESBA Code 独立性要求」—— 自动生成的管理层
记录这三条一条都做不出。照抄整套要素会产出一份**暗示存在独立鉴证的文件**，
那不是形制粗糙而是虚假陈述。故 §1.2 明确否认，并由 `m11` 锁定。

---

## 四份产出与其条文依据

| 产出（报告章节） | 条文依据 |
|---|---|
| **§5 依赖关系映射** | DORA Art. 8(1)（business functions ← 支撑资产 ← 其 dependencies）、8(4)、SYSC 15A.4.1R 的 technology 维度、关基条例第九条(一) |
| **§6 证据状态分列统计** | 证据等级 ← SYSC 15A.5.3R 场景测试；声明/观测 ← 本平台核心不变量；第三方范围 ← DORA Art. 8(5) |
| **§7 技术集中度** | SYSC 15A.2.7G(10)「multiple IBS rely on **common operational resources**」、DORA Art. 29/31 的技术输入、关基条例第九条(三) |
| **§8 承载层（单列）** | 不是依赖，但 EKS/LB 在 DORA 视角下确实是关键 ICT 服务，不能静默丢弃 |

Markdown 呈现审阅所需的列；**机器可读的完整字段集在同名 CSV 里**，两者取自同一
快照。CSV 刻意保留技术字段名 —— 消费方是脚本与取证工具，字段名稳定比可读性重要。

---

## 四条不可协商的纪律（由 `tests/test_54_compliance_export.py` 锁定）

**1. 依赖边清单必须来自契约。** 只允许 `graph_contract.dependency_edge_labels()`。
本仓库曾因四处各抄一份依赖边清单而产生分歧（契约 / RCA 兜底 / drift 对账 /
dr-plan ordering），**其中两处漂移到实际错误**。`m01` 静态扫描内联字面量，
`m01b` 确认真的调用了契约函数。

**2. `verify_status` 与 `dependency_kind` 是强制列。** 前者回答「凭什么说这条依赖
成立」（SYSC 15A.5.3R 的测试证据），后者回答「配置声明的还是运行时观测的」。
少任何一列，这份报告就退化成填表工具的产出。`m02` 同时检查列在不在、以及查询
是否真的取了这两个字段 —— 列在但永远为空比没有这一列更糟。

**3. 三栏分列不得合并成单一「覆盖率」。** `Breakdown` **不提供**任何返回总体
比率的方法，`m03` 扫公开接口名拦住聚合语义。平均会让「已声明但从未观测」与
「已观测但从未验证」互相抵消，而这是监管审查最容易挑的点。

**4. 承载层单列且注明性质。** `m04` 断言承载标签与契约依赖边**零重叠** ——
同一种边不能既是承载又是依赖；若契约改了分类，这里要跟着改。

另有三条：

- **`m05` 快照时刻唯一**：`take_snapshot` 里取当前时刻恰好 1 次。实测本环境
  数分钟内 `LocatedIn` 从 1060 变 1054、`EC2Instance` 从 13 变 10 —— 分次拼接的
  报告会自相矛盾。DORA 用 12/31 统一基准日正是为此，ESAs 点名的失败模式里就有
  「跨模板基准日不一致」。
- **`m06` 文件名带快照时刻**：SYSC 15A.6.2R 要求保存 **each version** 的记录 6 年，
  同名覆盖会让历史消失。（`--sample` 例外，用固定名便于 diff；快照时刻仍在文件内容里。）
- **`m07` 只读**：出现任何写操作关键字即失败。生成合规报告的过程若会改动被报告的
  对象，报告本身就不可采信。

---

## 已知边界（报告会自动披露，此处提前说明）

- **证据覆盖率约两成**：多数依赖从未做过故障注入验证。报告如实分列 `confirmed` /
  `inconclusive` / 未验证，**不合并**。`inconclusive`（注入了但退化不足以判定）
  与「不成立」是两件事 —— 零流量与健康在指标上无法区分。
- **SYSC 15A.4.1R 六要素只覆盖两项**：technology 与 facilities 有；
  people / processes 完全没有（无此类节点），information 只到数据基础设施层。
- **impact tolerance 未设定**：三个业务能力均为 `null`。报告**不得**出现任何
  「未越界」表述 —— 没有阈值就是没有阈值，不能用「没检测到越界」掩盖。
- **集中度是技术集中度，不是供应商集中度**：DORA Art. 29/31 要的是供应商层面的
  集中度与分包链，需要 `Vendor` 法人实体节点，本平台当前只有技术对象。
  第四方分包链在埋点边界外，**结构上做不到**。

---

## 模块结构

```
compliance_export/
├── __main__.py      CLI（唯一入口 —— 两个入口是「四处各抄一份」的同类风险）
├── queries.py       四份产出的查询口径；依赖边清单从契约取
├── breakdown.py     三栏分列统计；结构上不允许合并
├── report.py        Markdown + CSV 渲染；局限披露由数据算出，不手写
└── samples/         样例产出（--sample 重新生成）
```

设计判据写在各模块的 docstring 里，不在本文件重复。

## Neptune openCypher 的两个坑（实测）

- **不支持 `any()` / `all()` 列表谓词**（报 `'any' predicate function is not supported.`）。
  标签多选写成源侧 `(s:A OR s:B)`、目标侧 `labels(t)[0] IN [...]`。
- **`BusinessCapability` 零出边**，方向是 `Microservice -[Implements]-> BusinessCapability`。
  遍历必须先反向跳一步。契约已注明该出边刻意不生成，别去「修」它。

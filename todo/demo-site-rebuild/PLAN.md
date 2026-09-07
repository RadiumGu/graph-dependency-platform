# 展示站重建 — 任务台账

**目标(用户原话)**:「重新考虑一下应该构建一个什么样的展示网站，想好了就去实现，
能最大程度展现图数据库的依赖关系管理的价值就行」

线上入口 https://rainmeadows.com/streamlit/ (Cognito 认证后)
部署:`openclaw-instance-v2` (`i-022fb7c32b71c72d9`) 的 `streamlit-demo.service`,
源码 `/home/ubuntu/tech/graph-dependency-platform`,Streamlit 1.62.0。

---

## 一、站点该长什么样(设计决定)

### 论点先于功能

这个项目的差异化在整个排查过程里已经反复确认:**市面产品回答「我看到了什么」,
本项目回答「我看到的是真的吗」**。所以站点不该是九个并列的功能页,
而该是一条能走通的论证线:

    ① 这张图是什么        规模、来源、契约 —— 让人知道在看什么
    ② 这张图是真的吗      验证判定 / 证伪 / 覆盖率 —— **核心,别的产品没有**
    ③ 它能帮你做什么      根因分析、混沌实验设计、DR 计划
    ④ 你可以自己问它      自然语言查询 + 固化查询

### 现状的问题不只是「有页面坏了」

实测九个页面里有**两组重复**:

    Graph Explorer (pyvis 分层总览)  vs  Interactive Explorer (Cytoscape 点击展开)
    Query Catalog  (固化查询,免 AI)  vs  Smart Query      (自然语言查询)

每组里都有一个是坏的。两个图查看器、两个查询入口,访客不知道该点哪个 ——
这本身就削弱了论证。

### 收敛方案:9 页 → 6 页

| 新结构 | 由谁构成 | 处置 |
|---|---|---|
| 首页 · 这张图是真的吗 | `app.py` | 改写导语,把论点摆前面 |
| ① 依赖验证 | `1_Edge_Verification` | ✅ 正常,保留(核心论据) |
| ② 依赖图谱 | `3_Graph_Explorer` + `9_Interactive_Explorer` | **合并成一个能用的** |
| ③ 根因分析 | `6_Root_Cause_Analysis` | ✅ 已重写 |
| ④ 混沌与容灾 | `7_Chaos_Engineering` + `8_DR_Plan` | 修 DR,考虑并页 |
| ⑤ 问图谱 | `4_Smart_Query` + `2_Query_Catalog` | 修布局,固化查询并入作「示例」 |
| ⑥ Agent 依赖 | `5_Agent_Dependencies` | ✅ 保留(差异化内容) |

---

## 二、实测坏在哪(2026-09-06 交互验证)

验证方法:AppTest 逐页**点按钮、填输入**,不是只看首屏 ——
上一轮只查首屏,完全漏掉了这三个问题,因为它们都在交互之后。

| 页面 | 现象 | 状态 |
|---|---|---|
| `2_Query_Catalog` | 5 个按钮(4×「运行」+「▶️ 执行查询」)全部 Δmd=0 Δdf=0 | ☐ 待修 |
| `8_DR_Plan` | 「🚀 生成 DR 计划」Δmd=**-5**、零表零指标 | ☐ 待修 |
| `9_Interactive_Explorer` | **0 个交互控件**、4 markdown、0 表 —— 近乎空页 | ☐ 待修/合并 |
| `3_Graph_Explorer` | 图分层坏了:全部节点挤在两层、标签截断(用户截图) | ☐ 待修 |
| `4_Smart_Query` | 引擎**正常**(返回真实表格),但对话框在页面最底、答案在最上 | ☐ 待改布局 |

已确认正常:`app.py`、`1_Edge_Verification`、`5_Agent_Dependencies`、
`7_Chaos_Engineering`、`6_Root_Cause_Analysis`。

---

## 三、任务队列

按「先让坏的能用、再谈合并」排序 —— 合并前必须先知道每个页面修好之后值多少,
否则会把还没看清价值的东西删掉。

- [x] **T0 在 RCA 页加对照屏** ✅ 2026-09-07 —— **下面这段是当时的调研记录（接口形状、agent space、MCP 关联现状），仍然有用；最终结论与实现见下方另一条 T0**。

  用户问「RCA 那里是不是可以加一个调用 devops agent 的对话框」。
  **该加,但不该做成通用对话框** —— Smart Query 已经是自然语言问答了,
  再加一个只是重复。真正有价值的是**对照**。

  ### 实测依据(2026-09-07,真实调用已跑通)

  `aws devops-agent` 是**官方 CLI 服务**(aws-cli 2.36.34,63 个操作),
  不需要 aws-samples 自研。闭环:

      CreateChat(agentSpaceId)          → {executionId}
      SendMessage(space, exec, content) → EventStream（流式,不是 dict,不能取 len）
      ListPendingMessages(space, exec)  → 轮询

  账号里已有 agent space **`petsite-devops`**
  (`60c2f48f-b6e3-4dce-a0a3-4144228b2051`,locale zh-CN)。

  **图谱 MCP server 已注册并关联**:service `47663b32-…` 名为 `graph-dependency`,
  endpoint 指向 AgentCore runtime `graph_dependency_mcp`(状态 READY),
  24 条只读查询全在 association 的 `configuration.mcpserver.tools` 里,
  描述写着「含故障注入验证判定、逐条出处与证据纪律」。

  ### ⚠️ 更正:我第一次的结论是错的

  第一次实测后我写的是「**它没用图谱**」。**这个结论被三轮采样推翻了**,
  而且推翻它的正是我自己犯的第五次同族测量错误 ——
  **我数的是 `contentBlockStart` 里 `tool_use` 类型的块,恒为 0;
  真实工具调用体现在 `tool_summary` 块。**

  同一个问题、同一个 agent space、五分钟内三次:

  | | 第1轮 | 第2轮 | 第3轮 |
  |---|---:|---:|---:|
  | `tool_summary` 块数 | 2 | **5** | 2 |
  | 真实实验 ID(`exp-`) | 0 | **12** | 0 |
  | `injection_confirmed` 推理 | 0 | **4** | 0 |
  | FIS 模板 ID(`EXT`) | 12 | **0** | 12 |
  | 判定与图谱一致 | ✗ | **✓ 5/3/9** | ✗ |

  **第 2 轮查了图谱**,给出 `confirmed 5 / inconclusive 3 / untested 9`
  —— 与图谱实时查询**完全一致**,引用真实实验 ID
  `exp-pay-for-adoption-http-chaos-20260905` 和真实退化幅度 0.4%,
  还正确指出该实验**缺少 `injection_confirmed` 标志**、因此无法区分
  「注入生效但未传导」(应 refuted)与「注入根本没打到」(什么都没验证),
  所以**不下结论**。这条推理链质量很高。

  **第 1、3 轮没查**,改用「FIS 实验模板存在」当依据 —— 模板是**意图**,不是**结果**
  —— 而且两轮判定互不一致:SQS 第 1 轮 `inconclusive`、第 3 轮 `untested`;
  ALB 第 1 轮 `confirmed`、第 3 轮 `untested`。

  **可靠判据**(写进 `demo/fixtures/agent_unaided_answer.json`):
  引用真实实验 ID(`exp-` 前缀)且不引用 FIS 模板 ID(`EXT` 前缀)→ 用了图谱。

  ### 这个更正让论点变得更强,而不是更弱

  原来的说法(「agent 不用图谱、会编造」)既不稳健也有挑选证据的风险。
  真正的发现是:**同一个 agent、同一个问题,查了图谱的那次答案稳定、可核对、
  带实验 ID;没查的两次每次都变。** 图谱不是让它更聪明,是让答案**可核对**。

  ⚠️ **框架纪律**:这一屏**不是比谁聪明**。任何 LLM 被问一个它没有事实依据的问题
  时都会给出答案,因为它不会说「我不知道」—— **包括本项目自己的引擎**。
  写页面时不许暗示 DevOps Agent 蠢:它擅长的部分(从 CloudTrail 挖 FIS 实验
  与发起者)做得确实好,而且它**接上图谱后表现很好**。

  ### 真实对照数据(实时查,不写死)

  petsite 的 17 条依赖边:**confirmed 5 / inconclusive 3 / untested 9**。
  全图 113 条:confirmed 13 / inconclusive 14 / untested 86,
  **验证率 11.5%**,refuted 0。

  ⚠️ 判定属性名是 **`verify_status`**,不是 `verification_status`
  (我第一次查错了,得到「273 条边全无判定」的假象)。
  依赖边类型由 `_dependency_edge_labels()` 定义:
  `AccessesData / Calls / Delegates / DependsOn / Invokes / InvokesTool / Retrieves`。
  `RunsOn`(224 条)、`TestedBy`(25 条)**不是依赖边**。

  ### 待查:为什么只有三分之一的调用查了图谱

  `aws` 那条 association 有 `"status": "valid"`,**MCP 这条没有 status 字段**。
  但第 2 轮证明它**能用** —— 所以不是配置坏了,而是**模型每次自己决定要不要调**。
  这本身就是要展示的现象:**工具可用 ≠ 工具会被用**。

  ### 页面怎么做

  RCA 页加第五个 Tab「🤖 交给 Agent」,展示**三轮真实采样**:

      左:没查图谱的那两轮（依据是 FIS 模板存在，判定互不一致）
      右:查了图谱的那一轮（判定 5/3/9，带实验 ID）+ 图谱实时查询结果

  底部给**可核对的差异**,并说明判别方法(`exp-` vs `EXT`)。

  **默认展示已抓取的真实记录**(带时间、executionId、逐字原文、复现命令),
  **不做实时阻塞调用** —— 实测单次 47~59 秒,展示站上点下去转一分钟比不做更糟,
  而且回答非确定性。另给一个「重新实测」按钮供不信的人现场跑。
  图谱那一栏是**实时查的**:毫秒级、每次一样。这个不对称本身就是论据。

- [x] **T1 修 `2_Query_Catalog` 按钮无反应** ✅ 2026-09-06

  **实测结论:两个独立 bug,都无声。**

  ① **执行按钮被永久禁用。** 默认选中 `q10_infra_root_cause`(必填
  `affected_service`),而参数下拉是 `[""] + KNOWN_SERVICES` ——
  **空串在第一位所以是默认值** → `missing` 非空 →
  `disabled=bool(missing) or not online` → 禁用。四个精选「运行」的
  `qc_autorun` 路径同样要求 `not missing`,也走不通。
  AppTest 实测:`selectbox 值: ['q10_infra_root_cause', '']`、
  `('▶️ 执行查询', disabled=True)`。**页面渲染正常、零异常、零日志,
  只是所有按钮都按不动。**
  修法:必填参数不给空选项;可选参数保留(空 = 不传该参数,是有意义的语义)。

  ② **精选按钮推不动选择框。** 它只设 `qc_selected`,指望
  `st.selectbox(..., index=...)` 去读 —— 但 **widget 带 key 且 session state
  已有值时 Streamlit 忽略 `index=`**。实测四个精选按钮全部返回
  `q10_infra_root_cause` 的结果,而标称是 q20/q16/q21/q2。
  修法:直接写 selectbox 自己的 state key `qc_select_box`,并去掉 `index=`
  (两条路并存会让 Streamlit 告警且 index 不生效)。

  顺带两处:
  - dict 型结果原来一律 `st.write(rows)` 渲染成一团原始字典,既无成功提示也无
    表格 —— 而默认查询恰好就是 dict 型。改成按键分表:列表→表格、标量→指标。
  - 服务清单硬编码且含 `petadoptionshistory`(图谱里叫 `pethistory`,同一个毛病
    在 RCA 页修过)。改用 `C.service_names()`,并把有内容的服务排前面 ——
    图谱按字母序返回,第一个是 `artillery`(压测工具,边已清理),
    拿它当默认参数几乎所有查询都返回空。

  **修后实测:5/5 按钮产出结果**(q20 → 40 行、q16 → 5 行、q21 → 50 行、
  q2 → 5 行、q10 → 3 个集合),零异常、零告警。

  门禁 `tests/test_54_query_catalog_usable.py`(4 条,**已逐条反向验证**):
  m01 必填参数不得默认空选项 / m02 精选按钮必须写 selectbox 的 state key /
  m03 服务清单不得硬编码图谱里没有的名字 / m04 精选按钮点下去真的会换查询。

  ⚠️ 门禁自己先出过两处错,原因同一个:**断言匹配到了我自己写的注释**。
  m03 在正确代码上就报红(注释里写着 `petadoptionshistory`),m02 在注入回归后
  不报红(注释里出现 `qc_select_box`)。已加 `_code_only()` 先剥注释与 docstring。
  这是本会话第三次踩这个坑。

- [x] **T2 修 `8_DR_Plan` 生成不出计划** ✅ 2026-09-06

  **实测结论:三层原因,每一层都不报错、只是把内容变空。**

  ① **没设 workload profile。** 报的是
  `ProfileNotConfigured: No workload profile configured. Pass --profile
  <profile.yaml> or set DR_PROFILE. There is deliberately no default: a wrong
  profile silently produces a plan pointing at the wrong domain, SSM keys and
  namespace, which looks correct until it is executed.`
  上游**刻意不给默认值,这个设计是对的**,所以修法不是给上游加默认,
  而是让页面做**显式**选择(`profiles/petsite.yaml`)。

  ② **默认参数用虚构 AZ 名。** `apne1-az1` / `apne1-az2` / `apne1-az4` 在图谱里
  不存在(图谱是 `ap-northeast-1a` / `ap-northeast-1c` / `ap-northeast-1d`)。
  **这套虚构命名贯穿整个 `dr-plan-generator`** —— examples、fixtures、tests、
  README、README_CN、SKILL.md、AGENT.md、docs/prd.md,连 `graph/queries.py` 的
  docstring 都写着「e.g. `apne1-az1`」,而且**没有任何别名翻译层**。
  也就是说 **AZ scope 从来只在合成 fixture 上验证过**。

  ③ **即使换成真实 AZ 名,受影响服务仍是 0。** `anchors=10, nodes=394` 但零匹配 ——
  profile 声明的服务锚点里有 `petadoptionshistory` / `pethistory-service` /
  `PetAdoptionStatusUpdater` 这些图谱里不存在的名字,而 `Microservice` 节点也不
  直接挂在 AZ 上(路径是 Service→Pod→EC2→AZ)。**这是 dr-plan-generator 侧的
  锚定问题,不在本页范围内**,已另立 T11。

  实测三种 scope:

  | scope | source | 受影响服务 | 阶段 | RTO |
  |---|---|--:|--:|--:|
  | `az` | `apne1-az1`(虚构) | 0 | 2 | 6 |
  | `az` | `ap-northeast-1a`(真实) | 0 | 5 | 803 |
  | **`service`** | **`petsite`** | **6** | 4 | 61 分钟 |

  **修后实测:默认预设改为「服务故障:petsite」,点一下得到真实计划** ——
  计划 ID `dr-service-1788748447`、RTO 61 分钟、**受影响服务 6**
  (petsite / pethistory / payforadoption / petlistadoptions / petsearch / petfood)、
  零异常。

  顺带三处:
  - AZ 选项改为从图谱查 `MATCH (a:AvailabilityZone)`,不硬编码 ——
    硬编码正是 ② 的来源。
  - 受影响服务为 0 时**显式告知**并给出上面那张对照表。空计划有计划 ID、
    有 RTO/RPO、有阶段,唯独没有内容 —— **比报错更容易误导人**。
  - `估算 RPO` 原来渲染成 `None 分钟`。生成器返回 None 是**刻意**的
    (`RPO cannot be derived from configuration (aurora, dynamodb, s3, sqs).
    The plan will say so rather than print a number that cannot be justified.`),
    渲染成 `None 分钟` 把这份诚实抹成了一个 bug。改为显示「无法推导」+ 解释。

  门禁 `tests/test_55_dr_plan_usable.py`(7 条,**m01/m02/m04/m07 已反向验证**):
  m01 必须显式设 workload profile / m02 默认参数不得是虚构 AZ 名 /
  m03 AZ 选项必须从图谱取 / m04 默认预设必须是实测能出计划的那个 /
  m05 受影响服务为零时必须明确告知 / m06 无法推导的 RPO 不得渲染成 None /
  m07(需活图谱)默认场景真的出得来计划。

  ⚠️ m02 第一版又是「断言匹配到自己的说明文字」—— 页面上解释「为什么 AZ scope
  算不出来」的告警里必然引用 `apne1-az1`,而 `EXAMPLE_PLAN_PATH` 指向的示例产物
  文件名也含它。改为只盯**参数赋值位置**(`default_source=` / `"source":` /
  `value=`),不盯说明文字。**本会话第四次踩这个坑** —— 判据要对准语法位置,
  不是对准字符串。

- [x] **T4 改 `4_Smart_Query` 布局** ✅ 2026-09-07

  **实测结论:引擎本来就是好的,问题纯在布局。**

  交互验证时它返回了真实答案(「petsite 依赖 2 个数据库」带表格、
  「20 条下游依赖,涵盖 5 类关系」),所以不是功能问题。

  **根因不是 `chat_input` 的位置。** 它在顶层**总是固定在页面底部**,
  这是 Streamlit 的行为也是聊天界面的常规,不该改。真正的问题是
  **它和对话之间塞了东西**:

      标题 / 引擎状态
      对话历史                  ← 答案在这里
      并排对比开关
      [chat_input 固定在底部]
      新一轮问答渲染
      契约 few-shot 语料（过滤输入框 + N 个 expander）← 又长又占地
      空状态引导                ← 第一次打开的人根本滚不到

  few-shot 那一大块渲染在对话之后,把答案顶到很上面、输入框压到很下面,
  中间隔几十行 —— 这就是用户说的「对话框在最下面,被很多内容隔开,答案在最上面」。

  **修法:主区只放对话。**
  - 并排对比开关 → 侧栏(它是**配置**,不是内容)
  - 契约 few-shot 语料 → 侧栏折叠区(离线时它是主材料,所以不删,只收起;
    离线自动展开)
  - 空状态引导 → 移到对话**上方**(原来在页面最底,第一次打开滚不到)

  主区现在自上而下只有:**引擎状态 → 空状态引导 → 对话 → 输入框(固定底部)**,
  答案永远紧贴输入框上方。

  **「对话框上灰色的,看着好像坏了」** = `disabled=not ONLINE` 禁用了却不解释。
  现在离线会显式说「底部输入框是禁用状态(**不是坏了**)」并指向侧栏的语料。

  修后实测:0 异常、`chat_input` 未禁用、对比开关已入侧栏、
  点示例问题仍返回真实表格答案。

  门禁 `tests/test_56_smart_query_layout.py`(5 条,**m02/m03/m04 已反向验证**):
  m01 few-shot 必须在侧栏 / m02 对比开关必须在侧栏 /
  m03 空状态引导必须在对话之前 / m04 离线必须解释输入框为什么是灰的 /
  m05 主区不得出现顶层 `st.subheader`。

- [x] **T0 在 RCA 页加「工具可用 ≠ 工具会被用」对照** ✅ 2026-09-07

  ### 最终结论(n=12 次真实调用)

  | 提问方式 | 查了图谱 | 比例 |
  |---|---|---|
  | 不提图谱(原问题) | 2/8 | **25%** |
  | 点名要求查图谱 | 4/4 | **100%** |

  **「为什么只有三分之一」的答案是:取决于怎么问。**
  工具是好的、关联是好的、一调就准 —— 模型只是**不主动去拿**。
  没查的那些调用一样成功返回、排版精美、语气自信。

  查了图谱的那些:引用真实实验 ID(`exp-pay-for-adoption-http-chaos-20260905`)、
  真实退化幅度(0.4%),并在证据不足时**主动拒绝下结论** ——
  原话:「无法区分『依赖不传导』与『注入根本没打到』,不能证伪,故不下结论」。
  这正是本项目的判定纪律,而它不是被教会的,是**图上的数据本身逼出来的**。

  没查的那些:依据是「FIS 实验模板存在」。**模板是意图,不是结果** ——
  它说明有人打算测,不说明测过了、更不说明测出了什么。
  而且彼此矛盾:SQS 一轮 `inconclusive` 一轮 `untested`;
  ALB 一轮 `confirmed` 一轮 `untested`。

  ### 判据(三版,前两版都错)

  | 版本 | 判据 | 结果 |
  |---|---|---|
  | v1 | 数 `contentBlockStart` 里 `tool_use` 类型的块 | **恒为 0** → 误判「从不查图谱」 |
  | v2 | 引用 `exp-` 前缀实验 ID | 漏两次(写成「HTTP chaos · 2026-09-05」)→ 1/8 而非 2/8 |
  | **v3** | **退化幅度数字 + `injection_confirmed`** | **零重叠、零残余歧义** |

  v3 为什么可靠:**退化幅度数字是类别性分离的** ——
  查了图谱的引用 20~36 个实测百分比,没查的**恰好 0 个**,
  因为 AWS 控制面里没有这个数,它只存在于故障注入实验结果里。
  `injection_confirmed` 同理,那是**边上的属性名**,控制面看不到。

  ### 页面实现

  `demo/pages/6_Root_Cause_Analysis.py` 第五个 Tab「🤖 交给 Agent」,六节:
  ① 框架说明(不是比谁聪明)② 12 次采样的比例 ③ 判别方法 + 我踩过的两个坑
  ④ 两类回答逐字对照 ⑤ **图谱侧实时查询**(毫秒级、每次相同)
  ⑥ **这个结果指出我们自己要修的东西**

  证据文件 `demo/fixtures/agent_unaided_answer.json`:
  12 轮逐字全文 + executionId + 耗时 + 信号 + 判别规则 + 复现命令。

  **默认不做实时阻塞调用** —— 实测单次 44~205 秒,展示站上转一分钟比不做更糟,
  且回答非确定性。图谱那栏实时查,这个不对称本身就是论据。

  门禁 `tests/test_57_rca_agent_tab.py`(6 条,**m01~m06 全部反向验证**):
  m01 证据必须是真实调用记录且有对照组 / **m02 比例必须从证据现算不能写死** /
  m03 必须保留「不是比谁聪明」的框架 / m04 必须公开判别方法与踩过的坑 /
  m05 图谱侧必须实时查不能读快照 / m06 必须承认这个结果指向我们自己要修的东西

  ⚠️ m02 上线前就抓到一处真实问题:我把 25% 写死在第 6 节文案里了。

- [x] **T13 把自发查询率从 25% 提到 100%** ✅ 2026-09-07

  ### 结果:同一个问题,25% → 100%

  | 组 | 条件 | 查了图谱 |
  |---|---|---|
  | ① 基线 | 不提图谱,无 skill | 2/8 = **25%** |
  | ② | 问题里点名要求查图谱 | 4/4 = **100%** |
  | ③ | **注册 skill 后**,问题与 ① 一字不改 | 8/8 = **100%** |

  而且第 ③ 组里 **FIS 模板 ID 引用共 0 次** ——
  「拿模板存在推断验证状态」这个失败模式彻底消失。

  ### 根因是纪律写错了层

  原先写在 MCP `initialize.instructions` 与工具描述里,两处都明确写着
  「讨论任何依赖关系之前先调它」,比例仍上不去。因为这个 agent 每次调用都先
  `load_skill`,而 agent space 原有 7 个 skill 没一个讲依赖图谱;
  唯一提到 Neptune 的那条 memory 把它描述成**基础设施清单**。

  ### 落地

  `mcp/agent_skill/dependency-verification-graph.md`(七条规程)注册为 skill 资产:
  `ki-4b88354a-6e4c-4032-a2d4-0e9d08968167`,metadata 照现有 skill 的形状
  (`agent_types=[GENERIC]` / `skill_type=USER` / `status=ACTIVE`),
  content 用 `{'file': {'path': ..., 'body': {'text': md}}}`。

  ⚠️ **通用教训比这个数字重要**:把 agent 该遵守的纪律放在**协议字段**里,
  不等于它会读到。得放进那个 agent **实际先读的那一层** —— 对 AWS DevOps Agent
  是 skill / agents_md 资产,对别的宿主可能是别的地方。**先测,再改,再测。**

  ### 诊断:两条我们控制的通道都已经写对了,仍然只有 25%

  | 通道 | 现状 | 实测有效性 |
  |---|---|---|
  | MCP `initialize.instructions` | 规则 2 明确写着「**依赖关系必须先查再说**」 | ❌ 25% |
  | 工具描述 | `q22` 描述里写着「**讨论任何依赖关系之前先调它**」 | ❌ 25% |

  **所以问题不是话说得不够狠,是话没进到它会读的那一层。**

  ### 根因:这个 agent 是 skill 优先架构

  12 轮采样里**每一轮都有 `load_skill` 块**。查 agent space 的资产:

      skill 7 个 / memory_store 5 个 / memory 27 个 / artifact 1 个
      agents_md 0 个   ← 论断里说的「AGENTS.md v2」不在这里

  **7 个 skill 没有一个讲依赖图谱。** 实际加载的是
  `understanding-agent-space`(v10)、`chat-tool-use-best-practices`(v8)、
  `tool-use-best-practices` —— 这些赢过了 MCP 协议字段。

  唯一提到 Neptune 的是 memory `components/neptune-graph-platform`(v1,09-02),
  但**解压后确证**它把 Neptune 描述成**基础设施清单**(集群、ETL Lambda、
  事件管道),完全没提故障注入验证、`verify_status`、q22/q23 或 MCP 工具。

  ⚠️ 这里我又差点踩坑:第一次的关键词检查是对 **zip 压缩字节**做的,
  当然全是 False。**必须解压后再判。**(本会话第七次同族测量错误。)

  ### 杠杆:注册一个 skill 资产

  `ListAssetTypes` 里 `skill` 的定义是
  「Reusable instructions that extend agent capabilities」——
  **这正是它先读的那一层。**

  内容已写好并入库(可评审、可版本控制):
  `mcp/agent_skill/dependency-verification-graph.md`

  七条规程:先查再说 / 判定四态含义 / 零退化≠依赖不成立 /
  不编造图谱没返回的数值 / 观测层与干预层不混用 / 单一观测源降权 / 边方向。
  并明确写上「**不要用 FIS 模板的存在推断验证状态**」——那是实测到的具体失败模式。

  ### ⚠️ 但这一步是对共享资源的写操作,需要你批准

      aws devops-agent create-asset \
        --agent-space-id 60c2f48f-b6e3-4dce-a0a3-4144228b2051 \
        --asset-type skill --content <上面那份 md>

  `petsite-devops` 里已有 40 个资产、5 个 AgentCore runtime 在用它
  (`WaggleAI*` 那几个)。加一个 skill 是**新增、可逆**(`DeleteAsset`),
  但它会改变所有人用这个 agent space 时的行为,所以我不擅自做。

  批准后的验收方式(已有度量手段):注册 → 用**原问题**(不提图谱)重跑 8 次 →
  与 25% 基线对比。升不升一目了然。

- [x] **T14 用 v3 判据复核本项目自己的「agent 编造」论断** ✅ 2026-09-07

  ### 结论:这条论断在本仓库无法核实,已从承重位置降级

  | 核查项 | 结果 |
  |---|---|
  | 进入仓库时间 | **2026-09-05**(`59f41b5`),散文形式,**未随附任何原始证据** |
  | 引用的「AGENTS.md v2」 | 仓库里没有;`petsite-devops` 里**也没有 `agents_md` 资产** |
  | 2026-09-01 的记录 | `todo/` 最早是 09-04,**没有** |
  | 对话记录 / executionId / 指标名 / iowait 数值 | **全无** |
  | 发生地 | 另一个系统(SAP)、另一个 agent space |

  按项目自己的标准,这是**一条不可证伪的论断被放在承重位置**。
  讽刺很精确:`mcp/provenance.py` 一边告诉 agent「不要编造本 server 没有返回的
  数值」,一边用一个没人能核对的数字论证这条规则。

  **对比说明这个标准是可达的**:agent space 里的
  `components/neptune-graph-platform` memory 带着 `claim / evidence / source`
  结构和 `devopsagent:execution` ID。这条旧记录只是没达到。

  ### 修法:降级为标注过的轶事,承重位置换成可核实的数据

  改了两个**随代码发布、会喂给 agent 的活文档**:
  `mcp/provenance.py` 与 `mcp/README.md` —— 承重证据换成 2026-09-07 那 12 次采样
  (带 executionId、可用 `list-pending-messages` 逐条取回),旧记录保留但明确标注
  「无法核实,不作为论据」。

  `todo/` 下另外 4 处引用是**带日期的历史快照**,按纪律不改写。

  它可能是真的 —— 但**拿证据说话的项目,自己的证据也要能被证伪。**

- [x] **T3 `3_Graph_Explorer` 分层布局 —— 复核后判定无需再修** ✅ 2026-09-07

  ### 用户报的两个症状在已提交代码里都已解决

  数值核实方式:拦截 `C.embed_html` 捕获生成的 HTML,解析 vis.js 节点坐标。
  比截图可靠 —— Streamlit 只渲染视口,其内部容器无法被 playwright 滚动,
  图在首屏以下截不到(已知限制)。

  | 截图报的症状 | 实测现状 |
  |---|---|
  | 节点挤成两排 | **4 层**:y=-135(上游调用者 2)/ 0(锚点 1)/ 230(10)/ 310(6) |
  | 标签截断 | **0 个截断**,最长 18 字,0 个含换行 |
  | tooltip `AccessesData · 未验证` | **不是 bug** —— `untested` 就该显示「未验证」,是诚实行为 |

  x 跨度 82.5~1697.5,分布开阔。19 个节点、0 异常。

  ### 为什么用户看到的是坏的

  **线上跑的是手工拷贝的旧代码。** 并发会话已把这页重设计成
  「预置场景(依赖验证现状 / 应用到数据层 / Agent 依赖 / 服务间调用)
  + 1 跳邻域默认 + 节点上限 30」,petsite 是 17 节点 / 16 关系 —— 不是截图里那团星形。
  布局代码里还留着针对该截图的注释(「截图里 ssm/sts 压在绿柱上就是这个」)。

  ⚠️ **所以这一项的结论是:不要再改代码,去部署。** 在已经修好的东西上继续改
  是凭空造工作量。唯一待观察的是 1697px 宽度是否超出容器需要横向拖动 ——
  那要真人看,部署后再判。

- [ ] T3b 部署后确认图布局在真实浏览器里的观感(1697px 宽度是否需要横向拖动)
- [x] T4 改 `4_Smart_Query` 布局 ✅ 2026-09-07（详见上方 T4 条目）
- [ ] T5 查 `9_Interactive_Explorer` 为何是空页(Cytoscape 组件未加载?)
- [ ] T6 决定 `9_Interactive_Explorer` 与 `3_Graph_Explorer` 合并还是删一个
- [ ] T7 决定 `2_Query_Catalog` 并入 `4_Smart_Query` 还是独立保留
- [ ] T8 改写 `app.py` 首页导语,把「这张图是真的吗」这条论证线摆前面
- [x] T9 每修一个都加守门测试 ✅ 2026-09-07 —— 本会话新增 test_54/55/56/57/58 共 25 条，**全部做过反向验证**
- [x] T10 全量测试 + 部署 + 线上逐页复验 ✅ 2026-09-07 —— 8+2 个文件分三批部署，每批编译校验通过才重启；线上 md5 与本地 HEAD 逐一核对一致；十页 AppTest 全 0 异常，线上 journal 0 traceback、0 重启。**遗留 1 条测试失败不属于本线**：`test_s6_02` 报 `RoutesToRuntime`/`RoutesVia` 在 Neptune 不存在，那是并发会话的迁移期已知状态（`profiles/petsite.yaml:493` 自己写着「删除要等 ETL 部署+存量清理之后」），不要动它。
- [ ] T11 `dr-plan-generator` 的 AZ scope 锚定在真实图谱上匹配不到任何服务

  **不是 demo 页的问题,单独立项。** 实测 `anchors=10, nodes=394` 但零匹配。
  两层原因:

  ① `profiles/petsite.yaml` 声明的服务名里有图谱里不存在的:
  `petadoptionshistory`(图谱是 `pethistory`)、`pethistory-service`、
  `petadoptionstatusupdater` / `PetAdoptionStatusUpdater`(图谱是 `petstatusupdater`)。

  ② `Microservice` 节点不直接挂在 `AvailabilityZone` 上 ——
  路径是 `Service→Pod→EC2→AZ`。若锚定只查一跳,AZ 子图里根本没有 Microservice。

  另外整个 `dr-plan-generator` 用的 `apne1-az1` 这套 AZ 名在图谱里不存在,
  且无翻译层 —— 它的 examples / fixtures / tests / docs 全是这套名字,
  **说明 AZ scope 从来没在真实图谱上跑过**。

  ⚠️ 动 `profiles/petsite.yaml` 要小心:它是共享契约文件,并发会话可能在改。

  顺带记录几条噪音(不影响结论但值得清):
  `Unknown resource type 'Subnet'/'ECRRepository'/'AgentRuntime'/'AWSServiceEndpoint'`
  —— registry 不认识这些类型,而它们在图谱契约里是有的;
  `Cycle detected in layer during topological sort; 6 nodes not reachable`。

## 四、纪律(本会话已付学费)

1. **门禁必须做反向验证** —— 注入一次已知回归,确认它真的会红。
   本会话我自己写出过三次恒绿门禁(缺陷 #43)。
2. **AppTest 全绿 ≠ 渲染正确** —— 它跑离线路径、喂干净桩数据。
   改完必须在真实数据下跑一遍,能截图就截图。
3. **首屏没异常 ≠ 功能可用** —— 用户报的三个问题全在交互之后。
4. **不要假设,去比对** —— 部署前 md5 比对线上与本地,别信「应该是一样的」。
5. **改共享层/共享文件前先查当前最高版本** —— 缺陷 #42 是我覆盖了并发会话的改动。
6. **并发会话在同一仓库活跃** —— `demo/pages/3_Graph_Explorer.py` 与
   `demo/fixtures/*.json` 有它们的未提交改动,动之前先看 `git status`。

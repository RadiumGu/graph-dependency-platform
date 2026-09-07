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

- [ ] **T0(最高优先级)在 RCA 页加「同一问题:接图谱 vs 不接图谱」对照**

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

  ### 实测它没用图谱 —— 而这正是最有说服力的地方

  问它:「petsite 依赖哪些服务?对每一条给出故障注入验证状态与依据。
  不要编造数字,没有数据就说没有。」

  它返回排版精美、信心十足的表格,大部分标 `confirmed`。事件流里是
  `load_skill` + `tool_summary`,**没有一个图谱工具调用**。它自己说
  「完全基于已知的架构文档和 FIS 实验模板配置」,而判 `confirmed` 的依据是:

  > 实验模板存在且有明确的目标资源和停止条件,说明依赖已通过故障注入验证路径建立

  **「FIS 模板存在」不等于「这条依赖被验证过」。** 按本项目判据,`confirmed`
  要求在依赖目标端注入故障、观测源端退化 ≥20%。对照真实图谱(`q23`):

  | | Agent 的说法 | 图谱实测 |
  |---|---|---|
  | Aurora / SQS / DLQ / Lambda / EKS / 网络 / ALB | 基本都 `confirmed` | 119 条边:confirmed **13** / inconclusive 14 / untested **92** |
  | 验证率 | 未提,暗示很高 | **10.92%** |

  与 `mcp/README.md` 记的 2026-09-01 那次同型:DevOps Agent 从 CloudTrail 挖 FIS
  实验做得很好,但**编造了 CWAgent 指标值**、用**虚构的 iowait 数字**排除了存储
  瓶颈。**LLM 被问「什么依赖 X」时一定会给答案,因为它不会说「我不知道」。**

  ### 待查:关联了为什么没调用

  `aws` 那条 association 有 `"status": "valid"`,**MCP 这条没有 status 字段**。
  怀疑未验证/未激活,或 OAuth client credentials 未配好。这是本任务第一个待查点。

  ### 页面怎么做

  RCA 页加第五个 Tab「🤖 交给 Agent」,同一问题两栏并排:

      左:DevOps Agent 直接回答（不接图谱）
      右:同一问题 + 图谱查询结果作为证据

  底部给一行**可核对的差异**:agent 说 N 条 confirmed,图谱说 M 条,
  并列出分歧的具体边。这一屏是整站最短的价值证明 ——
  **不是宣称图谱有用,而是当场让人看见不用图谱会得到什么。**

  ⚠️ 实现纪律:agent 回答必须标明「未经图谱核验」,不能与图谱数据混在一起呈现 ——
  那正是本项目反对的事。

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

- [ ] T3 修 `3_Graph_Explorer` 分层布局(用户截图:节点挤成两排、标签截断)
- [ ] T4 改 `4_Smart_Query` 布局:输入框置顶或固定,答案紧随其后
- [ ] T5 查 `9_Interactive_Explorer` 为何是空页(Cytoscape 组件未加载?)
- [ ] T6 决定 `9_Interactive_Explorer` 与 `3_Graph_Explorer` 合并还是删一个
- [ ] T7 决定 `2_Query_Catalog` 并入 `4_Smart_Query` 还是独立保留
- [ ] T8 改写 `app.py` 首页导语,把「这张图是真的吗」这条论证线摆前面
- [ ] T9 每修一个都加守门测试(本会话反复的教训:没有门禁的东西会漂回去)
- [ ] T10 全量测试 + 部署 + 线上逐页复验
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

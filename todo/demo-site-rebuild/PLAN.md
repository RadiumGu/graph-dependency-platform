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

- [ ] T2 修 `8_DR_Plan` 生成不出计划
- [ ] T3 修 `3_Graph_Explorer` 分层布局(用户截图:节点挤成两排、标签截断)
- [ ] T4 改 `4_Smart_Query` 布局:输入框置顶或固定,答案紧随其后
- [ ] T5 查 `9_Interactive_Explorer` 为何是空页(Cytoscape 组件未加载?)
- [ ] T6 决定 `9_Interactive_Explorer` 与 `3_Graph_Explorer` 合并还是删一个
- [ ] T7 决定 `2_Query_Catalog` 并入 `4_Smart_Query` 还是独立保留
- [ ] T8 改写 `app.py` 首页导语,把「这张图是真的吗」这条论证线摆前面
- [ ] T9 每修一个都加守门测试(本会话反复的教训:没有门禁的东西会漂回去)
- [ ] T10 全量测试 + 部署 + 线上逐页复验

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

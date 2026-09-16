# North Star — 图数据库作为系统依赖关系的唯一源头

> 本文件是循环的**不变目标**。每轮推动消息都会让代理重读此文件。
> 修改本文件等于改变目标,请谨慎。
> 锚目录:`/home/ec2-user/works/graph-dependency-platform/todo/goal-loop/`

---

## 1. 目标陈述(用户原话)

> 把图数据库作为系统依赖关系的**唯一源头**,并可以管理**动态和静态**的依赖,
> 处理依赖关系的**退化和变化**,并可以被**各种 agent 快速调用**。

四个子目标及 2026-08-28 评估基线:

| # | 子目标 | 基线达成度 |
|---|---|---|
| 1 | 图数据库作为依赖关系唯一源头 | ~60% |
| 2 | 管理动态与静态依赖 | ~55% |
| 3 | 处理依赖的退化与变化 | ~40% |
| 4 | 被各种 agent 快速调用 | ~55% |

完整评估证据见 `../design-goals-assessment_20260828-1705.md`。

---

## 2. Definition of Done(可用 shell / 查询验证)

循环仅在**全部**满足时停止。每轮先按序自查这 5 条。

### DoD-1 — 依赖边会失效(目标 3 · 变化)

```bash
# 陈旧超 7 天的 Calls 边必须已被标记 active=false 或删除
python3 ~/.kiro/crew/scratch/nq.py \
  "MATCH ()-[r:Calls]->() WHERE r.active = true AND r.last_seen < timestamp()/1000 - 604800 RETURN count(r) AS stale_active"
# 通过条件:stale_active = 0
```

附加:所有**活跃**的 `Calls` 边必须带 `first_seen`。

```bash
python3 ~/.kiro/crew/scratch/nq.py \
  "MATCH ()-[r:Calls]->() WHERE r.active = true RETURN count(r) AS active_total, count(r.first_seen) AS with_first_seen"
# 通过条件:active_total == with_first_seen
```

> **2026-08-28 cycle-4 收窄**:本条原先要求**全部**边都带 `first_seen`,
> 但那在设计上不可达 —— DeepFlow 不再观测到的陈旧边根本不会进入 upsert 流程,
> 因此拿不到 `first_seen`,只会被对账标记 `active=false` 并最终删除。
> 一条即将失效的边缺 `first_seen` 不是缺陷。
>
> 也**刻意不用 `last_seen` 回填**存量边:`first_seen <= last_seen` 恒成立,
> 把 `last_seen` 写成 `first_seen` 等于断言"依赖是那时才出现的",
> 比留空更具误导性。存量边的 `first_seen` 语义是
> **「自埋点起首次观测到」**,非真实首现时间。

### DoD-2 — 韧性反馈闭环真正闭合(目标 3 · 退化)

```bash
cd /home/ec2-user/works/graph-dependency-platform
# 写入方与读取方使用同一属性名,不再三向分裂
grep -rn "resilience_score" chaos/code/runner/graph_feedback.py \
  chaos/code/runner/neptune_helpers.py chaos/code/agents/learning_direct.py
# 通过条件:三处属性名完全一致
```

```bash
# 活图中读取方查的属性必须真实存在且能取到值
python3 ~/.kiro/crew/scratch/nq.py \
  "MATCH (n:Microservice) WHERE exists(n.resilience_score) AND exists(n.last_chaos_test) RETURN count(n) AS with_score_and_time"
# 通过条件:with_score_and_time > 0
```

> **2026-08-28 cycle-2 更正**:本条原先检查 `chaos_last_verified`,那是我写 DoD 时
> 凭空取的名字。活图实际的时效标记是 **`last_chaos_test`**(6 个节点,由
> `graph_feedback.py:_update_node` 一直在写)。再引入第三个属性名,恰好就是
> 本条 DoD 要消除的那类错误,故改为校验既有属性。

### DoD-3 — 静态与动态依赖可区分(目标 2)

```bash
python3 ~/.kiro/crew/scratch/nq.py \
  "MATCH ()-[r]->() WHERE type(r) IN ['Calls','DependsOn','AccessesData'] RETURN count(r) AS total, count(r.dependency_kind) AS with_kind"
# 通过条件:total == with_kind
```

```bash
cd /home/ec2-user/works/graph-dependency-platform
grep -n "dependency_kind" profiles/petsite.yaml rca/neptune/neptune_queries.py
# 通过条件:schema 已声明该属性,且 q1/q3 支持按它过滤
```

### DoD-4 — 图谱有对外调用契约(目标 4)

```bash
cd /home/ec2-user/works/graph-dependency-platform
test -s rca/.mcp.json && python3 -c "
import json; d=json.load(open('rca/.mcp.json'))
assert d.get('mcpServers'), '.mcp.json 仍为空'
print('registered:', list(d['mcpServers']))
"
# 通过条件:非空,且注册了图谱查询 server
```

### DoD-5 — 唯一源头不再漂移(目标 1)

```bash
cd /home/ec2-user/works/graph-dependency-platform
# 硬编码副本已清除:只看真正的赋值语句,排除注释与测试
grep -rnE "^[^#]*\b(SERVICE_FUNCTION_MAP|SVC_TO_CW)\s*[:=]" --include="*.py" rca/ | grep -v test
# 通过条件:无输出(已统一走 ServiceRegistry / profile)
```

> **2026-08-28 cycle-5 修正**:原检查是裸 `grep -rn "SERVICE_FUNCTION_MAP\|SVC_TO_CW"`,
> 会把「说明该字典已被移除」的注释也算成违规,导致改对了却判不通过。
> 现改为只匹配行首非 `#` 且带 `=` / `:` 的赋值形式。
> (同 cycle-2 的 DoD-2:验收标准本身写错,与被测代码无关。)

```bash
cd /home/ec2-user/works/graph-dependency-platform
# 已发生的漂移已修复:只看代码行(排除注释),活图无 petadoptionshistory / petfood 作为规范名
grep -nE "^[^#]*['\"](petadoptionshistory|petfood)['\"]\s*[,:)]" \
  infra/lambda/rca_window_flush/config.py | grep -v "aliases"
# 通过条件:无输出
```

> **2026-08-28 cycle-5 修正**:与第一条同样的问题 —— 原检查是裸 grep,
> 会命中「说明该漂移已修复」的注释行,以及 `# 加入 aliases（如 petadoptionshistory ...）`
> 这类正确的别名说明。现只匹配代码中作为字符串字面量出现的位置。

```bash
# YAML ↔ 活图一致性校验测试存在且通过
cd /home/ec2-user/works/graph-dependency-platform && ls tests/ | grep -i "live.*schema\|schema.*live"
# 通过条件:存在该测试文件
```

---

## 3. 问题发现来源(每轮无卡可领时执行)

1. `tasks.md` 中状态为 `todo` 且无未完成依赖的条目
2. `../design-goals-assessment_20260828-1705.md` 第 7 节的 P0→P3 清单
3. 活图实测:重跑 DoD 的各条查询,任何未通过项即为新卡来源
4. 生产日志:`gp-window-flush` 与 `petsite-rca-engine` 的 `[WARNING]` / `[ERROR]`
   ——本项目历史上 11 个缺陷里 9 个是静默失败,日志是主要发现渠道
5. `grep -rn "TODO\|FIXME\|XXX" --include="*.py" rca/ chaos/ infra/lambda/ dr-plan-generator/`

发现新问题 → 追加到 `tasks.md`,不要直接开始改。

---

## 4. 操作不变量(硬约束,不得违反)

- **禁止** `git push` 到 main / master / mainline。特性分支需显式命名分支
- **禁止**直接读凭证文件(`~/.aws/*`、`~/.ssh/*` 等)
- **禁止**破坏性操作:`DROP TABLE`、`rm -rf /`、删除生产资源等,一律先问用户
- **生产 Lambda 只用 `update-function-code` / `update-function-configuration`**,
  绝不对生产跑完整 `deploy.sh`(其 Step 3 会按 `.env` 重写全部 12 个环境变量,
  Step 4-5 会创建/订阅 SNS 主题)
- **改生产前先在 canary 上验证**。本项目历史:首次直接部署生产连撞两个缺失依赖
  并导致回滚;改用 canary 后 6 个缺陷全部在 canary 暴露,生产零影响
- **IAM 用新增内联策略,不改写既有策略**,以便单独回滚
- **一轮 = 一个原子步骤**(≤5 次工具调用),不要一轮做完多个卡
- **改依赖或环境变量前先读全量再回写**,避免整体替换丢键
- 每轮结束在对应卡片追加 `<UTC 时间> cycle-<n>: <动作> <结果>`

---

## 5. 停止条件(只有两种)

1. **目标达成** — 第 2 节 5 条 DoD 全绿 → 调 `autonudge_stop(reason="DoD met")`
2. **不可恢复的基础设施故障** — 磁盘满、网络分区、凭证提供方连续 3 轮不可用、
   内核 OOM 等代理无法绕开的问题 → 记录一行诊断后 `autonudge_stop(reason="infra: <原因>")`

**其余一切都是待解决的问题,不是停止理由。** 明确不算停止条件的情况:

- 测试失败、构建报错、lint 报错 → 修掉,这就是工作本身
- "我不知道怎么做" → 读代码、grep、查日志、跑小探针、在 `tasks.md` 加一张调研卡
- 某张卡看起来被阻塞 → 拆小、解依赖、或标明阻塞后换一张卡
- 工具报错 → 读错误、纠正调用方式、重试
- 目标看起来做不到 → 重读本文件、拆解成更小的卡、重跑发现流程
- 同一方法已试两次 → 换第三种;绝不因"试过两次"而停
- 轮次预算快用完 → 继续工作,`max_cycles` 由服务强制,不由代理判断

**手动急停**:创建停止哨兵文件

```
/home/ec2-user/works/graph-dependency-platform/todo/goal-loop/STOP
```

注意:该哨兵仅在循环创建时 `stop_sentinel_path` 非空才生效。
若循环是用 `monitor_start` MCP 工具起的(该工具无此参数),
请改用 🎯 弹层停止,或让代理调 `autonudge_stop`。

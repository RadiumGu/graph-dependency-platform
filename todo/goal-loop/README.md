# goal-loop 锚目录 — 使用说明

> 位置:`/home/ec2-user/works/graph-dependency-platform/todo/goal-loop/`
> 创建时间:2026-08-28 17:10 UTC

---

## 目录内容

| 文件 | 作用 | 谁修改 |
|---|---|---|
| `north_star.md` | **不变目标** + 5 条可验证 DoD + 发现来源 + 操作不变量 + 停止条件 | 仅用户 |
| `roadmap.md` | 阶段 0→3 + 阶段 X 遗留欠项 + 明确不做的事 | 用户为主,代理可提议 |
| `tasks.md` | 任务看板(T-001…T-093),**代理每轮必须更新** | 代理 |
| `README.md` | 本文件:如何武装循环 | — |
| `STOP` | 停止哨兵(**尚未创建**;创建它即请求停止) | 用户 |

文件名 `north_star.md` / `roadmap.md` / `tasks.md` 是**刻意**按推动消息模板取的
—— 消息里写了 "Your north star is in north_star.md, roadmap in roadmap.md,
tasks in tasks.md",文件名不匹配会导致每轮都找不到文件。

---

## 如何武装(推荐:🎯 弹层)

在 Kiro Crew 仪表盘点 🎯 →「设定目标」,把下面这段**原样粘贴**进「目标描述」:

```
Your north star is in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop/north_star.md, roadmap in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop/roadmap.md, tasks in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop/tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create /home/ec2-user/works/graph-dependency-platform/todo/goal-loop/STOP
```

用绝对路径而非相对路径,因为循环唤醒时的工作目录不保证是本目录。

建议参数:

| 参数 | 建议值 | 理由 |
|---|---|---|
| 间隔 | **600 秒**(10 分钟) | 本项目单个原子步骤常含构建+部署+等窗口过期(缓冲窗口 120s + flush + RCA 约 40s),间隔太短会在上一轮还没验证完就催下一轮 |
| 轮次上限 | **30** | 阶段 0+1 约 8 张卡,按每卡 2–3 轮估算 |
| 停止哨兵 | 上面那个 `STOP` 绝对路径 | **必须非空**,否则哨兵文件被忽略 |

---

## 备选:用 `monitor_start` MCP 工具

代理也可以自己起循环,但有一个**实质差异**:`monitor_start` 没有
`stop_sentinel_path` 参数,因此 `STOP` 文件**不会被服务检查**。
那种情况下停止只能靠:

- 让代理调 `autonudge_stop`
- 或在 🎯 弹层里手动停

若用这条路,推动消息里仍应保留 STOP 检查指令 —— 代理会自己 `ls` 该文件,
只是不如服务级检查可靠。

---

## 每轮期望行为

1. 先查停止条件:`STOP` 是否存在、5 条 DoD 是否全绿 → 是则 `autonudge_stop`
2. 从 `tasks.md` 领一张 `todo` 且无未完成依赖的卡,改 `doing`
3. 无可领卡时,跑 `north_star.md` 第 3 节的发现来源,把新问题追加为卡
4. 执行**一个原子步骤**(≤5 次工具调用)
5. 更新 `tasks.md`:状态 + 一行 `<UTC> cycle-<n>: <动作> <结果>`
6. 只在 DoD 达成 / 硬阻塞 / STOP 触发时在聊天里说话,否则保持安静

---

## 关于阻塞

`north_star.md` 第 5 节的停止条件只有两条(目标达成、不可恢复的基础设施故障)。
测试失败、构建报错、不知道怎么做、试过两次没成 —— **都不是停止理由**。

真遇到需要用户决策的事(例如要动生产数据、要改 tier0 库、要推送到受保护分支),
把卡改 `review` 并**只提一次** blocker,然后**换另一张卡继续**,不要空转等待。

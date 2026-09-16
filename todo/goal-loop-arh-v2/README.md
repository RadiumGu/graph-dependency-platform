# goal-loop 锚目录 — ARH v2 纳管

> 位置:`/home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/`
> 创建:2026-08-29 06:15 UTC

## 目录内容

| 文件 | 作用 | 谁修改 |
|---|---|---|
| `north_star.md` | 不变目标 + 5 条可验证 DoD + 实测事实表 + **成本** + 操作不变量 + 停止条件 | 仅用户 |
| `roadmap.md` | 服务切分决策 + 阶段 0→4 + 阶段 X 遗留 + 明确不做 | 用户为主,代理可提议 |
| `tasks.md` | 任务看板(T-001…T-094),**代理每轮必须更新** | 代理 |
| `exports/` | ARH 拓扑边 / 依赖 / findings 的 JSON 导出(阶段 4 产出) | 代理 |
| `coverage-ledger.md` | 覆盖率台账(阶段 4 产出,DoD-2 的核验对象) | 代理 |
| `verify_dod.sh` | 5 条 DoD 的可执行核验脚本(阶段 4 产出) | 代理 |
| `STOP` | 停止哨兵(**尚未创建**;创建它即请求停止) | 用户 |

文件名 `north_star.md` / `roadmap.md` / `tasks.md` 是刻意按推动消息模板取的 ——
消息里写了 "Your north star is in north_star.md, roadmap in roadmap.md, tasks in tasks.md",
文件名不匹配会导致每轮找不到文件。

## 武装方式

推动消息(绝对路径,因为循环唤醒时的工作目录不保证是本目录):

```
Your north star is in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/north_star.md, roadmap in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/roadmap.md, tasks in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/tasks.md. Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md. Post a blocker ONCE if genuinely stuck. To halt the loop, create /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/STOP
```

建议参数:

| 参数 | 建议值 | 理由 |
|---|---|---|
| 间隔 | **420 秒**(7 分钟) | 单个原子步骤多为 CLI 调用(秒级),但阶段 3 的 assessment 轮询可能数分钟 |
| 轮次上限 | **40** | 24 张主线卡,按每卡 1–2 轮估算 |

⚠️ 用 `monitor_start` MCP 工具起循环时**没有** `stop_sentinel_path` 参数,
服务不会自动检查 `STOP` 文件 —— 代理每轮自己 `ls` 该文件。要可靠停止,
用仪表盘 🎯 弹层,或直接让代理调 `autonudge_stop`。

## 环境前提(每轮都要设)

```bash
export PATH="$HOME/.local/bin:$PATH"   # 官方 aws-cli v2.36.34,支持 resiliencehubv2
export AWS_DEFAULT_REGION=ap-northeast-1
aws --version   # 必须是 2.36.x;若显示 2.33.15 说明 PATH 没生效,该版本不支持 v2 命名空间
```

## 每轮期望行为

1. 先查停止条件:`STOP` 是否存在、5 条 DoD 是否全绿 → 是则 `autonudge_stop`
2. 从 `tasks.md` 领一张 `todo` 且依赖全 `done` 的卡,改 `doing`
3. 无可领卡时,跑 `north_star.md` 第 3 节的发现来源,把新问题追加为卡
4. 执行**一个原子步骤**(≤5 次工具调用)
5. 更新 `tasks.md`:状态 + 一行 `<UTC> cycle-<n>: <动作> <结果>`
6. 只在 DoD 达成 / 硬阻塞 / STOP 触发时在聊天里说话,否则保持安静

## 关于阻塞

停止条件只有两条(目标达成、不可恢复的基础设施故障)。
评估 `FAILED`、权限报错、配额不足、试过两次没成 —— **都不是停止理由**。
`errorCode` 正是要修的东西,那就是工作本身。

真遇到需要用户决策的事(超出 `north_star.md` 第 6 节允许的写操作、
超出第 5 节预算、要推送分支),把卡改 `review` 并**只提一次** blocker,
然后换另一张卡继续,不要空转。

# 怎么用这套定义让我干活

四个文件在 `/home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/`：

| 文件 | 作用 | 谁改 |
|---|---|---|
| `north_star.md` | 不变目标 + 8 条 DoD + 12 条运行不变量 + 停止条件 | 只有你能改 |
| `roadmap.md` | Stage 0-7 阶段划分与依赖 | 我可以随进展调整 |
| `tasks.md` | 34 张卡的看板，**每轮必须更新** | 我每轮更新 |
| `verify_dod.sh` | 一条命令判定 DoD 是否达成 | 我随 DoD 变化维护 |

停止哨兵路径（**必须写绝对路径**）：

```
/home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/STOP
```

---

## 用法一：让我自己武装循环（推荐，你只需说一句话）

直接对我说「**武装这个循环**」，我会调 `monitor_start` 把下面这段作为循环指令挂上，
之后每轮自动唤醒我继续推进，你不用再说话。

建议参数：**间隔 900 秒、上限 40 轮**。
理由：这个项目一轮原子步骤常包含改代码 + 跑全量测试（约 1-3 分钟）+ 查活图谱，
间隔太短会在上一轮还没验证完就催我。

一个必须知道的限制：`monitor_start` **没有** `stop_sentinel_path` 参数，
所以 `STOP` 文件对它是**惰性的** —— 真正的停止靠我自己调 `autonudge_stop`。
`STOP` 文件仍然有用：它是给我看的信号，我每轮会先检查它。
你要中途叫停有三条路：
1. 直接对我说「停」；
2. 在 dashboard 的 🎯 popover 里停掉循环；
3. `touch` 上面那个 STOP 路径（我下一轮会看到并收口）。

---

## 用法二：你自己从 🎯 popover 武装

把下面这段**原样**粘进循环指令框（路径已展开为绝对路径，因为循环唤醒时的工作目录不保证）：

```
Your north star is in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/north_star.md,
roadmap in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/roadmap.md,
tasks in /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/tasks.md.
Pick the single highest-leverage next step toward the goal and execute it. Update tasks.md.
Post a blocker ONCE if genuinely stuck.
To halt the loop, create /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify/STOP
```

参数同上：间隔 900 秒，上限 40 轮。

---

## 用法三：不开循环，一轮一轮手动推

对我说「**推进下一张卡**」即可。我会读 `tasks.md`、按 roadmap 的选卡优先级挑一张、
做完更新看板、然后停下等你。适合你想逐轮 review 的时候。

---

## 我每轮会做什么

1. 检查 `STOP` 是否存在 —— 存在就收口并写交接文档
2. 读 `tasks.md`，按 roadmap 的选卡优先级挑**一张**卡（关 DoD 的卡压过 backlog 卡）
3. 执行，实测验证（不是"看代码认为对"）
4. 更新 `tasks.md`：卡状态 + 进度总览表 + 追加一行 Cycle 日志
5. 每轮跑 `python3.11 -m pytest`；基线是 **442 passed / 0 failed**，任何失败都是本轮造成的
6. 卡住 3 轮 → 播报 blocker **一次** → 置 `blocked` → 换卡

---

## 三件需要你现在决定的事

循环会在 **Stage 7** 整个停下 —— 那三张卡按 `north_star.md` §6 全部标了
「需用户批准」，我不会自己执行：

| 卡 | 动作 | 现在的状态 |
|---|---|---|
| T-270 | 部署四个 ETL 函数代码 | 层 `:6` 已就绪，函数包里的 `find_vertex_by_name` 修复与契约门禁**尚未生效** |
| T-271 | `fix_wrong_source_edges.py --apply` 清 183 条错源边 | 必须在 T-270 之后，否则旧代码下轮原样重建 |
| T-272 | `GRAPH_EDGE_EXPIRY_ENABLED=true` | 活图谱 dry-run 0 条待失效，开启是安全的 |

另有 **T-214 / T-233**（真实故障注入到非生产 EKS）也需要你一次性授权 ——
这两张是目标 A 从"设计完成"变成"跑通"的唯一路径，不授权的话循环最多做到
Stage 1 的代码就绪，`DoD-3` / `DoD-4` 永远拿不到数字。

**如果你现在一次性授权这些，循环就能一路跑到 DoD 全绿；不授权，它会在 Stage 5 前后停住。**

---

## 先跑一次核验，看当前离 DoD 有多远

```bash
cd /home/ec2-user/works/graph-dependency-platform/todo/goal-loop-chaos-verify
./verify_dod.sh --local-only     # 只跑本地检查，不碰 Neptune / AWS
./verify_dod.sh                  # 全量（需 Neptune 与 AWS 只读权限）
```

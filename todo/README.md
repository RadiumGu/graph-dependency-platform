# todo/ — 过程记录与运行数据

**如果你想了解这个项目，请先看 [`docs/README.md`](../docs/README.md)，不是这里。**

2026-09-21 做过一次文档归类：有长期价值的设计说明与实测教训（39 份）已移入
`docs/design/`、`docs/lessons/`、`docs/runbooks/`。留在这里的是两类东西。

---

## 一、目标循环台账与过程报告（17 份根目录 md + 9 个子目录）

这些是「做某件事的过程」的记录，不是「系统怎么设计的」说明。新人不必读，
但在追溯某个决定为什么这么做时有用。

| 子目录／文件 | 内容 |
|---|---|
| `goal-loop/` | 平台目标循环锚目录（含哨兵 `STOP`） |
| `goal-loop-arh-v2/` | AWS Resilience Hub v2 接入循环 + 三套 app 的图谱导出 |
| `goal-loop-chaos-verify/` | 混沌验证循环 |
| `adot-migration-goal/GOAL.md` | ADOT 迁移循环的权威状态文件（1412 行） |
| `agentobv/` | PetSite + AgentCore 重建循环，含 Stage1-6 执行清单与图谱快照 |
| `demo-site-rebuild/PLAN.md` | 展示站重建任务台账 |
| `webui/01~04` | Streamlit 展示界面的审计、落位、任务、MCP 部署记录 |
| `graph-dep-research-20260531/` | 2026-05-31 的外部调研链路（已被后续调研取代） |
| `loadtest/` | 压测机 userdata 与 TargetGroupBinding 配置 |
| `tokyo-*.md`、`deploy-result*.md`、`cdk-live-reconcile*.md` | 东京区部署与变更报告 |
| `LOOP-GOAL_20260920.md` | 当前循环的目标与纪律文件（**活跃使用中**） |
| `HANDOVER_20260828-2215.md` | 22 轮目标循环的收口交接 |
| `CROSS-SESSION-NOTE_20260905-0630.md` | 跨会话运行日志（1906 行，名实不符：名为「4 条测试失败提醒」，实为多主题日志） |

## 二、脚本运行输出（62 个 JSON）

**这些不是文档，是脚本的输入输出，删了会弄坏代码。**

```
scripts/emit_resilience_scorecard.py      读 todo/retracted-false-soft-verdicts_*.json
                                          读 todo/chaos-external-edge-run_*.json
scripts/prune_deleted_workloads.py        写 todo/pruned-workloads_<时刻>.json
```

系列清单（每个系列通常只有最新一份有意义）：

| 系列 | 份数 | 内容 |
|---|---|---|
| `iam-deny-probe_*.json` | 14 | IAM deny 探针输出 |
| `preflight-edge-traffic_*.json` | 6 | 边流量预检 |
| `marked-unreachable-edges_*.json` | 4 | 标记不可达边 |
| `reclassified-blocked-edges_*.json` | 4 | 重分类受阻边 |
| `chaos-external-edge-run_*.json` | 4 | 外部边混沌注入结果 |
| `rds-fault-probe_*.json` | 4 | RDS 故障探针 |
| `svc-blackhole-probe_*.json` | 2 | 服务黑洞探针 |
| `retracted-false-soft-verdicts_*.json` | 2 | 撤销误判 soft 边 |
| 其余单次快照 | 5 | actionable-edge-queue、decision-bearing-edge-queue、removed-verify-attrs-nondependency-edges、pruned-workloads、stepfn-three-ring |
| `agentobv/snapshots/*.json` | 5 | 图谱 before／after 快照 |
| `goal-loop-arh-v2/exports/*` | 17 | 三套 app 的 dependencies／findings／resources／topology 导出 |

要清理的话，安全做法是**每个系列保留最新一份**，并先确认没有脚本按固定文件名读取。

---

## 关于「direct 引擎」

这里的多数文档写于 2026-09-20 之前，会把 `direct` 与 `strands` 双引擎当作现状。
**那已经结束**：七个模块的 direct 实现已全部删除，只剩 strands。
读到双轨描述时按历史记录理解。

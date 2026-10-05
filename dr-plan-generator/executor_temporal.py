"""
executor_temporal.py — 经 Temporal 执行 DR plan 的执行引擎(骨架)。

## 与 StrandsExecutor 的分工

`StrandsExecutor` 在**本进程内**逐步执行:进程死了,切换就断在中途,
而且没有任何地方记得走到了第几步。对一次 region 切换来说那是不可接受的 ——
切换本身可能要几十分钟,跨越多个需要人工放行的决策点。

`TemporalExecutor` 把执行**交给 Temporal**:本类只负责启动 workflow、
把计划正文送进去、然后观察。真正执行步骤的是 worker。

    本类(在哪跑都行)   ──启动/观察──>  Temporal (10.20.1.10:7243，首尔)
                                             │
                                             │ 派单
                                             ▼
                                      worker(Temporal 同一台 EC2)
                                             │
                                             └─> 只读：探针核实终态
                                                 （变更由人执行，2026-10-05 起）

## 为什么 worker 和 Temporal 同一台 EC2

2026-09-24 定的。备选是韩国 EKS 或东京 EKS,选这台的理由:

1. **切换时 EKS 可能正是要被操作的对象。** 把 worker 放在韩国 EKS 上,
   就出现「执行切换的东西依赖被切换的东西」——拉起节点组这一步会把
   自己的运行环境卷进去。
2. **守夜灯站点平时零节点。** 韩国 EKS 常态 desired=0,worker 放上面
   等于平时不存在,而切换恰恰是它最该在的时候。
3. 东京 EKS 可用,但灾难场景里东京可能正是失效的那一侧。

代价:这台 EC2 成了单点。**这是已知取舍,不是疏漏** —— 守夜灯站点的
定位本就是「切换时才全量拉起」,而 worker 的职责是发起而非承载流量。

## ⚠️ 本文件是骨架:三件事刻意没做

1. **没有真的写权限。** 见下面 `_REQUIRED_WRITE_ACTIONS` 的说明。
2. **没有定计划的保存形态。** 见 `_RETENTION_PROBLEM`。
3. **没有实现 worker。** worker 是独立进程,不在本仓库这一层。
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from executor_base import ExecutorBase

if TYPE_CHECKING:
    from models import DRPlan
    from validation.verification_models import RehearsalReport, VerificationLevel

logger = logging.getLogger(__name__)


# ── 阶段 D 的两个未决问题,写在代码里而不是只写在 todo 里 ──────────────────

_REQUIRED_WRITE_ACTIONS = """
worker 的写权限 —— **2026-10-05 定案：不授予，也不再需要。**

切换步骤真正要动的（拉节点组 eks:UpdateNodegroupConfig、提升从集群
rds:FailoverGlobalCluster、切 DNS route53:ChangeResourceRecordSets）
现在全部是 dr-runbook 里的 `manual` 步骤：Temporal 把命令原文展示给人，
**由人执行**，再用只读探针核实终态。所以 worker 角色永远只需要读。

「有序切换 vs --allow-data-loss」仍是需人裁决的点 —— 在 runbook 里是一个
`decision` 步骤，没有默认值，等不到裁决就停住。
"""

_RETENTION_PROBLEM = """
计划的保存形态 —— **2026-10-05 定案。**

实测：default 命名空间 workflowExecutionRetentionTtl = 2592000s（720h / 30 天），
2026-09-26 重建时从 24h 提上来（见 /opt/temporal/rebuild.sh 的 RETENTION）。
此前代码注释里的「86400s（1 天）」是重建之前的状态，已过时。

归档刻意未开（filestore 指向单点 EC2 的 /tmp，比不开更糟），所以：
  · 计划正文：S3 不可覆盖的 plans/<plan_id>/v<N>.md（put_plan_version）
  · 执行记录：结束时（含被取消）导出到 plans/<plan_id>/executions/
  · Temporal 只承载评审与执行的过程，30 天后过程会被清掉，结论不会
"""


@dataclass
class TemporalWorkflowHandle:
    """启动后拿到的句柄。刻意不叫「执行结果」—— 启动成功不等于执行成功。"""

    workflow_id: str
    run_id: str
    task_queue: str
    #: 服务端是否受理了启动。⚠️ 这**不表示**有 worker 在执行。
    accepted: bool
    #: 启动时任务队列上是否真的有 poller。None = 服务端没报(无法区分)。
    pollers_present: bool | None


class TemporalExecutor(ExecutorBase):
    """把 DR plan 交给 Temporal 执行。

    ⚠️ 骨架阶段:`execute()` 只做到「启动并确认有 worker 接单」,
    不实现步骤执行 —— 那在 worker 里。
    """

    ENGINE_NAME = "temporal"

    #: 默认任务队列。worker 必须监听同一个名字,否则 workflow 会永远排队
    #: 而**看起来是 RUNNING**(实测过)。
    DEFAULT_TASK_QUEUE = "dr-plan-queue"

    def __init__(self, dry_run: bool = True) -> None:
        super().__init__(dry_run=dry_run)
        self.task_queue = os.environ.get("DR_TEMPORAL_TASK_QUEUE", self.DEFAULT_TASK_QUEUE)
        #: temporal-mcp 在 AgentCore 上的 ARN。经它间接操作 Temporal,
        #: 这样本进程不需要在 VPC 内 —— Temporal 没有公网入口。
        self.mcp_runtime_arn = os.environ.get("DR_TEMPORAL_MCP_RUNTIME_ARN") or ""

    # ── 启动 ──────────────────────────────────────────────────────────────

    def _build_start_args(self, plan: "DRPlan", plan_ref: str) -> dict[str, Any]:
        """拼 start_workflow 的参数。

        每个字段都不是可选的,理由见 temporal-mcp 里 startWorkflowSchema
        的注释(那些字段的线上名都对活服务端实测过)。
        """
        return {
            # ⚠️ workflow ID 是「一次只做一个切换」的**唯一**保障机制。
            #
            # 同一个 ID 在运行中时，Temporal 默认的重用策略会直接拒绝第二次
            # 启动（WorkflowExecutionAlreadyStarted）。所以这里刻意**不传**
            # id_reuse_policy —— 传 ALLOW_DUPLICATE 会把这层互斥拆掉。
            #
            # 2026-09-24 的教训：我曾以为把 worker 的
            # max_concurrent_workflow_tasks 设成 1 就等于「一次一个切换」。
            # 那是错的：那个数限制的是「推进状态机的短任务」，防不住两个
            # 不同 ID 同时跑，却造成队头阻塞 —— 实测一个永久失败的 workflow
            # 就能把真切换拖三分钟。
            #
            # 代价要说清楚：按 plan_ref 取 ID 意味着**两份不同的计划仍可并发**。
            # 若要全局单例（同一时刻整个站点只允许一个切换），把 ID 改成固定
            # 前缀 + 目标 region，例如 f"dr-failover-{region}"。
            # 现在不改，是因为「同一份计划不许重复触发」已覆盖误重试这个
            # 主要风险，而跨计划并发需要先定义「哪些计划互斥」。
            "workflow_id": f"dr-failover-{plan_ref}",
            # DrFailoverWorkflow 已于 2026-10-05 退役（未注册的类型会让执行
            # 永远 RUNNING 而不报错）。入口现在是计划评审生命周期本身。
            "workflow_type": "DrPlanWorkflow",
            "task_queue": self.task_queue,
            # 切换会改动生产基础设施 —— 没有超时,卡住了就永远挂着。
            "execution_timeout": os.environ.get("DR_TEMPORAL_EXEC_TIMEOUT", "7200s"),
            "run_timeout": os.environ.get("DR_TEMPORAL_RUN_TIMEOUT", "3600s"),
            "task_timeout": "60s",
            # 事后复盘要能查出是谁发起的。
            "identity": f"dr-plan-generator@{os.environ.get('HOSTNAME', 'unknown')}",
            # 幂等:切换命令重试不能起出第二个切换流程。
            "request_id": str(uuid.uuid4()),
            # ⚠️ 正文不进 memo —— 保留期 30 天后 history 会被清掉，
            #    权威正文在 S3 的不可覆盖版本键里。这里只放引用。
            "memo": {
                "plan_ref": plan_ref,
                "dry_run": self.dry_run,
                "generator": "dr-plan-generator",
            },
            "input": {"plan_ref": plan_ref, "dry_run": self.dry_run},
        }

    def execute(
        self, plan: "DRPlan", level: "VerificationLevel | None" = None
    ) -> "RehearsalReport":
        """启动切换 workflow。

        ⚠️ 仍未实现，且**刻意**不实现成「一键切换」：执行由人在
        DrRunbookWorkflow 上逐步推进，不是本进程能代劳的事。抛异常而不是返回
        一个看起来成功的空报告 —— 本项目的教训是「静默降级会让错误路径变成
        实际路径长达五个月」（见 executor_factory.py 里 direct 回退那段）。
        """
        raise NotImplementedError(
            "TemporalExecutor.execute() 不实现：切换由人在 DrRunbookWorkflow 上逐步执行。\n"
            "入口：在首尔 worker 主机上 `probe_cli.py start-plan` 起一条 DrPlanWorkflow，\n"
            "经 revise_plan / approve_plan / start_drill / authorize_execution 推进。\n"
            "两个曾经阻塞的决定已定案：\n"
            f"{_REQUIRED_WRITE_ACTIONS}\n{_RETENTION_PROBLEM}"
        )

    # ── 启动后必须独立确认的事 ────────────────────────────────────────────

    @staticmethod
    def interpret_task_queue_pollers(describe_output: dict[str, Any]) -> bool | None:
        """判断任务队列上到底有没有 worker。

        返回 True/False/None,**None 表示无法判断** —— 不是「没有」。

        实测(Temporal 1.29.7):
            有 worker 在听   返回体含 pollers 字段
            没有 worker      pollers 字段**整个不存在**

        字段不存在时以下三种状态返回同一个形状,本接口无法区分:
            ① 队列名拼错(队列不存在)
            ② 队列存在、无待办、无 worker
            ③ 有 workflow 正在 RUNNING 等着,但没有 worker ← 切换卡死

        ③ 是实测出来的,不是设想。所以这里返回 None 而不是 False,
        与本项目「零流量与健康在指标上无法区分 → 一律 inconclusive」
        同一个立场。
        """
        if "pollers" not in describe_output:
            return None
        return len(describe_output.get("pollers") or []) > 0

"""
executor_temporal.py — 经 Temporal 执行 DR plan 的执行引擎(骨架)。

## 与 StrandsExecutor 的分工

`StrandsExecutor` 在**本进程内**逐步执行:进程死了,切换就断在中途,
而且没有任何地方记得走到了第几步。对一次 region 切换来说那是不可接受的 ——
切换本身可能要几十分钟,跨越多个需要人工放行的决策点。

`TemporalExecutor` 把执行**交给 Temporal**:本类只负责启动 workflow、
把计划正文送进去、然后观察。真正执行步骤的是 worker。

    本类(在哪跑都行)   ──启动/观察──>  Temporal (10.20.1.125:7243)
                                             │
                                             │ 派单
                                             ▼
                                      worker(Temporal 同一台 EC2)
                                             │
                                             └─> 真正调 AWS API

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
worker 需要的写权限边界 —— **尚未授予,故意的**。

切换步骤真正要动的:
    eks:UpdateNodegroupConfig      把节点组从 0 拉到 2
    rds:FailoverGlobalCluster      提升韩国从集群为可写
    elasticloadbalancing:*         建/改入口
    route53:ChangeResourceRecordSets   DNS 切换

这些都是**不可逆或高影响**的动作。现在给宽权限等于在步骤还没定型时
先开口子,所以 02-temporal.yaml 的实例角色目前只有 Describe/读。

授予的前提是先回答:哪些步骤自动执行、哪些必须 signal 放行。
特别是 rds:FailoverGlobalCluster 有两个变体:
    有序切换(无数据丢失)          需要主集群还活着
    --allow-data-loss             主集群已失联时才用
**「用哪个」必须是需人工放行的决策点**,不能由 workflow 自己挑。
"""

_RETENTION_PROBLEM = """
计划的「保存」形态 —— **尚未定型**。

实测约束:本服务端 workflowExecutionRetentionTtl = 86400s(1 天)。
所以「把计划以 workflow 形式存在 Temporal 里」这条路走不通:
一天后历史就被清掉,而灾备计划要活几个月。

三个候选:
    ① 提高保留期            改服务端配置,最直接,但历史体积会涨
    ② 用 Schedule 承载      supportsSchedules=true 已实测确认;
                            Schedule 不受 workflow 保留期约束
    ③ 计划正文另存(S3/仓库),workflow 的 memo 只放引用
实测:memo 可用且 describe 能回读(见 temporal-mcp 的实测记录)。

倾向 ③ + ②:正文归档在能做版本管理的地方,Temporal 只管执行。
但这要用户拍板,所以本骨架两条都没实现。
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
            "workflow_type": "DrFailoverWorkflow",
            "task_queue": self.task_queue,
            # 切换会改动生产基础设施 —— 没有超时,卡住了就永远挂着。
            "execution_timeout": os.environ.get("DR_TEMPORAL_EXEC_TIMEOUT", "7200s"),
            "run_timeout": os.environ.get("DR_TEMPORAL_RUN_TIMEOUT", "3600s"),
            "task_timeout": "60s",
            # 事后复盘要能查出是谁发起的。
            "identity": f"dr-plan-generator@{os.environ.get('HOSTNAME', 'unknown')}",
            # 幂等:切换命令重试不能起出第二个切换流程。
            "request_id": str(uuid.uuid4()),
            # ⚠️ 正文不进 input —— 保留期只有 1 天,且体积未知上限
            #    (describe_namespace 拿不到 blobSizeLimitError,实测确认)。
            #    这里只放引用,正文另存。
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

        ⚠️ 骨架:尚未实现。抛异常而不是返回一个看起来成功的空报告 ——
        本项目的教训是「静默降级会让错误路径变成实际路径长达五个月」
        (见 executor_factory.py 里 direct 回退那段)。
        """
        raise NotImplementedError(
            "TemporalExecutor 是骨架,尚未实现 execute()。\n"
            "阻塞在两个需要定型的决定上:\n"
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

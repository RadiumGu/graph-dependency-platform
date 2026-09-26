"""
worker.py — 灾备切换 worker 的入口。

## 跑在哪

Temporal 服务端同一台 EC2(ap-northeast-2,私有 IP 固定为 10.20.1.10)。

⚠️ 这里刻意**不写实例 ID**:实例会被替换(2026-09-25 就替换过一次,
`i-06f0a3e4961b8061e` → `i-09380e417a0177ed4`),写死 ID 的注释会变成
误导人的过期事实。IP 是固定的(02-temporal.yaml 的 PrivateIpAddress),
所以用 IP 指代更稳。
2026-09-24 定的,备选是韩国 EKS 与东京 EKS,选这台的理由:

1. **切换时 EKS 可能正是要被操作的对象。** worker 放在韩国 EKS 上,
   就出现「执行切换的东西依赖被切换的东西」—— 拉起节点组这一步会把
   自己的运行环境卷进去。
2. **守夜灯站点常态 desired=0。** worker 放上面等于平时不存在,
   而切换恰恰是它最该在的时候。
3. 东京 EKS 可用,但灾难场景里东京可能正是失效的那一侧。

代价:这台 EC2 成了单点。**已知取舍** —— worker 的职责是发起切换而非
承载流量,且守夜灯站点本就定位为「切换时才全量拉起」。

## 环境(实测确认,不是假设)

    宿主系统 python3   3.9.25  ← 不能用，temporalio 要求 >=3.10
                               且 aws-cfn-bootstrap / ec2-utils 依赖它，
                               不许替换
    并装解释器          python3.12.14（AL2023 仓库里有 3.11~3.14）
    venv               /opt/dr-worker/venv
    temporalio         1.33.0（cp310-abi3 的 aarch64 轮子，实测可导入）
    Temporal gRPC      localhost:7233 ← worker 走 gRPC
                       ⚠️ 不是 7243，那是 HTTP API（temporal-mcp 用的）
                       也不是 8080，那是 Web UI

## ⚠️ 当前只跑 dry_run

本实例的 IAM 角色只有 Describe 权限(见 `infra/dr-korea/02-temporal.yaml`)。
`dry_run=False` 现在会 AccessDenied —— **刻意的**,权限按步骤逐个放开。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    fetch_plan_body,
    promote_database,
    put_plan_version,
    scale_up_nodegroup,
    verify_step,
)
from plan_workflow import DrPlanWorkflow
from snapshot_workflow import ExportSnapshotWorkflow, export_graph_snapshot
from workflows import (
    DrFailoverWorkflow,
    FetchPlanWorkflow,
    PromoteDatabaseWorkflow,
    ScaleNodegroupWorkflow,
    VerifyWorkflow,
)

#: workflow 会 upsert 的自定义 Search Attributes → Temporal 的索引类型枚举。
#:
#: 刻意**直接引用枚举成员**而不是用 `f"INDEXED_VALUE_TYPE_{名字.upper()}"`
#: 拼出来:实测那样拼会在 KeywordList 上炸（真实名字是
#: `INDEXED_VALUE_TYPE_KEYWORD_LIST`，带下划线），而且是启动时才炸。
#: 字符串拼枚举名省不了几行，却把一个编译期错误推迟成了运行期错误。
#:
#: ⚠️ **必须在 worker 启动时确保它们存在,否则 workflow 会静默卡死。**
#:
#: 2026-09-26 实测（temporalio 1.33.0）:属性未注册时
#: `upsert_search_attributes` 让服务端返回
#:     'Client specified an invalid argument': search attribute DRDryRun is not defined
#: 该错误发生在 **workflow activation 提交阶段**,不在调用点抛出 ——
#: 所以在 workflow 里 try/except 是**接不住**的,执行会无限重试那次 activation,
#: 表现为「RUNNING 但永不前进」。
#:
#: 这正是本项目最警惕的那类失败:**审计功能把被审计的东西搞挂了**。
#: 所以不把它当成"运行时优雅降级",而是当成**部署前置条件**,在这里保证。
def _search_attribute_types() -> dict[str, int]:
    from temporalio.api.enums.v1 import IndexedValueType as T

    return {
        "DRStepsExecuted": T.INDEXED_VALUE_TYPE_KEYWORD_LIST,
        "DRPlanRef": T.INDEXED_VALUE_TYPE_KEYWORD,
        "DRDryRun": T.INDEXED_VALUE_TYPE_BOOL,
        "DRStepName": T.INDEXED_VALUE_TYPE_KEYWORD,
        "DRDecision": T.INDEXED_VALUE_TYPE_KEYWORD,
        # ── 计划评审生命周期（DrPlanWorkflow）────────────────────────────
        # 加这三个是为了让「哪些计划正等着审批 / 演练过了没有」成为一条
        # 查询，而不是一次翻历史：
        #     temporal workflow list --query 'DRPlanState = "draft"'
        "DRPlanId": T.INDEXED_VALUE_TYPE_KEYWORD,
        "DRPlanVersion": T.INDEXED_VALUE_TYPE_INT,
        "DRPlanState": T.INDEXED_VALUE_TYPE_KEYWORD,
    }


async def ensure_search_attributes(client: Client, namespace: str) -> None:
    """确保自定义 Search Attributes 存在。幂等:已存在即跳过。

    失败时**抛出而不是继续** —— 带着缺失属性启动 worker,等于让第一次真实
    切换在中途卡死。宁可 worker 起不来（响亮、立刻可见），
    也不要 workflow 卡死（安静、要翻 history 才看得出来）。
    """
    from temporalio.api.operatorservice.v1 import (
        AddSearchAttributesRequest,
        ListSearchAttributesRequest,
    )
    from temporalio.service import RPCError, RPCStatusCode

    wanted = _search_attribute_types()
    try:
        existing = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=namespace)
        )
    except RPCError as e:
        if e.status == RPCStatusCode.UNIMPLEMENTED:
            # 精简版测试服务（`WorkflowEnvironment.start_time_skipping()`）不实现
            # OperatorService。**这不是"属性缺失"**，两者必须分开处理：
            # 真实服务端不支持这个 API 是不可能的，所以只在测试场景出现。
            logging.warning(
                "服务端未实现 OperatorService.ListSearchAttributes —— "
                "跳过属性检查。仅精简测试服务会这样；真实部署不该走到这里。"
            )
            return
        raise

    missing = {k: v for k, v in wanted.items() if k not in existing.custom_attributes}
    if not missing:
        logging.info("Search attributes 齐备（%d 个）。", len(wanted))
        return

    logging.info("注册缺失的 search attributes: %s", sorted(missing))
    await client.operator_service.add_search_attributes(
        AddSearchAttributesRequest(namespace=namespace, search_attributes=missing)
    )
    logging.info("已注册 %d 个 search attribute。", len(missing))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("dr-worker")

#: worker 走 gRPC。默认 localhost 因为它与 Temporal 同机。
TEMPORAL_TARGET = os.environ.get("DR_TEMPORAL_TARGET", "localhost:7233")
NAMESPACE = os.environ.get("DR_TEMPORAL_NAMESPACE", "default")
#: 必须与发起方用的队列名一致。不一致的后果是 workflow 永远排队
#: 而**看起来是 RUNNING**（实测过），不会报任何错。
TASK_QUEUE = os.environ.get("DR_TEMPORAL_TASK_QUEUE", "dr-plan-queue")
#: 快照队列。与切换队列分开是刻意的：Schedule 指向它，
#: 而快照跑挂了不该影响切换队列上的执行排队。
SNAPSHOT_QUEUE = os.environ.get("DR_SNAPSHOT_TASK_QUEUE", "dr-snapshot-queue")


async def main() -> None:
    client = await Client.connect(TEMPORAL_TARGET, namespace=NAMESPACE)
    logger.info(
        "已连上 Temporal target=%s namespace=%s task_queue=%s",
        TEMPORAL_TARGET,
        NAMESPACE,
        TASK_QUEUE,
    )

    # 部署前置条件:workflow 会 upsert 的 search attributes 必须存在。
    # 放在 Worker 创建**之前**，缺了就在这里响亮失败 ——
    # 而不是等第一次切换跑到一半静默卡死。
    await ensure_search_attributes(client, NAMESPACE)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        # 父 + 四个原子步骤子 workflow。**子 workflow 类型必须在这里注册**，
        # 否则父起子时会一直等一个不存在的 handler —— 而那正是
        # 「启动成功不等于在执行」那类静默卡死。
        workflows=[
            # 计划评审生命周期。它会以子 workflow 的形式起 DrFailoverWorkflow，
            # 所以两者必须注册在**同一个**队列上。
            DrPlanWorkflow,
            DrFailoverWorkflow,
            FetchPlanWorkflow,
            ScaleNodegroupWorkflow,
            PromoteDatabaseWorkflow,
            VerifyWorkflow,
        ],
        activities=[
            fetch_plan_body,
            put_plan_version,
            scale_up_nodegroup,
            promote_database,
            verify_step,
        ],
        # ── 2026-09-24：这两个数原来都是 1，那是个设计错误 ──────────────
        #
        # 当时的想法是「一次只做一个切换」。但 **workflow task 并发 ≠
        # 并发切换数**：workflow task 是「推进一步状态机」的短任务，
        # 把它限制成 1 防不住两个不同 workflow ID 同时跑（它们只是
        # 互相拖慢），却会造成**队头阻塞**。
        #
        # 实测到的后果：队列上有一个永久失败的 workflow（workflow type
        # 没注册，Temporal 会无限重试它的 workflow task）时，唯一的槽位
        # 被它占住，真正的切换 workflow 被拖了 3 分钟，历史里留下
        # WORKFLOW_TASK_TIMED_OUT —— 即使把 workflowTaskTimeout 从 10s
        # 调到 60s 也照样超时。在真实切换里这几分钟是有代价的。
        #
        # 「一次只做一个切换」的正确机制是 **workflow ID**：
        # 同一个 workflow ID 在运行中时，默认的重用策略会直接拒绝第二次
        # 启动（WorkflowExecutionAlreadyStarted），这才是真正的互斥。
        # 见 executor_temporal._build_start_args 里 workflow_id 的构造。
        max_concurrent_workflow_tasks=10,
        # activity 才是真正干活、可能长时间跑的那个。这里留小一点：
        # 切换步骤之间基本是串行的，给 5 个足够容纳重试与心跳。
        max_concurrent_activities=5,
    )

    # ── 第二个队列：图快照 ────────────────────────────────────────────────
    #
    # 为什么与切换 worker 同进程，而不是另起一个 systemd 单元：
    #   · 这台 EC2 本身就是单点，多一个进程并不提高可用性，只多一份运维面
    #   · Temporal 的两个 Worker 相互独立 —— activity 失败不会掀翻进程，
    #     快照 activity 也把异常收成 SnapshotResult(ok=False)
    #
    # 代价要说清楚：**进程级故障会同时带走两个队列**。切换队列是关键路径，
    # 快照是周期作业，所以这个耦合的方向是「次要拖累关键」——
    # 如果哪天快照 activity 引入了能杀进程的东西（比如 segfault 的 C 扩展），
    # 就该把它拆出去。现在的依赖只有 urllib + boto3，不值得为此拆。
    snapshot_worker = Worker(
        client,
        task_queue=SNAPSHOT_QUEUE,
        workflows=[ExportSnapshotWorkflow],
        activities=[export_graph_snapshot],
        # 快照是单步、无并发需求的周期作业。给 1 就够 ——
        # overlap_policy=SKIP 已经在 Schedule 层面挡住了重叠。
        max_concurrent_workflow_tasks=2,
        max_concurrent_activities=1,
    )

    stop = asyncio.Event()

    def _on_signal(signame: str) -> None:
        # 收到停止信号时优雅退出:让当前 activity 跑完而不是半途断开。
        # 半途断开的后果是 Temporal 要等 activity 超时才重派,
        # 而切换过程中多等几分钟是有代价的。
        logger.info("收到 %s，等当前任务结束后退出", signame)
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, _on_signal, s.name)

    # ⚠️ 这里原来写的是
    #     「⚠️ 当前实例角色只有 Describe 权限，只能跑 dry_run。」
    # 那是一句**写死的断言**，而它在 2026-09-26 授予 dr-plan-write 之后就成了
    # 假话 —— 日志照旧这么说，而真执行其实已经可行。
    #
    # 一句启动日志**不可能知道**角色能做什么，除非真去做一次（而那就有副作用）。
    # 所以改成只报身份：能不能真执行由每一步自己的闸门与 AWS API 说话，
    # 不由一句看起来权威的日志说话。
    logger.info(
        "worker 开始轮询。队列：%s（切换）、%s（快照）。"
        "真执行能力不在此断言 —— 由各步骤的闸门与 AWS API 在执行时裁定。",
        TASK_QUEUE,
        SNAPSHOT_QUEUE,
    )
    async with worker, snapshot_worker:
        await stop.wait()
    logger.info("worker 已退出")


if __name__ == "__main__":
    asyncio.run(main())

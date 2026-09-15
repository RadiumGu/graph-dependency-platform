"""rca/actions/devops_agent_trigger.py — 告警触发 DevOps Agent 调查

## 为什么不用 `aws devops-agent create-trigger`

实测（2026-09-15）`create-trigger` 的 `--condition` 是 tagged union，
文档原文：

    NOTE: This is a Tagged Union structure. Only one of the following
          top level keys can be set: schedule.

**只支持定时触发，没有告警条件。** 所以"告警自动触发调查"必须走别的路。

AWS 工作坊正文其实说了同一件事：labs 里用 CLI 直接调
`create-backlog-task` 是为了自包含，而"In a production deployment,
alarm-driven investigations [are] invoked by a Lambda function"。

## 为什么接在 AlertBuffer 之后而不是新建 Lambda

`rca/handler.py` 已经在接 CloudWatch Alarm 的 SNS 事件，并且有
`AlertBuffer` 做窗口去重。新建一个 Lambda 等于把告警接入做两遍，
而且两份的去重逻辑会漂移。

接在 `put_alert()` 返回 `is_first=True` 那一刻最合适：
**同一个故障只发起一次调查**。不去重的话一次告警风暴会把 agent
的任务配额打满，而后面那些任务查的是同一件事。

## 发起时把证据纪律写进 description

`create-backlog-task` 的 `description` 是自由文本、由我们写。
这是把采纳率从 25% 提到 100% 的杠杆（实测见
`scripts/devops_agent_investigate.py` 的模块 docstring）：
不点名要求查图谱时，DevOps Agent 只有 25% 的概率会去查，
而没查的那些依据是「FIS 实验模板存在」——**模板是意图不是结果**。

纪律文本与 `scripts/devops_agent_investigate.py` 同源，从那里 import ——
不在这里复制一份。本仓库为「同类清单各处一份」付过代价。

## 默认关闭

`DEVOPS_AGENT_INVESTIGATE_ENABLED` 默认 `false`。理由：
它会消耗 agent task 配额（`get-account-usage` 可查），
而配额耗尽时后续真实故障就发不出调查了。
开之前应先确认配额与告警量的比例。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 与 `scripts/devops_agent_investigate.py` 同一个 agent space。
#: 那里已经硬编码了，这里 import 过来，不另立第二份来源。
_ENABLED_ENV = 'DEVOPS_AGENT_INVESTIGATE_ENABLED'


def enabled() -> bool:
    return os.environ.get(_ENABLED_ENV, 'false').lower() == 'true'


def _investigator():
    """按需 import 发起器。

    放在函数里而不是模块顶层：`scripts/` 不在 Lambda 部署包的常规路径上，
    import 失败不该让整个 RCA handler 起不来 ——
    发起调查是**增强**，不是告警处理的必要环节。
    """
    import pathlib
    import sys
    root = pathlib.Path(__file__).resolve().parents[2]
    sp = str(root / 'scripts')
    if sp not in sys.path:
        sys.path.insert(0, sp)
    import devops_agent_investigate as dai        # noqa: PLC0415
    return dai


def on_first_alert(unified, dry_run: bool = False) -> dict:
    """告警首次进入窗口时发起一次调查。

    `unified` 是 `core.event_normalizer.UnifiedAlertEvent`。
    返回结果 dict；任何异常都**吞掉并记日志** ——
    发起调查失败不能影响告警本身的处理，那是主链路。
    """
    if not enabled():
        return {'skipped': f'{_ENABLED_ENV} 未开启'}
    try:
        dai = _investigator()
        svc = getattr(unified, 'service_name', '') or 'unknown'
        alarm = getattr(unified, 'alarm_name', '') or ''
        metric = getattr(unified, 'metric', '') or ''
        fp = (getattr(unified, 'fingerprint', '') or '')[:8]
        symptom = (f'CloudWatch 告警 {alarm} 触发'
                   + (f'（指标 {metric}）' if metric else '')
                   + f'。告警指纹 {fp}')
        # 把「这是真实告警不是演练」写进上下文。
        #
        # 工作坊正文记录了一个行为：DevOps Agent 能识别出 CPU 尖峰是 FIS
        # 注入的，从而判定"这是演练"并**不给处置建议**。
        # 我们的混沌实验也走 FIS/Chaos Mesh，所以真实告警必须说清来源，
        # 否则可能被当成演练而拿不到处置计划。
        extra = ('本次调查来自**生产告警**，不是混沌演练。'
                 '若在时间窗内看到 FIS 或 Chaos Mesh 的活动，'
                 '请先确认它与本告警是否相关，不要据此判定为演练而跳过处置建议。')
        r = dai.create_investigation(
            title=f'{svc}: {alarm or "告警"}',
            service=svc, symptom=symptom, extra=extra,
            priority='HIGH', dry_run=dry_run)
        logger.info('DevOps Agent 调查已发起 svc=%s alarm=%s -> %s',
                    svc, alarm, list(r))
        return r
    except Exception as exc:                        # noqa: BLE001
        # 主链路优先：发起调查是增强，失败只记录。
        logger.warning('发起 DevOps Agent 调查失败（不影响告警处理）: %r', exc)
        return {'error': repr(exc)}

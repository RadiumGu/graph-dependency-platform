#!/usr/bin/env python3
"""主动向 AWS DevOps Agent 发起调查，并把证据纪律写进任务本身。

## 解决什么问题

`mcp/README.md` 记着一个实测数字 —— 同一个问题问 12 次：

| 提问方式 | 会去查图谱 | 比例 |
|---|---|---|
| 不提图谱 | 2/8 | **25%** |
| 点名要求查图谱 | 4/4 | **100%** |

23 个图谱查询工具已经关联好、`status: valid`，但**被用到的概率只有四分之一**。
这是全项目投入产出比最差的一处落差：不是缺功能，是缺"让它一定被用上"的机制。

没查图谱的那些，依据是「FIS 实验模板存在」。**模板是意图，不是结果** ——
它说明有人打算测，不说明测过、更不说明测出了什么。

## 做法

`create-backlog-task` 的 `description` 是**自由文本，由我们写**。
所以不靠祈祷：把要调用哪些工具、以及证据纪律，直接写进调查任务。

纪律部分抄自 MCP server 在 `initialize.instructions` 里已经声明的那套
（`mcp/server.py`），保持一处来源：

    confirmed   —— 有干预实测支撑，可直接用于推理
    refuted     —— 图谱曾声称存在、注入证明不成立，**不得用于推理**
    untested    —— 可用，但结论里必须声明"未经验证"
    inconclusive—— 试过但不能下结论，等同未验证

## 用法

    # 看将要发出的任务（不调 AWS）
    python3 scripts/devops_agent_investigate.py --dry-run \
        --title "petsite 5xx 上升" --service petsite

    # 真发
    python3 scripts/devops_agent_investigate.py --title ... --service petsite

    # 统计采纳率（读已有调查，不发新的）
    python3 scripts/devops_agent_investigate.py --measure-uptake

## 环境（踩过的坑）

`aws devops-agent` 的 agent space 是**区域性**资源，我们的在**东京**。
只查 us-east-1 会拿到空列表 —— 而空列表看起来像"资源不存在"，
不像"你查错地方了"。所以本脚本所有调用都显式带 region。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: agent space 与 MCP 关联。与 `scripts/emit_graph_coverage_metrics.py` 的
#: AGENT_SPACE 同一个值 —— 那里已经硬编码了，这里不另立第二份来源。
REGION = 'ap-northeast-1'
AGENT_SPACE_ID = '60c2f48f-b6e3-4dce-a0a3-4144228b2051'
MCP_ASSOCIATION_ID = 'd96fc34a-27aa-416f-bba6-fea28660e235'


#: 图谱工具里最该被调用的几个。列进 description 是为了给出**具体**指令 ——
#: "请查依赖图谱"这种客套话与不说没区别。
_CORE_TOOLS = [
    ('q1_blast_radius', '这个资源出问题会波及哪些服务'),
    ('q3_upstream_deps', '它依赖谁（上游）'),
    ('q16_single_point_of_failure', '路径上有没有单点'),
    ('q22_edge_verification_verdicts', '每条依赖边的验证结论与实验 ID'),
    ('q18_chaos_history', '这个资源上做过哪些混沌实验、结果如何'),
]

#: 证据纪律。与 mcp/server.py 的 initialize.instructions 同源。
_EVIDENCE_DISCIPLINE = """
证据纪律（必须遵守，这是本次调查的硬性要求）：

1. 先调用依赖图谱工具，再下结论。图谱里的依赖关系带**干预实测**的验证状态，
   这是遥测相关性给不出的东西 —— 相关性不是依赖关系。
2. 每条依赖边的 verify_status 决定它能怎么用：
   - confirmed    有故障注入实测支撑，可直接用于推理，请引用实验 ID 与退化幅度
   - refuted      图谱曾声称存在、注入证明不成立，**不得用于推理**
   - untested     可用，但结论里必须显式声明「这条依赖未经验证」
   - inconclusive 试过但不能下结论，等同未验证
3. **不要**把「存在 FIS 实验模板」当作证据。模板是意图不是结果：
   它说明有人打算测，不说明测过，更不说明测出了什么。
4. 证据不足时请明确说不能下结论，并说明缺什么。不要给一个听起来合理的猜测。
""".strip()


def _aws(*args: str, timeout: int = 180) -> tuple[int, str]:
    """调 devops-agent。有 `aws` CLI 就用它，没有则回落到 boto3。

    ## 为什么需要回落（2026-09-17 部署到 Lambda 时踩到）

    本模块原先只有 subprocess 这一条路。它在本地跑得很好，
    但 **Lambda 运行时不含 AWS CLI** —— 实测 `shutil.which('aws')`
    返回 `None`。于是生产日志报：

        发起 DevOps Agent 调查失败（不影响告警处理）:
        FileNotFoundError(2, 'No such file or directory')

    而 `handler.py` 那段是 try/except + warning（发起调查是增强、
    不是主链路），所以**告警照常处理、闭环静默失效**。
    这是同一天里第二个"本地绿、生产坏"的形状。

    boto3 这条路已实测可用：Lambda 运行时 botocore 1.42.97
    有 `devops-agent` client 且 `create_backlog_task` 存在
    （用一个临时探针 Lambda 验的，验完即删）。

    保留 CLI 分支而不是全量改 boto3：CLI 路径是本地实测过的那条
    （采纳率 25%→100% 那批实验都走它），换掉等于把已验证的行为
    重新变成未验证的。
    """
    import shutil
    if shutil.which('aws'):
        p = subprocess.run(['aws', 'devops-agent', *args,
                            '--region', REGION, '--output', 'json'],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')

    # ── boto3 回落 ──
    # CLI 的 `--kebab-case` 参数要转成 boto3 的 PascalCase/snake_case。
    # 只支持本模块实际用到的操作，不做通用转换器 ——
    # 通用转换器会在遇到没测过的操作时静默传错参数。
    import json as _json
    import boto3
    if not args:
        return 2, 'no operation given'
    op = args[0]
    kv, i = {}, 1
    while i < len(args):
        a = args[i]
        if a.startswith('--'):
            key = a[2:].replace('-', '_')
            if i + 1 < len(args) and not args[i + 1].startswith('--'):
                kv[key] = args[i + 1]
                i += 2
                continue
            kv[key] = True
        i += 1

    def _camel(s: str) -> str:
        head, *rest = s.split('_')
        return head + ''.join(w[:1].upper() + w[1:] for w in rest)

    # `_camel` 的输出（camelCase）就是这个服务的形参名，直接用。
    params = {_camel(k): v for k, v in kv.items()}

    # `--cli-input-json` 是 **CLI 专属**参数，boto3 没有对应形参。
    # 它的值本身就是一份完整的请求体，所以在 boto3 这条路上
    # 应当**展开**成实际参数，而不是当成一个叫 CliInputJson 的字段传进去。
    #
    # 踩到过：转换器把它变成 `CliInputJson=<json 字符串>`，
    # API 报参数错误，而 handler 把错误吞成 warning —— 又是一次静默失效。
    # ⚠️ 这个服务的 boto3 形参是 **camelCase**（`agentSpaceId`、`taskType`），
    # 不是多数 AWS 服务的 PascalCase。所以 `_camel` 的输出直接可用，
    # 不要再首字母大写 —— 本地验证时就是这里报
    # `Unknown parameter in input: "cliInputJson"`。
    blob = params.pop('cliInputJson', None)
    if blob:
        try:
            params.update(_json.loads(blob) if isinstance(blob, str) else blob)
        except (TypeError, ValueError) as exc:
            return 2, 'cli-input-json 解析失败: %s' % exc

    try:
        c = boto3.client('devops-agent', region_name=REGION)
        method = getattr(c, op.replace('-', '_'))
        resp = method(**params)
        resp.pop('ResponseMetadata', None)
        return 0, _json.dumps(resp, default=str)
    except Exception as exc:                    # noqa: BLE001
        return 1, '%s: %s' % (type(exc).__name__, exc)


def build_description(service: str, symptom: str, extra: str = '') -> str:
    """拼出调查任务的 description。

    结构刻意固定：先说查什么、再说纪律、最后给上下文。
    把纪律放中间而不是末尾，是因为长文本的结尾最容易被忽略。
    """
    tools = '\n'.join(f'   - {name}：{why}' for name, why in _CORE_TOOLS)
    return f"""调查对象：{service}
现象：{symptom}

请在调查中调用已关联的依赖图谱 MCP 工具（association {MCP_ASSOCIATION_ID}），
至少包括：
{tools}

{_EVIDENCE_DISCIPLINE}

{extra}""".strip()


def create_investigation(title: str, service: str, symptom: str,
                         extra: str = '', priority: str = 'HIGH',
                         dry_run: bool = True) -> dict:
    payload = {
        'agentSpaceId': AGENT_SPACE_ID,
        'taskType': 'INVESTIGATION',
        'title': title,
        'description': build_description(service, symptom, extra),
        'priority': priority,
        'clientToken': uuid.uuid4().hex,
    }
    # `reference` 是**可选**的，刻意不填。
    #
    # 它描述的是"这条任务来自哪个工单系统"，而 `reference.system` 必须是
    # DevOps Agent 已知的 data plane 服务名（实测填 'graph-dependency-platform'
    # 报 `ValidationException: Unknown data plane service name`）。
    # 我们没有接工单系统，伪造一个 system 值只会换来另一个校验错误，
    # 而把它填成某个真实系统的名字则是**谎报来源**。
    # 需要溯源时靠 title 与 description 里的上下文，够用。
    if dry_run:
        return {'dry_run': True, 'payload': payload}
    rc, out = _aws('create-backlog-task', '--cli-input-json',
                   json.dumps(payload, ensure_ascii=False))
    if rc != 0:
        return {'error': out.strip()[:600], 'payload': payload}
    try:
        return {'created': json.loads(out), 'payload': payload}
    except Exception:                                          # noqa: BLE001
        return {'created_raw': out.strip()[:600], 'payload': payload}


def measure_uptake(limit: int = 20) -> dict:
    """统计最近若干次调查里，有多少真的调用了图谱工具。

    判据是 journal records 里出现我们的工具名。**不看**调查结论里
    是否提到"依赖"这类字眼 —— 那个词模型自己就会说，不构成证据。
    这与仓库既有的「判据不要对准文本而要对准语义」是同一条纪律。
    """
    rc, out = _aws('list-executions', '--agent-space-id', AGENT_SPACE_ID)
    if rc != 0:
        return {'error': out.strip()[:400]}
    try:
        execs = (json.loads(out).get('executions') or [])[-limit:]
    except Exception:                                          # noqa: BLE001
        return {'error': f'解析 list-executions 失败: {out[:300]}'}

    tool_names = {n for n, _ in _CORE_TOOLS}
    used, total, detail = 0, 0, []
    for e in execs:
        eid = e.get('executionId') or e.get('id')
        if not eid:
            continue
        total += 1
        rc2, out2 = _aws('list-journal-records',
                         '--agent-space-id', AGENT_SPACE_ID,
                         '--execution-id', str(eid))
        blob = out2 if rc2 == 0 else ''
        hit = sorted(t for t in tool_names if t in blob)
        # 宽一点：任何 q\d+_ 前缀的工具名都算命中，_CORE_TOOLS 只是重点
        if not hit and 'q1_blast_radius' not in blob:
            import re
            hit = sorted(set(re.findall(r'\bq\d+[a-z_]*', blob)))
        if hit:
            used += 1
        detail.append({'executionId': eid, 'graph_tools_seen': hit})
    return {
        'examined': total,
        'used_graph': used,
        'uptake_pct': round(used / total * 100, 1) if total else None,
        'baseline_pct': 25.0,
        'detail': detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--title')
    ap.add_argument('--service')
    ap.add_argument('--symptom', default='指标异常，需定位根因')
    ap.add_argument('--extra', default='')
    ap.add_argument('--priority', default='HIGH',
                    choices=['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--measure-uptake', action='store_true')
    args = ap.parse_args()

    if args.measure_uptake:
        r = measure_uptake()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if r.get('uptake_pct') is not None:
            print(f"\n采纳率 {r['uptake_pct']}%（基线 25%）")
        return 0

    if not args.title or not args.service:
        ap.error('发起调查需要 --title 与 --service')
    r = create_investigation(args.title, args.service, args.symptom,
                             extra=args.extra, priority=args.priority,
                             dry_run=args.dry_run)
    if 'payload' in r and args.dry_run:
        print('── 将发出的 description ──')
        print(r['payload']['description'])
        print('\n── 完整 payload ──')
        print(json.dumps(r['payload'], ensure_ascii=False, indent=2))
        print('\n（dry-run，未调用 AWS）')
        return 0
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 1 if 'error' in r else 0


if __name__ == '__main__':
    raise SystemExit(main())

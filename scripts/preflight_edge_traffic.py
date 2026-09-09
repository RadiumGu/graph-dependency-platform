#!/usr/bin/env python3
"""对待验依赖边做**边级流量**前置检查，挑出「现在就能验」的那批。

## 为什么必须是边级而不是节点级

仓库已经付过一次代价：重验 `petsite -[Calls]-> payforadoption` 得到
「观测方退化 0.37%」，看着像「打断了但没传导」。查边级流量才发现真相是
**这条路径近 15 分钟 0 次调用** —— 负载生成器只压首页与搜索，
领养提交路径压根没流量。那 0.37% 是噪声。

观测方总流量再大也无关：**没有调用就无从打断**，此时任何退化数字
都是噪声。所以前置条件必须落在**被测的那条边**上。

## 三类源、三个后端

    Microservice   -> DeepFlow l7_flow_log（L7 实测流量，最可靠）
    LambdaFunction -> X-Ray 服务图 + CloudWatch Invocations
    StepFunction   -> CloudWatch AWS/States ExecutionsStarted
    SNSTopic       -> CloudWatch AWS/SNS NumberOfMessagesPublished

## 一类不走流量检查的边

`Microservice -DependsOn-> ECRRepository`：依赖只在 Pod 启动拉镜像时被用到，
稳态下**本来就没有**流量。对它做流量检查等于必然判「无流量」，
但它并不是休眠 —— `injectability` 把它判为 `needs_compound_experiment`
（配合删 Pod 就是可验证的复合实验）。所以按边形态先分流，不进流量检查。

## 用法

    python3 scripts/preflight_edge_traffic.py            # 只报告
    python3 scripts/preflight_edge_traffic.py --annotate # 把无流量的标进图谱
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# ⚠️ 顺序有讲究，且**不要**把 chaos/code/runner 也加进来：
# 加了之后 `runner` 会被当成顶层模块而不是 `chaos.code.runner` 包，
# 于是它内部的 `from .experiment import ...` 报
# `ImportError: attempted relative import with no known parent package`。
# xray_metrics / injectability 用 chaos.code.runner 前缀导入即可。
for _p in (ROOT / 'chaos' / 'code', ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 边级流量下限。与 `edge_verification` 的 `min_observation_requests` 同一
#: 量级：低于这个数，注入期的任何退化都分不清信号与噪声。
MIN_EDGE_CALLS = 20

#: 边上 `phase` 属性 -> 传给 `injectability()` 的 reason_text token。
#:
#: ## 为什么必须传 reason_text（2026-09-09 交接书指出的坑）
#:
#: `injectability(src_label, dst_label)` **不带 reason_text 时会返回
#: `injectable`** —— 因为按类型级判定，`Microservice -> ECRRepository`
#: 的源在集群内，轴二（源侧切断）成立。只有传入 `'image-repo-dependency'`
#: 才会命中 `REASON_TOKEN_CLASS` 拿到 `needs_compound_experiment`。
#:
#: 第一版这里硬编码了边形态 `{('DependsOn','Microservice','ECRRepository')}`。
#: 那次**碰巧给出正确答案**（实测 phase='startup' 的 13 条边确实全是这个形态），
#: 但方式是错的：判据应该来自边上的属性，而不是脚本里的一份形态清单。
#: 本仓库已经因为「同类清单各处一份」踩过坑
#: （rca 那份依赖边清单少 Invokes，线上漏 16 条边）。
_PHASE_TO_REASON = {
    'startup': 'image-repo-dependency',
}


def _reason_text_for(props: dict) -> str:
    """从边属性推出该传给 injectability 的 reason_text。

    先看 `phase`，再退回边上已记录的 `verify_reason` 里的 token ——
    后者是既有实验留下的判定依据，比重新猜更可靠。
    """
    phase = (props or {}).get('phase')
    if phase and phase in _PHASE_TO_REASON:
        return _PHASE_TO_REASON[phase]
    # 边上已有的原因文本里可能就带着 token（如 'image-repo-dependency: ...'）
    existing = str((props or {}).get('verify_reason') or '')
    for token in _PHASE_TO_REASON.values():
        if token in existing:
            return token
    return ''


#: DeepFlow **结构性看不见**的目标类型。
#:
#: 2026-09-09 实测：拿 9 条已 confirmed 的边反向验证探针，只有 1 条测出流量。
#: 追下去发现 `petsite-deployment` 在 DeepFlow 里**根本没有**到 RDS 或
#: AWS 服务的流 —— 即使去掉协议过滤，目标也只有 search-service 与 xray-service。
#: 原因是 DeepFlow 抓 L7：MySQL/PostgreSQL 协议与到 AWS 端点的 TLS
#: 都不在它能解析的范围内。
#:
#: 所以对这些目标类型，DeepFlow 返回 0 的含义是「**瞎了**」而不是「无流量」。
#: 当无流量处理会把一批可验的边错标成休眠 —— 与「采集失败必须 ok=False」
#: 是同一条原则。这类边改走 X-Ray（源侧已插桩，看得见 AWS 调用）。
_DEEPFLOW_BLIND_TARGETS = {
    'AWSServiceEndpoint', 'RDSCluster', 'RDSInstance', 'DynamoDBTable',
    'S3Bucket', 'SQSQueue', 'SNSTopic', 'StepFunction', 'NeptuneCluster',
    'AgentRuntime', 'ECRRepository',
}


def _window_seconds() -> int:
    return 900


def _xray_traffic(src: str, dst: str, dst_label: str = '') -> tuple[int | None, str]:
    """X-Ray 服务图边级调用数。AWSServiceEndpoint 目标按 Type 前缀匹配。

    图谱的 AWSServiceEndpoint 是**服务级**抽象（`dynamodb`），而 X-Ray 是
    **资源级**节点（表名），按名字永远匹配不上；`petsite -> ssm` 这条
    已确证的边在 X-Ray 里叫 `PetSite -> SimpleSystemsManagement`。
    """
    from runner import service_names
    from runner.xray_metrics import XRayEdgeMetrics

    prefixes = ()
    if dst_label == 'AWSServiceEndpoint':
        prefixes = service_names.xray_aws_type_prefixes(dst)
        if not prefixes:
            return None, (f'端点名 {dst!r} 没有已知的 X-Ray Type 映射 —— '
                          f'测不出，**不等于**无流量')
    snap = XRayEdgeMetrics().collect_edge_flow(
        src, dst, window_seconds=3600, dst_type_prefixes=prefixes)
    if not snap.ok:
        return None, 'X-Ray 服务图里没有这条边（测不出，不等于无流量）'
    return snap.total_requests, f'X-Ray 近 1h {snap.total_requests} 次'


def _microservice_traffic(src: str, dst: str,
                          dst_label: str = '') -> tuple[int | None, str]:
    """源是 Microservice 时的边级流量，按目标类型选后端。"""
    if dst_label in _DEEPFLOW_BLIND_TARGETS:
        return _xray_traffic(src, dst, dst_label)
    from runner.metrics import DeepFlowMetrics
    snap = DeepFlowMetrics().collect_edge_flow(src, dst,
                                              window_seconds=_window_seconds())
    if not snap.ok:
        return None, 'DeepFlow 采集失败（不等于零流量）'
    return snap.total_requests, f'DeepFlow 近 15min {snap.total_requests} 次'


def _lambda_traffic(src: str, dst: str,
                    dst_label: str = '') -> tuple[int | None, str]:
    """X-Ray 边级；看不到时退到 CloudWatch 源侧调用数。

    退到源侧是**有损**的：源被调用不等于这条边被走到。所以源侧有量时
    返回 None（测不出）而**不是** 0 —— 只有源侧确实为 0 才是休眠。
    """
    n, note = _xray_traffic(src, dst, dst_label)
    if n is not None and n > 0:
        return n, note
    inv = _cw_sum('AWS/Lambda', 'Invocations', 'FunctionName', src)
    if inv is None:
        return None, 'X-Ray 看不到该边且 CloudWatch 查不到源函数'
    if inv == 0:
        return 0, '源函数 24h 零调用 —— 链路休眠'
    return None, (f'源函数 24h 有 {inv} 次调用，但 X-Ray 看不到这条边 —— '
                  f'边级**测不出**（可能未开 Active 追踪），不等于无流量')


def _stepfunction_traffic(src: str, dst: str,
                          dst_label: str = '') -> tuple[int | None, str]:
    n = _cw_sum('AWS/States', 'ExecutionsStarted', 'StateMachineArn', src,
                match_suffix=True)
    if n is None:
        return None, 'CloudWatch 查不到该状态机的执行数'
    if n == 0:
        return 0, '状态机 24h 零执行 —— 链路休眠'
    return None, (f'状态机 24h {n} 次执行，但这是**源侧**不是边级 —— 边级测不出')


def _sns_traffic(src: str, dst: str,
                 dst_label: str = '') -> tuple[int | None, str]:
    n = _cw_sum('AWS/SNS', 'NumberOfMessagesPublished', 'TopicName', src)
    if n is None:
        return None, 'CloudWatch 查不到该主题的发布数'
    if n == 0:
        return 0, '主题 24h 零发布 —— 链路休眠'
    return None, f'主题 24h 发布 {n} 条，但这是源侧不是边级 —— 边级测不出'


def _cw_sum(namespace: str, metric: str, dim: str, value: str,
            match_suffix: bool = False) -> int | None:
    """CloudWatch 24h 求和。查不到返回 None（**不是** 0）。

    None 与 0 必须分开：0 是「确实没有」，None 是「不知道」。
    把 None 当 0 会把一条测不出的边错标成休眠。
    """
    import boto3
    cw = boto3.client('cloudwatch',
                      region_name=os.environ.get('REGION', 'ap-northeast-1'))
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        if match_suffix:
            # StateMachineArn 维度要全 ARN，图谱只有名字 —— 用 ListMetrics
            # 找出后缀匹配的那个维度值，避免自己拼 ARN 拼错。
            paginator = cw.get_paginator('list_metrics')
            found = None
            for page in paginator.paginate(Namespace=namespace,
                                          MetricName=metric):
                for m in page.get('Metrics', []) or []:
                    for d in m.get('Dimensions', []) or []:
                        if d['Name'] == dim and str(d['Value']).endswith(value):
                            found = d['Value']
                            break
                if found:
                    break
            if not found:
                return None
            value = found
        r = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric,
            Dimensions=[{'Name': dim, 'Value': value}],
            StartTime=now - datetime.timedelta(hours=24), EndTime=now,
            Period=86400, Statistics=['Sum'])
        pts = r.get('Datapoints') or []
        return int(sum(p['Sum'] for p in pts)) if pts else 0
    except Exception:                                          # noqa: BLE001
        return None


_PROBES = {
    'Microservice': _microservice_traffic,
    'LambdaFunction': _lambda_traffic,
    'StepFunction': _stepfunction_traffic,
    'SNSTopic': _sns_traffic,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--annotate', action='store_true',
                    help='把无流量的边标进图谱（precondition_unmet）')
    args = ap.parse_args()

    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    os.environ.setdefault('REGION', 'ap-northeast-1')

    import yaml
    from neptune import neptune_client as nc

    gc = yaml.safe_load((ROOT / 'profiles' / 'graph_contract.yaml')
                        .read_text(encoding='utf-8'))
    labels = sorted(k for k, v in (gc.get('edge_types') or {}).items()
                    if isinstance(v, dict) and v.get('dependency'))

    rows = nc.results(f"""
MATCH (a)-[r:{'|'.join(labels)}]->(b)
WHERE (r.verify_status IS NULL OR r.verify_status='untested')
  AND r.verify_blocked_class IS NULL
RETURN type(r) AS edge, labels(a)[0] AS sl, a.name AS src,
       labels(b)[0] AS dl, b.name AS dst, properties(r) AS props
""")
    print(f'待验边（untested 且未被标阻断）: {len(rows)} 条')
    print(f'边级流量下限: {MIN_EDGE_CALLS} 次 / {_window_seconds()}s\n')

    verifiable, no_traffic, compound, unknown = [], [], [], []
    for r in rows:
        # 判定来自 injectability，reason_text 由边上的 phase 推出 ——
        # 不再用脚本里的形态清单（见 _PHASE_TO_REASON 的说明）。
        from runner import injectability as _inj
        _verdict, _why = _inj.injectability(
            r['sl'], r['dl'], _reason_text_for(r.get('props') or {}))
        if _verdict == _inj.NEEDS_COMPOUND:
            compound.append({**r, 'note': _why})
            continue
        probe = _PROBES.get(r['sl'])
        if probe is None:
            unknown.append({**r, 'note': f"没有针对源类型 {r['sl']} 的流量探针"})
            continue
        n, note = probe(r['src'], r['dst'], r['dl'])
        rec = {**r, 'calls': n, 'note': note}
        if n is None:
            unknown.append(rec)
        elif n >= MIN_EDGE_CALLS:
            verifiable.append(rec)
        else:
            no_traffic.append(rec)

    print(f'✅ 有流量、现在就能验 : {len(verifiable)} 条')
    for v in verifiable:
        print(f"   {v['edge']:<13} {v['src'][:30]:<30} -> {v['dst'][:30]:<30} {v['note']}")
    print(f'\n⏸  无流量（先造流量）  : {len(no_traffic)} 条')
    agg = collections.Counter(f"{x['edge']} {x['sl']}->{x['dl']}" for x in no_traffic)
    for k, c in agg.most_common():
        print(f'   {k:<50} {c:>3} 条')
    print(f'\n🔁 需复合实验          : {len(compound)} 条')
    print(f'❓ 探针测不出          : {len(unknown)} 条')
    for u in unknown[:8]:
        print(f"   {u['edge']:<13} {u['src'][:28]:<28} -> {u['dst'][:24]:<24} {u['note']}")

    out = ROOT / 'todo' / (
        'preflight-edge-traffic_'
        + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
        + '.json')
    out.write_text(json.dumps(
        {'verifiable': verifiable, 'no_traffic': no_traffic,
         'compound': compound, 'unknown': unknown},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n明细已写: {out.relative_to(ROOT)}')

    if not args.annotate:
        print('（未标注。加 --annotate 把无流量的边写进图谱）')
        return 0

    from runner import injectability as inj
    n = 0
    for rec in no_traffic:
        reason = (f"dependency-path-dormant: 被测路径近 "
                  f"{_window_seconds()}s 只有 {rec['calls']} 次调用"
                  f"（需 >= {MIN_EDGE_CALLS}）—— {rec['note']}。"
                  f"环境前提，有流量后应重测").replace("'", "\\'")
        nc.results(f"""
MATCH (a)-[r:{rec['edge']}]->(b)
WHERE a.name = $src AND b.name = $dst
  AND (r.verify_status IS NULL OR r.verify_status='untested')
SET r.verify_blocked_reason = '{reason}',
    r.verify_blocked_class = '{inj.PRECONDITION_UNMET}'
RETURN count(r) AS n
""", {'src': rec['src'], 'dst': rec['dst']})
        n += 1
    for rec in compound:
        reason = ('image-repo-dependency: 依赖只在 Pod 启动拉镜像时被用到，'
                  '稳态无可观测退化。配合删 Pod 是可验证的复合实验，'
                  '**不是**不可注入').replace("'", "\\'")
        nc.results(f"""
MATCH (a)-[r:{rec['edge']}]->(b)
WHERE a.name = $src AND b.name = $dst
  AND (r.verify_status IS NULL OR r.verify_status='untested')
SET r.verify_blocked_reason = '{reason}',
    r.verify_blocked_class = '{inj.NEEDS_COMPOUND}'
RETURN count(r) AS n
""", {'src': rec['src'], 'dst': rec['dst']})
        n += 1
    print(f'已标注 {n} 条边')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

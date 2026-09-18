"""实测 per-runtime 日志组里 span 的真实属性名，不按 OTel semconv 规范假设。

这是 `02-agent可观测性方案` 第 5 步那条纪律的执行：
「实测各框架实际发出的 span 属性名，据此写 legacy-compat 映射表。**不要按 semconv 规范假设**。」

之所以必须先做这一步再改 SPAN_LOG_GROUP：ETL 的 SPAN_QUERY 用了 6 个字段，
我只验证过 3 个能命中。剩下 3 个若名字不对，改对日志组后 Delegates/Retrieves
边仍然出不来，而现象与「日志组没改对」完全一样 —— 分不清就会误判。
"""
import json
import time
from collections import Counter, defaultdict

import boto3

logs = boto3.client('logs', region_name='ap-northeast-1')
now = int(time.time())
LOOKBACK = 6 * 3600

groups = []
p = logs.get_paginator('describe_log_groups')
for page in p.paginate(logGroupNamePrefix='/aws/bedrock-agentcore/runtimes/'):
    groups += [g['logGroupName'] for g in page['logGroups']]
print(f"找到 {len(groups)} 个 agent runtime 日志组\n")


def insights(lg_list, q):
    qid = logs.start_query(logGroupNames=lg_list, startTime=now - LOOKBACK,
                           endTime=now, queryString=q)['queryId']
    for _ in range(90):
        r = logs.get_query_results(queryId=qid)
        if r.get('status') == 'Complete':
            return [{c['field']: c['value'] for c in row} for row in r.get('results', [])]
        if r.get('status') in ('Failed', 'Cancelled', 'Timeout'):
            raise RuntimeError(r.get('status'))
        time.sleep(1)
    logs.stop_query(queryId=qid)
    raise TimeoutError()


# 取原始 span JSON，自己解析出全部属性键 —— 比 Insights 的 fields 更可靠，
# 因为 Insights 对不存在的字段静默返回空，无法区分「没这个键」与「值为空」。
raw = insights(groups, 'fields @message | filter @message like /gen_ai/ | limit 400')
print(f"取到 {len(raw)} 条含 gen_ai 的 span\n")

attr_keys = Counter()
res_keys = Counter()
samples = defaultdict(set)
span_names = Counter()
ops = Counter()


def walk(prefix, obj, bucket):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                walk(f'{prefix}{k}.', v, bucket)
            else:
                bucket[f'{prefix}{k}'] += 1
                if v not in (None, '') and len(samples[f'{prefix}{k}']) < 3:
                    samples[f'{prefix}{k}'].add(str(v)[:60])


for r in raw:
    try:
        m = json.loads(r['@message'])
    except Exception:
        continue
    span_names[m.get('name', '?')] += 1
    walk('', m.get('attributes') or {}, attr_keys)
    walk('', (m.get('resource') or {}).get('attributes') or {}, res_keys)

print("=== span name 分布（前 12）===")
for n, c in span_names.most_common(12):
    print(f"  {c:>5}  {n}")

print("\n=== attributes.* 里所有 gen_ai / aws 相关键（实测）===")
for k, c in sorted(attr_keys.items()):
    if 'gen_ai' in k or 'aws' in k or 'tool' in k or 'agent' in k:
        s = ' | '.join(sorted(samples[k])[:2])
        print(f"  {c:>5}  attributes.{k:<44} 例: {s[:70]}")

print("\n=== resource.attributes.* 全部键（实测）===")
for k, c in sorted(res_keys.items()):
    s = ' | '.join(sorted(samples[k])[:2])
    print(f"  {c:>5}  resource.attributes.{k:<38} 例: {s[:70]}")

print("\n=== ETL 的 SPAN_QUERY 用的 6 个字段，逐个核对是否真实存在 ===")
ETL_FIELDS = [
    ('attributes', 'gen_ai.operation.name'),
    ('resource', 'cloud.resource_id'),
    ('attributes', 'gen_ai.tool.name'),
    ('attributes', 'gen_ai.agent.name'),
    ('attributes', 'gen_ai.request.model'),
    ('attributes', 'aws.bedrock.knowledge_base.id'),
]
for where, f in ETL_FIELDS:
    bucket = attr_keys if where == 'attributes' else res_keys
    hit = bucket.get(f, 0)
    mark = '✅ 存在' if hit else '❌ 不存在'
    alt = ''
    if not hit:
        tail = f.rsplit('.', 1)[-1]
        cands = [k for k in list(attr_keys) + list(res_keys) if tail in k]
        if cands:
            alt = f'  可能是: {", ".join(sorted(set(cands))[:3])}'
    print(f"  {mark}  {where}.{f:<40} 出现 {hit} 次{alt}")

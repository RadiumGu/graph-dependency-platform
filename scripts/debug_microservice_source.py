#!/usr/bin/env python3
"""Query Neptune for petstatusupdater Microservice source."""
import os
import sys
os.environ['AWS_REGION'] = 'ap-northeast-1'
os.environ['AWS_DEFAULT_REGION'] = 'ap-northeast-1'
os.environ['NEPTUNE_ENDPOINT'] = 'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com'

sys.path.insert(0, '/home/ubuntu/tech/graph-dependency-platform/infra/lambda/shared/python')
from neptune_client_base import neptune_query

# 查所有 Microservice 节点的 fault_boundary / service_type / source
q = "g.V().hasLabel('Microservice').valueMap('name','fault_boundary','service_type','source','managedBy','az','replica_count').toList()"
r = neptune_query(q)
import json
data = r.get('result', {}).get('data', {}).get('@value', [])
print(f"Found {len(data)} Microservice nodes:")
for item in data:
    props = item.get('@value', item) if isinstance(item, dict) else item
    # Gremlin ValueMap 返回的结构: {@type: "g:Map", @value: [k1, [v1], k2, [v2]]}
    if isinstance(props, dict) and '@value' in props:
        kv = props['@value']
    else:
        kv = props
    out = {}
    if isinstance(kv, list):
        for i in range(0, len(kv), 2):
            k = kv[i]
            v = kv[i+1]
            if isinstance(v, dict) and '@value' in v:
                v = v['@value']
            if isinstance(v, list) and len(v) == 1:
                v = v[0]
            out[k] = v
    print(json.dumps(out, ensure_ascii=False, default=str))

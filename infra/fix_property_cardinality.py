#!/usr/bin/env python3
"""
fix_property_cardinality.py — 规约 Neptune 顶点属性的多值累积（存量清理）

## 背景

Gremlin/Neptune 的顶点属性默认是 **SET 基数**：不带 `Cardinality.single` 的写入
会「追加」而不是「替换」。同一节点的同一属性因此可以存多个值，
而读属性时 Neptune 按值组合**扇出成笛卡尔积**：

    MATCH (n:LambdaFunction) RETURN labels(n)[0], count(*)          → 31    （不碰属性）
    MATCH (n:LambdaFunction) WHERE n.last_scanned IS NOT NULL       → 9 个真节点 / 1,253 行

实测最坏：单节点 **172 个不同的 `last_scanned` 值**（≈ etl_cfn 的运行次数）。

## 写入侧已修（本脚本只管存量）

- `etl_cfn/neptune_etl_cfn.py:get_or_create_vertex` —— 唯一还在流血的源，
  onMatch map 里的 `last_scanned` 每轮新增一个值。已改为尾部 `property(single)`。
- `etl_deepflow/neptune_etl_deepflow.py:batch_upsert_nodes` —— option-map 里的
  刷新字段改为尾部 `property(single)`，并兑现原 NOTE 承诺的 `az` single 更新。
- `etl_aws/neptune_client.py:upsert_vertex` —— **本来就有**尾部 single 兜底，无需改。

## 三类属性、三种规约策略

| 类别 | 例子 | 策略 |
|---|---|---|
| 死属性（当前无任何写入者） | `avg_duration_ms` | 直接全量 drop |
| 时间戳数值 | `last_updated` / `last_scanned` / `created_at` | 取自身值列表的 **max** → drop → single 重写 |
| 非时间戳标量 | `managedBy` / `az` / `error_rate` … | drop 全部，等权威 ETL 下一轮用 single 重填 |

**为什么非时间戳标量不能「按 last_updated 最新的那个值保留」**：
Neptune 的多值属性**不为每个 value 携带逐值时间戳**，值级 provenance 已经丢失，
无法把某个 `managedBy` 值关联到某个 `last_updated` 值。所以只能 drop 后重填
（etl_aws 现在就是 single 基数，重跑即修复），或按固定优先级确定性选一个。

`managed_by`（蛇形）是 etl_aws 内部改名断层留下的旧属性名，当前代码只写驼峰
`managedBy`，因此蛇形值并入驼峰后整属性 drop。

## 用法

    python3 fix_property_cardinality.py --dry-run     # 只报告，不改（默认）
    python3 fix_property_cardinality.py --apply       # 实际执行

环境变量：NEPTUNE_ENDPOINT（不带端口）、NEPTUNE_PORT（默认 8182）、
REGION / AWS_DEFAULT_REGION。

**需要 `neptune-db:DeleteDataViaQuery` 权限** —— drop 属性是删除操作。
（Neptune 授予的是一条查询「可能执行的动作」的并集，此前 Fix B 已为相关角色补过该动作。）
"""

import argparse
import json
import os
import sys
from collections import defaultdict

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', '').replace('https://', '').split(':')[0]
PORT = os.environ.get('NEPTUNE_PORT', '8182')
REGION = os.environ.get('REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'ap-northeast-1'

# 当前代码树里没有任何写入者的死属性 —— 直接删
DEAD_PROPERTIES = ['avg_duration_ms']

# 时间戳数值：取自身列表 max 后 single 重写
TIMESTAMP_PROPERTIES = ['last_updated', 'last_scanned', 'created_at',
                        'cw_updated_at', 'metrics_updated_at', 'nfm_updated_at']

# 旧属性名 → 新属性名（值并入后删除旧名）
RENAMED_PROPERTIES = {'managed_by': 'managedBy'}

# 语义上确实可能有多条的属性：合并成一个分隔字符串，保留全部信息
# （`evidence` 记录「这条依赖的证据来源」，多个 CFN 声明会产生多条。
#   直接 drop 会丢信息；而不同 label 上类型不一致
#   （RDSCluster.evidence 是 LIST、S3Bucket.evidence 是 STRING）会让查询行为不一致，
#   所以统一成单个 '; ' 连接的字符串。）
JOINABLE_PROPERTIES = ['evidence']
JOIN_SEP = '; '


def _session():
    """复用一个 boto3 Session —— 每次调用新建 Session 约 9.7ms，批量操作下不可忽略。"""
    if not hasattr(_session, '_s'):
        _session._s = boto3.Session()
    return _session._s


def gremlin(query: str):
    """对 Neptune 发一条 Gremlin 查询（SigV4 签名）。"""
    url = f'https://{ENDPOINT}:{PORT}/gremlin'
    body = json.dumps({'gremlin': query})
    req = AWSRequest(method='POST', url=url, data=body,
                     headers={'Content-Type': 'application/json'})
    creds = _session().get_credentials()
    if creds is None:
        raise RuntimeError('拿不到 AWS 凭证')
    SigV4Auth(creds.get_frozen_credentials(), 'neptune-db', REGION).add_auth(req)
    resp = requests.post(url, data=body, headers=dict(req.headers), verify=False, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _values(resp):
    return resp.get('result', {}).get('data', {}).get('@value', [])


def _plain(v):
    """把 GraphSON 的包装拆成原生 Python 结构。

    注意 `g:Map` 的 `@value` 是**扁平的 [k1,v1,k2,v2,…] 列表**，
    不是 dict —— 必须按类型标签判断后成对折叠回 dict。
    早先版本对所有 `@value` 一律当列表递归，结果 map 被拆成扁平 list，
    调用方的 `isinstance(d, dict)` 判断把每一行都跳过了，
    扫描于是静默返回「未发现多值属性」——一个假阴性。
    """
    if isinstance(v, dict):
        t = v.get('@type')
        if t == 'g:Map':
            flat = [_plain(x) for x in v.get('@value', [])]
            return dict(zip(flat[0::2], flat[1::2]))
        if '@value' in v:
            return _plain(v['@value'])
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def scan_multivalued():
    """扫出所有「同一顶点同一属性有多个值」的 (label, property, vid, values)。"""
    # 先拿全部 label
    labels = [_plain(x) for x in _values(gremlin("g.V().label().dedup()"))]
    findings = defaultdict(list)   # (label, prop) → [(vid, n_values)]
    for lb in labels:
        # 对该 label 的每个顶点，列出「属性名 → 值个数」中个数 > 1 的
        q = (f"g.V().hasLabel('{lb}').project('id','props')"
             f".by(id).by(properties().group().by(key()).by(count()))")
        try:
            rows = _values(gremlin(q))
        except Exception as e:
            print(f"  ⚠ label {lb} 扫描失败: {e}", file=sys.stderr)
            continue
        for row in rows:
            d = _plain(row)
            if not isinstance(d, dict):
                print(f"  ⚠ label {lb} 返回了非 map 结构，跳过：{type(d)}", file=sys.stderr)
                continue
            vid = d.get('id')
            props = d.get('props') or {}
            for k, cnt in props.items():
                if isinstance(cnt, int) and cnt > 1:
                    findings[(lb, k)].append((vid, cnt))
    return findings


def collapse_timestamp(vid, prop, apply_changes):
    """时间戳属性：取自身值列表的 max，drop 后 single 重写。"""
    vals = _plain(_values(gremlin(f"g.V('{vid}').properties('{prop}').value()")))
    nums = [int(v) for v in vals if isinstance(v, (int, float))
            or (isinstance(v, str) and str(v).isdigit())]
    if not nums:
        return None
    keep = max(nums)
    if apply_changes:
        gremlin(f"g.V('{vid}').properties('{prop}').drop()")
        gremlin(f"g.V('{vid}').property(single,'{prop}',{keep})")
    return keep


def drop_property(vid, prop, apply_changes):
    if apply_changes:
        gremlin(f"g.V('{vid}').properties('{prop}').drop()")
    return True


def _esc(v):
    return str(v).replace('\\', '\\\\').replace("'", "\\'")


def keep_one(vid, prop, apply_changes):
    """非时间戳标量：确定性保留一个值，drop 其余。**绝不让属性变成缺失。**

    为什么不 drop 全部等 ETL 重填：`namespace` / `ip` 这类属性有查询在读，
    drop 掉会在下一轮 ETL 之前（deepflow 5 分钟、aws 15 分钟）造成属性缺失，
    读到 null 比读到一个过期一轮的值更糟。保留一个值最坏是陈旧 5 分钟，
    而且下一轮 single 写入就会纠正。

    确定性规则：数值取 max，字符串取排序后最后一个。
    Neptune 的多值属性不带逐值时间戳，所以「哪个是最新的」无法判定，
    只能保证**同样的输入每次得到同样的结果**（可复现优先于猜准）。
    """
    vals = _plain(_values(gremlin(f"g.V('{vid}').properties('{prop}').value()")))
    if not vals:
        return None
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if nums and len(nums) == len(vals):
        keep = max(nums)
        lit = repr(keep)
    else:
        keep = sorted(str(v) for v in vals)[-1]
        lit = f"'{_esc(keep)}'"
    if apply_changes:
        gremlin(f"g.V('{vid}').properties('{prop}').drop()")
        gremlin(f"g.V('{vid}').property(single,'{prop}',{lit})")
    return keep


def join_values(vid, prop, apply_changes):
    """语义上可多条的属性：排序去重后用 JOIN_SEP 连接成单个字符串，保留全部信息。"""
    vals = _plain(_values(gremlin(f"g.V('{vid}').properties('{prop}').value()")))
    if not vals:
        return None
    merged = JOIN_SEP.join(sorted({str(v) for v in vals}))
    if apply_changes:
        gremlin(f"g.V('{vid}').properties('{prop}').drop()")
        gremlin(f"g.V('{vid}').property(single,'{prop}','{_esc(merged)}')")
    return merged


def merge_renamed(vid, old, new, apply_changes):
    """把旧属性名的值并入新名（新名缺失时才补），然后删除旧名。"""
    old_vals = _plain(_values(gremlin(f"g.V('{vid}').properties('{old}').value()")))
    new_vals = _plain(_values(gremlin(f"g.V('{vid}').properties('{new}').value()")))
    action = 'drop_old_only'
    if old_vals and not new_vals:
        action = f'copy_to_{new}'
        if apply_changes:
            v = old_vals[0]
            gremlin(f"g.V('{vid}').property(single,'{new}','{v}')")
    if apply_changes:
        gremlin(f"g.V('{vid}').properties('{old}').drop()")
    return action


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='实际执行；缺省为 dry-run')
    ap.add_argument('--dry-run', action='store_true',
                    help='只报告不改（默认行为，显式写出便于在脚本里表达意图）')
    args = ap.parse_args()
    if args.apply and args.dry_run:
        print('错误：--apply 与 --dry-run 互斥', file=sys.stderr)
        return 2
    apply_changes = args.apply

    if not ENDPOINT:
        print('错误：未设置 NEPTUNE_ENDPOINT', file=sys.stderr)
        return 2

    mode = 'APPLY（会写入）' if apply_changes else 'DRY-RUN（只报告）'
    print(f"Neptune 顶点属性基数规约 — {mode}")
    print(f"  endpoint: {ENDPOINT}:{PORT}  region: {REGION}\n")

    print("扫描多值属性 …")
    findings = scan_multivalued()
    if not findings:
        print("  ✅ 未发现任何多值属性")
        return 0

    total_nodes = 0
    total_extra = 0
    print(f"\n发现 {len(findings)} 个 (label, 属性) 组合存在多值：\n")
    for (lb, prop), items in sorted(findings.items(), key=lambda x: -max(c for _, c in x[1])):
        worst = max(c for _, c in items)
        total_nodes += len(items)
        total_extra += sum(c - 1 for _, c in items)
        if prop in DEAD_PROPERTIES:
            plan = 'DROP（当前代码无写入者，死属性）'
        elif prop in TIMESTAMP_PROPERTIES:
            plan = '取 max → drop → single 重写'
        elif prop in RENAMED_PROPERTIES:
            plan = f"并入 {RENAMED_PROPERTIES[prop]} 后 drop"
        elif prop in JOINABLE_PROPERTIES:
            plan = f"去重排序后用 '{JOIN_SEP}' 合并成单个字符串（保留全部信息）"
        else:
            plan = '确定性保留一个值（数值取 max / 字符串取排序末位），drop 其余'
        print(f"  {lb}.{prop}: {len(items)} 个节点，最多 {worst} 个值 → {plan}")

    print(f"\n合计 {total_nodes} 个 (节点,属性) 需要规约，"
          f"消除 {total_extra} 个冗余值。")

    if not apply_changes:
        print("\ndry-run 结束。加 --apply 才会实际执行。")
        return 0

    print("\n开始执行 …")
    done = 0
    for (lb, prop), items in findings.items():
        for vid, _cnt in items:
            try:
                if prop in DEAD_PROPERTIES:
                    drop_property(vid, prop, True)
                elif prop in TIMESTAMP_PROPERTIES:
                    collapse_timestamp(vid, prop, True)
                elif prop in RENAMED_PROPERTIES:
                    merge_renamed(vid, prop, RENAMED_PROPERTIES[prop], True)
                elif prop in JOINABLE_PROPERTIES:
                    join_values(vid, prop, True)
                else:
                    keep_one(vid, prop, True)
                done += 1
            except Exception as e:
                print(f"  ⚠ {lb}.{prop} on {vid} 失败: {e}", file=sys.stderr)

    print(f"\n✅ 已规约 {done} 个 (节点,属性)。")
    print("提示：被 drop 的非时间戳标量需要 etl_aws / etl_deepflow 下一轮跑完才会重填。")

    print("\n复核 …")
    left = scan_multivalued()
    if left:
        print(f"  ⚠ 仍有 {len(left)} 个组合多值：{sorted(left.keys())}")
        return 1
    print("  ✅ 已无多值属性")
    return 0


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    sys.exit(main())

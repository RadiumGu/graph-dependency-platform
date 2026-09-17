#!/usr/bin/env python3
"""把「观测充足、但成功率通道恒为 0% 因而不可验证」的边标成 precondition_unmet。

## 这是哪一类边

`petsearch -[AccessesData]-> serviceseks2-s3bucketpetadoption...`

2026-09-17 用已提交版 `verify_via_iam_deny.py` 跑它，探针的基线闸门拒绝开跑：

    被测边: {'success_rate': 0.0, 'total_requests': 138, 'p99_ms': 423.8}
    ✗ 基线成功率 0.00% < 下限 95% —— 拒绝开跑。
      基线本身已经是坏的，拿它算退化 delta 毫无意义。

**闸门是对的。** 但探针提示的常见原因（上次实验的 deny 残留）**不成立** ——
`searchserviceServiceAccountRoleDefaultPolicy608C0257` 里没有任何 Deny 语句。

X-Ray 服务图（近 15min）：

    PetSearch → s3bucketpetadoption...   total=634  ok=0  err=634  fault=0
    PetSearch → ddbpetadoption...        total=4535 ok=4535 err=0
    PetSearch → STS                      total=4    ok=4    err=0

634 次全 4xx、零 5xx、零成功。解码 trace 得到原因：

    S3  op=CreateBucket  status=409  BucketAlreadyOwnedByYouException

`petsearch` 每次都去 CreateBucket，桶已存在于是 409。**应用本身是好的**
（DynamoDB Scan 200、业务探针 home=26 稳定）。

## 为什么要标，而不是留着让人反复去试

这条边**观测充足（634 次）却结构性不可验证**：成功率通道恒为 0%，
任何 deny 注入都产生不了可测的 delta。不标的话，它会一直躺在
「有流量、现在就能验」的清单里，每个接手的人都会再跑一次、再被闸门拦一次。

分类用 `precondition_unmet` —— 词表里现有的三个值之一，语义正合：
探针的前置条件「基线成功率 ≥ 95%」未满足。
**刻意不新造一个类别**：词表只有
`precondition_unmet` / `unreachable_by_any_backend` / `needs_compound_experiment`，
加第四个要动词表与所有消费方，而这条边并不需要一个新类别才能说清楚 ——
`verify_blocked_reason` 里写清「为什么前置条件不满足」就够了。

## 与 unreachable_by_any_backend 的区别

    unreachable_by_any_backend   没有任何后端能对这个目标类型施加故障
    precondition_unmet（本例）   后端有（IAM deny s3:*），但测量通道已坏

## 刻意只处理这一条

`scripts/reclassify_blocked_edges.py` 是批量重分类，没有单边参数 ——
为一条边跑批量脚本会顺带改别的边。本脚本的匹配条件写死成这一条，
且执行前后都打印该边的属性供核对。

## 换证据通道才是真解

标注只是防止重复劳动。要真正验证这条边，得像 `74a9369` 给
`petsite -> StepFunction` 那样换证据通道：以**业务探针**为主
（搜索结果里的图片还能不能取到），边级成功率为辅。

⚠️ 另有一层**推断**（未读 petsearch 源码，故不写进图谱属性）：
这条边的真实数据路径可能根本不在这些 API 调用里 —— 图片是通过**预签名 URL**
交付的，预签名是本地密码学操作、不调用 S3 API，浏览器直取，
因此永远不出现在 petsearch 的 trace 里。若如此，deny s3:* 反而**会**影响真实
路径（预签名请求按签名者权限评估），但**观测不到**（请求不经 petsearch）。
这一层留在 todo/CROSS-SESSION-NOTE 里，不写进图谱。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / 'rca')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from neptune import neptune_client as nc          # noqa: E402

_SRC = 'petsearch'
_DST_MARK = 's3bucketpetadoption'
_EDGE = 'AccessesData'

_CLASS = 'precondition_unmet'
_REASON = (
    '基线成功率恒为 0%，成功率通道结构性失效：X-Ray 近 15min 该边 '
    'total=634 / ok=0 / err=634 / fault=0，全部是 CreateBucket 返回 409 '
    'BucketAlreadyOwnedByYouException（petsearch 每次都尝试建桶，桶已存在）。'
    '应用本身正常（同期 DynamoDB Scan 200、业务探针 home=26 稳定）。'
    '已排除实验残留：searchserviceServiceAccountRoleDefaultPolicy608C0257 '
    '无任何 Deny 语句。'
    '后端手段是有的（IAM deny s3:*），但拿一个 0% 的基线算退化 delta 无意义，'
    'verify_via_iam_deny 的基线闸门因此正确拒绝开跑。'
    '要验这条边须换证据通道：以业务探针（图片可取性）为主、边级成功率为辅，'
    '参照 74a9369 对 petsite -> StepFunction 的做法。'
)


def _fetch():
    return nc.results(
        f"MATCH (a)-[e:{_EDGE}]->(b) "
        f"WHERE coalesce(a.name,a.arn) = '{_SRC}' "
        f"AND b.name CONTAINS '{_DST_MARK}' "
        "RETURN coalesce(a.name,a.arn) AS src, b.name AS dst, "
        "coalesce(e.verify_status,'untested') AS status, "
        "e.verify_blocked_class AS blocked_class, "
        "e.verify_blocked_reason AS blocked_reason, "
        "e.xray_call_count AS xray_calls")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真的写入（默认只看）')
    args = ap.parse_args()

    before = _fetch()
    if len(before) != 1:
        print(f'✗ 匹配到 {len(before)} 条边，期望恰好 1 条。不动手。')
        for r in before:
            print(f'   {r}')
        return 1

    b = before[0]
    print('=== 当前状态 ===')
    print(f"  {b['src']} -[{_EDGE}]-> {b['dst']}")
    print(f"  verify_status        {b['status']}")
    print(f"  verify_blocked_class {b['blocked_class']}")
    print(f"  xray_call_count      {b['xray_calls']}")

    if b['status'] not in ('untested', None):
        print(f"\n✗ 该边已有判定（{b['status']}），不覆盖。"
              " 本脚本只标注未测的边。")
        return 1
    if b['blocked_class'] == _CLASS:
        print('\n已经是 precondition_unmet，无需重复标注。')
        return 0

    print(f'\n=== 将写入 ===\n  verify_blocked_class = {_CLASS}')
    print(f'  verify_blocked_reason = {_REASON[:100]}…')

    if not args.apply:
        print('\n（默认只看，加 --apply 写入。）')
        return 0

    nc.query(
        f"MATCH (a)-[e:{_EDGE}]->(b) "
        f"WHERE coalesce(a.name,a.arn) = '{_SRC}' "
        f"AND b.name CONTAINS '{_DST_MARK}' "
        "SET e.verify_blocked_class = $c, e.verify_blocked_reason = $r",
        {'c': _CLASS, 'r': _REASON})

    after = _fetch()[0]
    print('\n=== 写入后复验 ===')
    print(f"  verify_blocked_class {after['blocked_class']}")
    print(f"  verify_status        {after['status']}  "
          f"（必须仍是 untested —— 标注不等于判定）")
    ok = (after['blocked_class'] == _CLASS
          and after['status'] in ('untested', None))
    print('  ✅ 一致' if ok else '  ❌ 不一致')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

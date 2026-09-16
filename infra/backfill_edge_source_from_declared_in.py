"""从边自己记录的 `declared_in` 回填缺失的 `source`。

## 这不是猜，是读另一个字段里已经记着的事实

我上一轮在 audit 的日志里写了「补不了（事后无从推断当初是哪个源写的）」——
**那句话对一部分边是错的**，本脚本就是它的反例。

`neptune-etl-trigger -[AccessesData]-> neptune-etl-from-aws` 没有 source，
但它身上带着 etl_cfn 的三个签名字段：

    declared_in  = cfn                      ← 谁声明的，直接写在边上
    stack_name   = NeptuneEtlStack          ← 只有 etl_cfn 会写
    evidence     = env:ETL_FUNCTION_NAME    ← CFN 模板里的环境变量引用

再加一条旁证：`declared_in='cfn'` 的 7 条边里有 4 条带 `source='cfn-etl'` ——
同批同法创建的兄弟边取值一致。所以这里的 `cfn-etl` 是**读出来的**，不是猜的。

更强的一条依据是排除法：`etl_aws` 每次写 `AccessesData` 边都带
`{'source': 'aws-etl'}`（`handler.py:401/422/958/1040`），而 `upsert_edge` 的
`coalesce(values('source'), constant(...))` 在 source 为空时**会填上**。
这条边的 source 是空的 → **`etl_aws` 从未写过它** → 已知写过它的只有 cfn。

## 三个字段的证明力不相等（2026-09-06 查实，别一视同仁）

    declared_in='cfn'   只有 etl_cfn 写（neptune_etl_cfn.py:170）      ✅ 排他
    stack_name          只有 etl_cfn 写                                ✅ 排他
    evidence='env:X'    etl_aws 与 etl_cfn 都写                        ❌ 不排他

`evidence` 的 `env:` 前缀在 `handler.py:401/422/958/1040` 与
`neptune_etl_cfn.py:307` 两边都出现 —— 它记录「凭什么断定有这条依赖」，
不是「谁断定的」。**所以映射只依据排他字段 `declared_in`。**

## 另 2 条 declared_in='cfn' 却写着 source='aws-etl' 的边不在本脚本范围内

`statusupdater→ddbpetadoption`、`dynamodbquery→ddbpetadoption`。
**那不是矛盾，不要去「更正」**：etl_aws 对它们有自己的独立证据（前者
`evidence=source:petstatusupdater/index.js#UpdateCommand` 是代码扫描，后者
`env:DYNAMODB_TABLE_NAME` 是环境变量扫描），每轮都在写。两个字段回答不同问题 ——
`declared_in` 是哪个 CFN 栈声明了这些资源，`source` 是哪个 ETL 发现了这条依赖。

（本脚本第一版的注释曾断言这 2 条是「写一次属性被无条件覆盖那个时代的产物，
etl_aws 把 cfn 先写的 source 改掉了」—— **那是推测，且已被证伪**：etl_cfn 自己
写 source 就是幂等的（`neptune_etl_cfn.py:157-159` 的 `once_chain`），
而其中一条走的是幂等的 `upsert_edge`，结构上不可能覆盖任何人。）

## 为什么这条边一直没被清理掉

它的 `last_seen` 与 `xray_last_seen` **完全相等**，带 `xray_call_count=556` ——
**etl_xray 的印证路径每轮都在刷新它的 last_seen，却不写 source**（xray 没有发现
它，不该冒领 source，这个行为本身是对的）。而 `last_scanned` 是 148 天前，
说明真正的创建者 etl_cfn 早就不再扫到它。

后果是一个不显眼的死角：**被别的源印证、却无人认领的边是不死的**。
清理判据要求 `active=false`，而 `active=false` 要求 TTL 过期，TTL 过期要求
`last_seen` 陈旧 —— 印证行为让 `last_seen` 永远新鲜，于是这条边既不会被清理，
也永远不会有人负责。这与「孤儿边」是同一件事的更隐蔽版本。

## 为什么不写 manual-fix

契约词表里有 `manual-fix`，但用它会**盖掉真实溯源**：这条边确实是 cfn 发现的，
写成「人工修的」等于把已知的事实换成一句关于修补动作的描述。
真实来源写进 source，回填这个事实写进 evidence —— 两件事分开记。
"""
import argparse
import sys

sys.path.insert(0, 'infra/lambda/shared/python')
from graph_contract import SOURCES  # noqa: E402
from neptune_client_base import neptune_query  # noqa: E402

# declared_in 取值 → 契约里对应的 source。只列有确凿对应关系的。
DECLARED_IN_TO_SOURCE = {
    'cfn': 'cfn-etl',
    'etl_aws': 'aws-etl',
}


def rows(q):
    r = neptune_query(q)['result']['data']['@value']
    if not r:
        return []
    out = []
    for x in r[0]['@value']:
        it = iter(x['@value'])
        d = dict(zip(it, it))
        out.append({k: (v['@value'] if isinstance(v, dict) else v)
                    for k, v in d.items()})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='真正回填；不给就是 dry-run')
    args = ap.parse_args()

    for k, v in DECLARED_IN_TO_SOURCE.items():
        assert v in SOURCES, f'映射目标 {v} 不在契约 sources 词表里'

    print(f"=== 从 declared_in 回填缺失的 source"
          f"（{'写入' if args.write else 'dry-run'}）===\n")

    total = 0
    for declared, src in sorted(DECLARED_IN_TO_SOURCE.items()):
        sel = (f"g.E().hasNot('source').has('declared_in','{declared}')")
        rs = rows(sel + ".project('l','s','d','ev','st').by(label())"
                        ".by(__.outV().values('name')).by(__.inV().values('name'))"
                        ".by(coalesce(values('evidence'),constant('-')))"
                        ".by(coalesce(values('stack_name'),constant('-'))).fold()")
        if not rs:
            continue
        print(f"【declared_in={declared} → source={src}】{len(rs)} 条")
        for r in rs:
            print(f"    {r['s']} -[{r['l']}]-> {r['d']}")
            print(f"      evidence={r['ev']}  stack_name={r['st']}")
        total += len(rs)
        if args.write:
            # 幂等：coalesce 保证已有 source 的边不被覆盖（此处 hasNot 已筛过，
            # 双保险是为了这个脚本被重复执行时行为仍然确定）
            neptune_query(
                sel + f".property('source',__.coalesce(__.values('source'),"
                      f"__.constant('{src}')))"
                      f".property('source_backfilled_from','declared_in')"
                      f".iterate()")
            print(f"    → 已回填 {len(rs)} 条")

    print(f"\n  合计 {total} 条")
    if not args.write:
        print("  （dry-run，未写入。加 --write 执行）")
        return

    print("\n=== 回填后核对：仍无 source 的 dependency 边 ===")
    from graph_cleanup import audit_dependency_edges_without_source
    out = audit_dependency_edges_without_source(neptune_query)
    print(f"  total = {out['total']}")
    for lb, n in sorted(out['per_label'].items()):
        if n:
            print(f"    {lb}: {n}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3.11
"""把东京 petadoptions 表的数据同步到韩国。

## ⚠️ 这是权宜之计，不是正确答案

正确答案是 **DynamoDB 全局表**（托管的跨 region 复制）。但把东京那张表
转成全局表需要**两处改动东京生产表**:

  ① 开启 DynamoDB Streams（`StreamViewType: NEW_AND_OLD_IMAGES`）
     —— 实测东京那张表 `StreamSpecification` 是 null
  ② 添加韩国副本

两者都是对生产资源的变更，所以**必须先问用户**，不能自行决定。

在那之前，这个脚本做一次性复制。它的局限必须写清楚:

  - **不是持续同步**。东京改了数据，韩国不会跟着变，而且**看不出来** ——
    表里有数据、查询能返回，只是返回的是旧的。
  - 所以每次演练前应当重跑一次，或者干脆改用全局表。

## 数据量（实测 2026-09-25）

东京 26 条 / 1987 字节 —— 这是宠物目录，属于参考数据，不是用户数据。
所以一次性复制在功能上是够的。

用法:
    python3.11 scripts/sync_korea_ddb_items.py            # 只比对，不写
    python3.11 scripts/sync_korea_ddb_items.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

TOKYO = "ap-northeast-1"
KOREA = "ap-northeast-2"
# ── 表对照与键结构 ──────────────────────────────────────────────
# 键结构必须**逐表声明**，不能推断:主键字段名不同，
# 而拿错字段的表现是 KeyError 或者「比对永远认为全都缺失」。
# 三张表的键结构都来自实测 describe-table。
TABLES = {
    "petadoptions": {
        "tokyo": "ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM",
        "korea": "dr-korea-petadoptions",
        "keys": ("pettype", "petid"),
    },
    "petfood-foods": {
        "tokyo": "ServicesEks2-ddbpetfoodfoods00C5D62B-4FH25BBOAEWX",
        "korea": "dr-korea-petfood-foods",
        "keys": ("id",),
    },
    # carts 是用户购物车 —— **刻意不同步**。它是用户数据而不是参考数据，
    # 东京 0 条，而且切换后应当由用户重新加购，复制过来只会带来陈旧状态。
}


def aws(*args: str) -> str:
    r = subprocess.run(
        ["aws", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} 失败：{r.stderr.strip()}")
    return r.stdout


def scan(region: str, table: str) -> list[dict]:
    """全表扫描。⚠️ scan 是分页的 —— CLI 会自动翻页并合并。"""
    out = aws("dynamodb", "scan", "--region", region, "--table-name", table,
              "--query", "Items", "--output", "json")
    return json.loads(out)


def key_of(item: dict, keys: tuple[str, ...]) -> tuple[str, ...]:
    """主键元组。键名由 TABLES 逐表声明，不推断。"""
    return tuple(item[k]["S"] for k in keys)


def sync_one(label: str, cfg: dict, apply: bool) -> int:
    TOKYO_TABLE, KOREA_TABLE, keys = cfg["tokyo"], cfg["korea"], cfg["keys"]
    print(f"\n  ── {label} ──（键 {keys}）")
    src = scan(TOKYO, TOKYO_TABLE)
    dst = scan(KOREA, KOREA_TABLE)
    src_keys = {key_of(i, keys): i for i in src}
    dst_keys = {key_of(i, keys): i for i in dst}

    missing = sorted(src_keys.keys() - dst_keys.keys())
    extra = sorted(dst_keys.keys() - src_keys.keys())
    differing = sorted(
        k for k in src_keys.keys() & dst_keys.keys()
        if json.dumps(src_keys[k], sort_keys=True) != json.dumps(dst_keys[k], sort_keys=True)
    )

    print(f"  东京 {len(src)} 条，韩国 {len(dst)} 条")
    print(f"  韩国缺失 {len(missing)}，内容不同 {len(differing)}，韩国多出 {len(extra)}")
    for k in missing[:5]:
        print(f"      缺:{k}")
    for k in differing[:5]:
        print(f"      不同:{k}")
    for k in extra[:5]:
        print(f"      多:{k}  ← 本脚本**不会删**它")

    if not (missing or differing):
        print("  ✅ 韩国已包含东京的全部数据（内容一致）")
        return 0

    if not apply:
        print(f"\n  dry-run:会写 {len(missing) + len(differing)} 条。加 --apply 真写。")
        return 0

    ok = fail = 0
    for k in missing + differing:
        item = src_keys[k]
        try:
            aws("dynamodb", "put-item", "--region", KOREA, "--table-name", KOREA_TABLE,
                "--item", json.dumps(item))
            ok += 1
        except RuntimeError as e:
            print(f"  ❌ {k}: {e}")
            fail += 1
    print(f"\n  写入:成功 {ok}，失败 {fail}")

    # 改后重新比对 —— 这才是「写对了」的判据，不是看 put-item 的返回
    src2, dst2 = scan(TOKYO, TOKYO_TABLE), scan(KOREA, KOREA_TABLE)
    s2 = {key_of(i, keys): json.dumps(i, sort_keys=True) for i in src2}
    d2 = {key_of(i, keys): json.dumps(i, sort_keys=True) for i in dst2}
    bad = [k for k, v in s2.items() if d2.get(k) != v]
    if bad:
        print(f"  ❌ 复查仍有 {len(bad)} 条不一致:{bad[:5]}")
        return 1
    print(f"  ✅ 复查:东京 {len(s2)} 条全部在韩国存在且内容一致")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认只比对）")
    ap.add_argument("--only", help="只同步这一张（键取自 TABLES）")
    args = ap.parse_args()

    todo = TABLES if not args.only else {
        k: v for k, v in TABLES.items() if k == args.only
    }
    if not todo:
        print(f"❌ TABLES 里没有 {args.only}，可选:{sorted(TABLES)}")
        return 2

    rc = 0
    for label, cfg in todo.items():
        rc |= sync_one(label, cfg, args.apply)
    return rc


if __name__ == "__main__":
    sys.exit(main())

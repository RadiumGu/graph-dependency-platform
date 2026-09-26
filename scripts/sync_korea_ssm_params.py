#!/usr/bin/env python3.11
"""把 /petstore 参数按可移植性分档，搬到韩国。

## 分档依据是**值的形状 + 已知的资源类型**，不是名字

2026-09-25 实测东京 `/petstore` 下 41 个参数。分档:

| 档 | 数量 | 处置 | 新增计费资源 |
|---|---|---|---|
| A 集群内 DNS | 10 | 逐字复制 | 无 |
| B 字面值/开关 | 5 | 逐字复制 | 无 |
| C 已有韩国资源 | 3 | 写韩国的值 | 无 |
| D region 级但值里看不出来 | 5 | **需要韩国资源** | 有 |
| E region 绑定（值里有 ap-northeast-1） | 15 | **需要韩国资源** | 有 |
| F SecureString | 4 | 另行处理 | 无 |

**A+B+C 共 18 个可以现在就搬，不新增任何计费资源。**

## ⚠️ 一个纯按值形状分类会漏掉的陷阱

`dynamodbtablename` / `s3bucketname` / `agent/waggleai/guardrailid` /
`agent/waggleai/memoryid` / `agent/waggleai/nutritionkbid` 这五个的**值里不含
region 字样**（就是个裸名字或裸 ID），所以「按值形状」会把它们判成可移植 ——
但它们指向的资源全是 region 级的。照抄过去的表现是运行时 `ResourceNotFound`。

所以 D 档是**手工列出来的**，不靠形状推断。

## 为什么集群内 DNS 是真可移植

`search-service.petadoptions.svc.cluster.local` 这种名字由**所在集群**的
CoreDNS 解析，与 region 无关。韩国集群里同名 Service 已经建好（15-… 清单），
所以逐字复制就是对的 —— 而且这正是 petsite 报错缺的那一个
（`/petstore/searchapiurl` → ParameterNotFound → 渲染错误页）。

用法:
    python3.11 scripts/sync_korea_ssm_params.py            # 只看要做什么
    python3.11 scripts/sync_korea_ssm_params.py --apply    # 真写
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

TOKYO = "ap-northeast-1"
KOREA = "ap-northeast-2"
PREFIX = "/petstore"

# ── D 档:值里看不出 region，但资源是 region 级的 ──────────────────
# **手工列出**，因为按值形状分类看不见这一层。
REGION_SCOPED_BARE_NAMES = {
    # ⚠️⚠️ 2026-09-26: `dynamodbtablename` **从这份列表里移出去了**。
    #      原因:那张表已转成 DynamoDB 全局表，而全局表副本**必须与源表同名**
    #      （文档 V2globaltables_HowItWorks 原文:
    #        "All replicas in a global table share the same table name"）。
    #      所以它不再是「region 级」的 ——
    #      两个 region 的值**逐字相同**，应当归到 B 档逐字复制。
    #
    #      这条移动是有代价的判断，不是简化:D 档的意义是「按值形状看不出它
    #      是 region 级的」。转全局表把这个属性真正改变了 ——
    #      表名不再随 region 变化。如果哪天副本被删掉、回到两张独立的表，
    #      **必须把它挪回 D 档**，否则韩国会去读东京那张表。
    #      test_109 记录了这个条件。
    "s3bucketname": "S3 桶",
    "agent/waggleai/guardrailid": "Bedrock guardrail",
    "agent/waggleai/memoryid": "AgentCore memory",
    "agent/waggleai/nutritionkbid": "Bedrock 知识库（需向量库，成本最高的一项）",
    "searchimage": "容器镜像（petsearch-java —— 实测**两个 region 都没有**这个仓库）",
}

# ── C 档:韩国已有对应资源，写韩国的值 ─────────────────────────────
# 每一个值都是从活资源读出来的，不是猜的。
def korea_overrides() -> dict[str, str]:
    def aws(*a: str) -> str:
        r = subprocess.run(["aws", *a], capture_output=True, text=True, timeout=90)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.strip()

    cluster = "dr-korea-aurora-secondarycluster-5ctcqnmbkro4"
    writer = aws("rds", "describe-db-clusters", "--region", KOREA,
                 "--db-cluster-identifier", cluster,
                 "--query", "DBClusters[0].Endpoint", "--output", "text")
    reader = aws("rds", "describe-db-clusters", "--region", KOREA,
                 "--db-cluster-identifier", cluster,
                 "--query", "DBClusters[0].ReaderEndpoint", "--output", "text")
    return {
        "rdsendpoint": writer,
        "rds-reader-endpoint": reader,
        # ECR 仓库名跨 region 必须一致，只换 registry 主机名
        "pethistoryrepositoryuri":
            f"926093770964.dkr.ecr.{KOREA}.amazonaws.com/pet-adoptions-history",
    }


def tier_of(name: str, typ: str, value: str) -> tuple[str, str]:
    short = name[len(PREFIX) + 1:]
    if typ == "SecureString":
        return "F", "SecureString"
    if short in _OVERRIDE_KEYS:
        return "C", "韩国已有资源，写韩国值"
    if short in REGION_SCOPED_BARE_NAMES:
        return "D", f"需要韩国的 {REGION_SCOPED_BARE_NAMES[short]}"
    if "svc.cluster.local" in value:
        return "A", "集群内 DNS，与 region 无关"
    if TOKYO in value or value.startswith("arn:aws:"):
        return "E", "值指向东京资源"
    return "B", "字面值/开关"


_OVERRIDE_KEYS = {"rdsendpoint", "rds-reader-endpoint", "pethistoryrepositoryuri"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认只打印）")
    args = ap.parse_args()

    def aws(*a: str) -> str:
        r = subprocess.run(["aws", *a], capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"aws {' '.join(a[:3])}: {r.stderr.strip()}")
        return r.stdout

    params = json.loads(aws(
        "ssm", "get-parameters-by-path", "--region", TOKYO, "--path", PREFIX,
        "--recursive", "--query", "Parameters[].{n:Name,t:Type,v:Value}",
        "--output", "json",
    ))
    if len(params) != 41:
        print(f"⚠️  东京参数数量变了:{len(params)}（记档时是 41）")
        print("   —— 分档依据可能过期，先重新清点再继续，不要盲目写")

    overrides = korea_overrides()
    buckets: dict[str, list[tuple[str, str, str]]] = {}
    for p in sorted(params, key=lambda x: x["n"]):
        short = p["n"][len(PREFIX) + 1:]
        tier, why = tier_of(p["n"], p["t"], p.get("v") or "")
        val = overrides.get(short, p.get("v") or "")
        buckets.setdefault(tier, []).append((short, val, why))

    writable = buckets.get("A", []) + buckets.get("B", []) + buckets.get("C", [])
    blocked = buckets.get("D", []) + buckets.get("E", [])

    for tier, label in (
        ("A", "集群内 DNS —— 逐字复制"),
        ("B", "字面值/开关 —— 逐字复制"),
        ("C", "韩国已有资源 —— 写韩国值"),
    ):
        items = buckets.get(tier, [])
        if not items:
            continue
        print(f"\n  【{tier}】{label}（{len(items)} 个）")
        for short, val, _ in items:
            print(f"      {short:<34} = {val[:64]}")

    print(f"\n  【D+E】需要韩国自己的资源，**本脚本不处理**（{len(blocked)} 个）")
    for short, _, why in blocked:
        print(f"      {short:<34} {why}")

    f = buckets.get("F", [])
    print(f"\n  【F】SecureString（{len(f)} 个）—— 本脚本**不读也不写**它们的值")
    for short, _, _ in f:
        print(f"      {short}")

    if not args.apply:
        print(f"\n  dry-run:会写 {len(writable)} 个。加 --apply 真写。")
        return 0

    ok = fail = 0
    for short, val, _ in writable:
        name = f"{PREFIX}/{short}"
        try:
            aws("ssm", "put-parameter", "--region", KOREA, "--name", name,
                "--type", "String", "--value", val, "--overwrite")
            ok += 1
        except RuntimeError as e:
            print(f"  ❌ {short}: {e}")
            fail += 1
    print(f"\n  写入完成:成功 {ok}，失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

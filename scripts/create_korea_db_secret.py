#!/usr/bin/env python3.11
"""给韩国从集群建一份 Secrets Manager 密钥（结构照东京，host 换成韩国）。

## 为什么不用 CloudFormation

密钥里的**密码必须与东京一致** —— 韩国从集群是 Aurora 全局数据库的复制集，
共用主密码。而把密码写进 CFN 模板就等于把它提交进仓库。

所以这一步只能用脚本，而且**全程不打印密码**:读出来、换掉 host、写进去，
中间不经过任何 print、不落任何临时文件。

## pethistory 为什么直接依赖它

实测 pethistory 的环境变量写死了:

    AWS_REGION      ap-northeast-1
    RDS_SECRET_ARN  arn:aws:secretsmanager:ap-northeast-1:…:secret:DatabaseSecret…

从韩国 VPC 实测:东京 Secrets Manager **可达**（取密钥这步能过），
但东京 Aurora 的 5432 **连不上**（无 VPC 对等）。所以它取到密钥后卡在连库上 ——
表现是「容器 started、零日志、从不监听 8080」。

修法不是让韩国连上东京（真灾难时东京就是没了），而是给它韩国自己的密钥。

## 字段结构（实测东京那份）

    dbClusterIdentifier / dbname / engine / host / password / port / username

其中 host 与 dbClusterIdentifier 要换成韩国的，其余照抄。

用法:
    python3.11 scripts/create_korea_db_secret.py           # 只看会做什么
    python3.11 scripts/create_korea_db_secret.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

TOKYO = "ap-northeast-1"
KOREA = "ap-northeast-2"
TOKYO_SECRET = (
    "arn:aws:secretsmanager:ap-northeast-1:926093770964:"
    "secret:DatabaseSecret3B817195-VNRjDLU0sXke-PZeYnO"
)
KOREA_CLUSTER = "dr-korea-aurora-secondarycluster-5ctcqnmbkro4"
KOREA_SECRET_NAME = "dr-korea/petadoptions/database"

# 这些字段必须换成韩国的值；其余照抄东京
REGION_BOUND_FIELDS = ("host", "dbClusterIdentifier")

# 这些字段是密钥材料，任何情况下不许打印
SENSITIVE = ("password",)


def aws(*args: str) -> str:
    """跑 aws CLI。**不抑制 stderr** —— 错误文本往往就是答案。"""
    r = subprocess.run(
        ["aws", *args], capture_output=True, text=True, timeout=90, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def redact(d: dict) -> dict:
    """打印用的脱敏副本。"""
    return {
        k: (f"<已隐去，长度 {len(str(v))}>" if k in SENSITIVE else v)
        for k, v in sorted(d.items())
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认只打印脱敏预览）")
    args = ap.parse_args()

    src = json.loads(
        aws("secretsmanager", "get-secret-value", "--region", TOKYO,
            "--secret-id", TOKYO_SECRET, "--query", "SecretString", "--output", "text")
    )

    missing = [f for f in ("host", "password", "port", "username", "dbname") if f not in src]
    if missing:
        print(f"❌ 东京密钥里缺字段 {missing} —— 结构变了，先重新确认再写")
        return 2

    korea_host = aws(
        "rds", "describe-db-clusters", "--region", KOREA,
        "--db-cluster-identifier", KOREA_CLUSTER,
        "--query", "DBClusters[0].Endpoint", "--output", "text",
    )

    dst = dict(src)
    dst["host"] = korea_host
    dst["dbClusterIdentifier"] = KOREA_CLUSTER

    print("  东京密钥（脱敏）:")
    for k, v in redact(src).items():
        print(f"      {k:<22} {v}")
    print("\n  要写进韩国的（脱敏）:")
    for k, v in redact(dst).items():
        mark = "  ← 换了" if k in REGION_BOUND_FIELDS else ""
        print(f"      {k:<22} {v}{mark}")

    # 核对：除了 region 绑定的字段，其余必须逐字相同
    for k in src:
        if k in REGION_BOUND_FIELDS:
            continue
        if src[k] != dst[k]:
            print(f"❌ 字段 {k} 被意外改动了 —— 停止")
            return 2
    print(f"\n  ✅ 除 {list(REGION_BOUND_FIELDS)} 外的字段逐字相同")

    if not args.apply:
        print("\n  dry-run。加 --apply 真写。")
        return 0

    payload = json.dumps(dst)
    try:
        arn = aws("secretsmanager", "create-secret", "--region", KOREA,
                  "--name", KOREA_SECRET_NAME,
                  "--description", "Korea DR copy of petadoptions DB credentials",
                  "--secret-string", payload,
                  "--query", "ARN", "--output", "text")
        print(f"  已创建:{arn}")
    except RuntimeError as e:
        if "ResourceExistsException" not in str(e):
            raise
        aws("secretsmanager", "put-secret-value", "--region", KOREA,
            "--secret-id", KOREA_SECRET_NAME, "--secret-string", payload)
        arn = aws("secretsmanager", "describe-secret", "--region", KOREA,
                  "--secret-id", KOREA_SECRET_NAME, "--query", "ARN", "--output", "text")
        print(f"  已更新既有密钥:{arn}")

    # ── 写进 SSM，让应用能找到它 ──────────────────────────────────
    aws("ssm", "put-parameter", "--region", KOREA,
        "--name", "/petstore/rdssecretarn", "--type", "String",
        "--value", arn, "--overwrite")
    print("  已写 /petstore/rdssecretarn")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
resolve_targets.py — 实验目标解析 CLI

手动解析所有实验的目标资源，写入审计 JSON 文件：
  targets-fis.json       — FIS 实验目标（ARN）
  targets-chaosmesh.json — Chaos Mesh 实验目标（Pod 列表）

用法：
  python3 resolve_targets.py                     # 解析所有实验目标
  python3 resolve_targets.py --backend fis       # 只解析 FIS
  python3 resolve_targets.py --backend chaosmesh # 只解析 Chaos Mesh
  python3 resolve_targets.py --refresh           # 强制刷新（清除缓存后重新解析）
  python3 resolve_targets.py --dir <path>        # 指定实验目录（默认 ./experiments）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# 确保 runner 包可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _arn_short(arn: str | None) -> str:
    """将 ARN 缩短为可读形式，方便终端展示"""
    if not arn:
        return "(未解析)"
    # arn:aws:lambda:region:account:function:name → function:name
    parts = arn.split(":")
    if len(parts) >= 6:
        return ":".join(parts[5:]) or arn
    return arn


def print_fis_results(fis: dict, cache_file: str):
    print("\n=== FIS Targets ===")
    if not fis:
        print("  (无 FIS 实验)")
    else:
        for key, info in sorted(fis.items()):
            arn    = info.get("arn")
            src    = info.get("resolved_from", "?")
            exp    = info.get("experiment", "")
            status = "OK" if arn else "FAIL"
            print(f"  [{status}] {key}")
            print(f"        → {_arn_short(arn)}  ({src})")
            if exp:
                print(f"        实验: {exp}")
    print(f"Written to {cache_file}")


def print_cm_results(cm: dict, cache_file: str):
    print("\n=== Chaos Mesh Targets ===")
    if not cm:
        print("  (无 Chaos Mesh 实验)")
    else:
        for key, info in sorted(cm.items()):
            pods    = info.get("pods", [])
            rep     = info.get("replicas", 0)
            exps    = info.get("experiments", [])
            running = [p for p in pods if p.get("status") == "Running"]
            names   = ", ".join(p["name"] for p in pods[:3])
            if len(pods) > 3:
                names += f", ...({len(pods) - 3} more)"
            status = "OK" if pods else "FAIL"
            print(f"  [{status}] {key}")
            print(f"        pods={len(pods)} running={len(running)} replicas={rep}")
            if names:
                print(f"        pod names: {names}")
            if exps:
                print(f"        实验: {', '.join(exps)}")
    print(f"Written to {cache_file}")


def main():
    parser = argparse.ArgumentParser(
        description="解析所有混沌实验目标，输出 targets-fis.json / targets-chaosmesh.json"
    )
    parser.add_argument(
        "--backend", choices=["fis", "chaosmesh"],
        help="只解析指定后端（不指定则解析所有）",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="强制刷新：清除本地缓存后重新解析",
    )
    parser.add_argument(
        "--dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiments"),
        help="实验 YAML 目录（默认 ./experiments）",
    )
    args = parser.parse_args()

    from runner.target_resolver import TargetResolver, FIS_CACHE_FILE, CM_CACHE_FILE

    resolver = TargetResolver()

    if args.refresh:
        print("刷新缓存...")
        resolver.refresh()

    print(f"扫描实验目录: {args.dir}")
    results = resolver.resolve_all_experiments(args.dir)

    fis = results.get("fis", {})
    cm  = results.get("chaosmesh", {})

    if args.backend == "fis":
        print_fis_results(fis, FIS_CACHE_FILE)
    elif args.backend == "chaosmesh":
        print_cm_results(cm, CM_CACHE_FILE)
    else:
        print_fis_results(fis, FIS_CACHE_FILE)
        print_cm_results(cm, CM_CACHE_FILE)

    # 汇总
    fis_ok  = sum(1 for v in fis.values() if v.get("arn"))
    fis_all = len(fis)
    cm_ok   = sum(1 for v in cm.values() if v.get("pods"))
    cm_all  = len(cm)
    print(f"\n汇总: FIS {fis_ok}/{fis_all} ARN 已解析，Chaos Mesh {cm_ok}/{cm_all} 服务有 Pod")


if __name__ == "__main__":
    main()

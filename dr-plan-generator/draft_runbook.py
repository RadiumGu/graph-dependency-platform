"""draft_runbook.py — 在**操作者机器**上生成东京 → 首尔切换计划的初稿正文。

    python3 dr-plan-generator/draft_runbook.py --out /tmp/body.md

产出的正文交给 `probe_cli.py start-plan --body-file` 起一条 DrPlanWorkflow，
然后人审核、修改（revise_plan），填掉所有 `needs_human_input`，再批准。

## 为什么在操作者机器上跑，而不是 worker 上

镜像的**期望** digest 取自仓库里要应用的清单，不取自首尔 ECR —— 取自首尔 ECR
就是「用首尔核实首尔」的循环论证，永远 PASS。清单只在仓库里有权威定义。

## 读事实失败时

每一类事实读不到都写进 notes，**不静默跳过**：一个少了 DynamoDB 核查的计划
看起来和一个完整的计划长得一样。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent / "worker"))

from runbook import extract_runbook  # noqa: E402
from runbook_draft import DraftFacts, draft_tokyo_to_seoul, render_body  # noqa: E402

MANIFESTS = [
    "infra/dr-korea/petsite-korea-drill.yaml",
    "infra/dr-korea/15-korea-workloads.yaml",
]
_IMG = re.compile(
    r"image:\s*\d{12}\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repo>[a-z0-9._/-]+?)(?:@(?P<digest>sha256:[0-9a-f]{64})|:(?P<tag>[A-Za-z0-9._-]+))\s*$",
    re.MULTILINE,
)


def images_from_manifests(target_region: str) -> tuple[list, list, list[str]]:
    by_digest: set[tuple[str, str]] = set()
    by_tag: set[tuple[str, str]] = set()
    notes: list[str] = []
    for rel in MANIFESTS:
        p = ROOT / rel
        if not p.exists():
            notes.append(f"清单 {rel} 不存在 —— 它引用的镜像没有被核查")
            continue
        for m in _IMG.finditer(p.read_text(encoding="utf-8")):
            if m["region"] != target_region:
                notes.append(f"{rel} 引用了 {m['region']} 的镜像 {m['repo']}（不在目标区）")
                continue
            if m["digest"]:
                by_digest.add((m["repo"], m["digest"]))
            else:
                by_tag.add((m["repo"], m["tag"]))
                if m["tag"] == "latest":
                    notes.append(
                        f"{rel} 用 `{m['repo']}:latest` —— 可变标签，核查只能证明存在、"
                        "不能证明是哪一版。建议钉 digest"
                    )
    return sorted(by_digest), sorted(by_tag), notes


def live_facts(f: DraftFacts) -> None:
    import boto3  # noqa: PLC0415

    def note(what: str, e: Exception) -> None:
        f.notes.append(f"读{what}失败：{type(e).__name__}: {e} —— 相关核查未写入计划")

    try:
        gc = boto3.client("rds", region_name=f.target_region).describe_global_clusters(
            GlobalClusterIdentifier=f.global_cluster
        )["GlobalClusters"][0]
        for m in gc.get("GlobalClusterMembers") or []:
            if m["DBClusterArn"].split(":")[3] == f.target_region:
                f.secondary_cluster_arn = m["DBClusterArn"]
    except Exception as e:  # noqa: BLE001
        note("Aurora 全局集群", e)
    try:
        tables = boto3.client("dynamodb", region_name=f.target_region).list_tables()["TableNames"]
        f.dynamodb_table = next((t for t in tables if "ddbpetadoption" in t), "")
        if not f.dynamodb_table:
            f.notes.append("首尔没有找到宠物表（名字含 ddbpetadoption）—— DynamoDB 核查未写入")
    except Exception as e:  # noqa: BLE001
        note("DynamoDB 表", e)
    try:
        fns = boto3.client("lambda", region_name=f.target_region).list_functions()["Functions"]
        f.lambdas = sorted(x["FunctionName"] for x in fns if x["FunctionName"].startswith("dr-korea-"))
    except Exception as e:  # noqa: BLE001
        note("Lambda", e)
    try:
        elb = boto3.client("elbv2", region_name=f.target_region)
        f.target_groups = sorted(
            t["TargetGroupName"] for t in elb.describe_target_groups()["TargetGroups"]
            if t["TargetGroupName"].startswith("dr-korea-")
        )
        lb = elb.describe_load_balancers(Names=["dr-korea-petsite-alb"])["LoadBalancers"][0]
        listeners = elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]
        if any(x["Port"] == 80 for x in listeners):
            f.health_urls = [f"http://{lb['DNSName']}/health/status"]
    except Exception as e:  # noqa: BLE001
        note("ALB / 目标组", e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true", help="不读 AWS，只用清单（测试用）")
    a = ap.parse_args()

    f = DraftFacts(workload_manifests=list(MANIFESTS))
    f.images_by_digest, f.images_by_tag, notes = images_from_manifests(f.target_region)
    f.notes.extend(notes)
    if not a.offline:
        live_facts(f)

    rb = draft_tokyo_to_seoul(f)
    body = render_body(
        f, rb,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        generator="dr-plan-generator/draft_runbook.py",
    )
    # 自检：产出的正文必须能被 validator 接受（只允许带 needs_human_input）。
    assert extract_runbook(body) is not None
    Path(a.out).write_text(body, encoding="utf-8")
    print(f"已写 {a.out}：{len(rb.steps)} 步，待人填写 {rb.unresolved()}，发现 {len(f.notes)} 条")
    for n in f.notes:
        print("  ·", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

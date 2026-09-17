#!/usr/bin/env python3
"""把**源码证明不可能发生**的依赖边标为建模产物，并附 file:line 证据。

    python3 scripts/classify_modeling_artifacts.py --list
    python3 scripts/classify_modeling_artifacts.py --apply

## 为什么这件事必须带证据、且必须可审计

剔除分母会**抬高被考核的覆盖率**。一个能自己调整分母的指标不是指标。
所以本脚本刻意做成三条硬约束：

1. **声明式白名单**：只有下表里的边会被改，且每条必须带 `evidence`
   （file:line + 原文）与 `searched`（搜过哪些模式）。没有证据的边写不进表 ——
   `_validate()` 会在 import 期就拒绝。
2. **绝不删边**：写 `verify_status='modeling_artifact'`，边仍在图里、仍在总数里。
   报告分两个口径呈现（见 §6/§7），读者能同时看到 47 的分母和可评估分母。
3. **`searched` 必填**：因为本项目在「搜了不匹配的模式然后相信空结果」上
   已经栽过十次。最近一次差点让我删掉一条**真实**依赖：
   `payforadoption -> DynamoDB` 被我记成「Go 源码无任何 dynamodb. 调用」，
   而真相是它经 `guregu/dynamo` 的 `db.Table(...)` 调用
   （repository.go:525-532）—— 搜 `dynamodb.` 自然搜不到。
   把搜索词清单落到产物上，下一个人才能判断这个「无」有多可信。

## 三种「不是普通承重依赖」要分开，不能一律叫产物

    modeling_artifact   源码证明调用**不可能发生** → 出可评估分母
    platform_pull       真实但不在请求路径（ECR 拉镜像由 kubelet/执行角色发起）
                        → **留在分母**，只是验证手段不同
    designed_to_fail    真实调用但被设计成失败（petsearch 的 createBucket 挂在
                        `Math.random()*9999 < 100` 的 ~1% 门后，对已拥有的桶
                        必抛 BucketAlreadyOwnedByYouException）→ **留在分母**

把后两类也塞进 modeling_artifact 就是用分类掩盖问题：ECR 边是真的
（Pod 扩容/重启拉不到镜像就起不来），S3 边也是真的（只是稳态必失败）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "chaos", "code"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "infra", "lambda", "shared", "python"))
# 仓库根 —— compliance_export 是顶层包，从 scripts/ 直接跑时不在路径上
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

#: 角色快照 —— 具名 RDS 实例节点的 reader/writer 字样**不可信**。
#:
#: 2026-09-15 实测：`...databasewriter2462cc03...` 的 `IsClusterWriter=False`，
#: 而名字里写着 reader 的 `...databasereader1f54479b8...` 才是真 writer。
#: 某次故障转移换了角色、节点名没跟着换。
#:
#: 因此「某服务不连读实例」这类判定**只在特定角色快照下成立**，
#: 必须把快照连同判定一起落盘，否则下次故障转移后这条结论会静默变错。
_ROLE_SNAPSHOT_NOTE = (
    "角色快照 2026-09-15：databasereader1f54479b8=writer, "
    "databasewriter2462cc03=reader（名字与角色相反，勿按名字推断）"
)

#: (service, edge_type, target) -> 证据
#:
#: 只收**源码证明调用不可能发生**的边。有疑问的一律不进表 ——
#: 少标一条只是覆盖率数字低一点，错标一条是在合规产物上造假。
ARTIFACTS: dict[tuple[str, str, str], dict] = {
    ("petsite", "AccessesData", "serviceseks2-databaseb269d8bb-efjeyzicx2ak"): {
        "why": "petsite 无任何数据库客户端，不可能直连 RDS",
        "evidence": (
            "PetSite.csproj 无 Npgsql、无 EF 运行时 provider"
            "（仅 Microsoft.EntityFrameworkCore.Tools 9.0.10 且 PrivateAssets=all，"
            "是设计期脚手架、不入运行时）"),
        "searched": ("npgsql", "postgres", "SqlConnection", "DbContext",
                     "UseNpgsql", "connectionstring", ":5432", "entityframework"),
        "note": "此前被标为 confirmed —— 那是一条假 confirmed，本次修正",
    },
    ("petsite", "AccessesData", "ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM"): {
        "why": "petsite 无 DynamoDB 客户端；宠物数据经 petsearch/payforadoption 的 HTTP 接口取",
        "evidence": "PetSite.csproj 无 AWSSDK.DynamoDBv2；全树 grep 零命中",
        "searched": ("dynamo", "IAmazonDynamoDB", "AWSSDK.DynamoDBv2"),
        "note": "IRSA 角色的两条内联策略与四个托管策略也都无 DynamoDB 权限 —— 既无代码也无权限",
    },
    ("payforadoption", "AccessesData", "StepFnStateMachine76D362E8-3jkn8j2OUpdQ"): {
        "why": "payforadoption 不调 Step Functions",
        "evidence": "go.mod 中无 sfn/stepfunctions 依赖（连 indirect 都没有）",
        "searched": ("stepfunctions", "sfn", "service/sfn",
                     "StartExecution", "StateMachine"),
        "note": "状态机的真实调用方是 petsite（PaymentController.cs:211，仅 pettype==bunny 时）",
    },
    ("petsearch", "AccessesData", "sts"): {
        "why": "petsearch 源码无显式 STS 调用",
        "evidence": (
            "WebConfig.java 只构建 S3/S3Presigner/DynamoDb/Ssm 四个 bean"
            "（:46/:53/:60/:67），无 STS bean；唯一 grep 命中是 "
            "requeStStartTime / statusCode 的子串假阳性"),
        "searched": ("sts", "securitytoken", "getcalleridentity",
                     "assumerole", "stsclient"),
        "note": (
            "build.gradle 确实把 sts SDK 放进 classpath，运行在 EKS 上时默认凭证链"
            "（IRSA / AssumeRoleWithWebIdentity）会隐式用到 —— 那是**凭证解析层**，"
            "不是应用级依赖。这条边的语义若要保留，应改建成承载层而非应用调用。"),
    },
}

#: 真实但不在请求路径 —— **留在可评估分母**，只是验证手段不同。
PLATFORM_PULL: dict[tuple[str, str, str], dict] = {
    (svc, "DependsOn", "cdk-hnb659fds-container-assets-926093770964-ap-northeast-1"): {
        "why": "ECR 拉镜像由 ECS 执行角色 / kubelet 发起，不是应用代码调用",
        "evidence": (
            "cdk/pet_stack/lib/services/ecs-service.ts:34-41 ExecutionRolePolicy 含 "
            "ecr:GetAuthorizationToken/BatchCheckLayerAvailability/"
            "GetDownloadUrlForLayer/BatchGetImage"),
        "searched": ("ecr:BatchGetImage", "ExecutionRolePolicy",
                     "ContainerImageBuilder", "ECRDeployment", "containerUri"),
        "note": (
            "这条边是**真的**：Pod 扩容或重启时拉不到镜像就起不来。"
            "但它不在请求路径上，切断它不会让稳态业务退化 —— "
            "所以既不能当产物剔除，也不能用请求路径的手段去验。"),
    }
    for svc in ("petsite", "payforadoption", "petlistadoptions", "petsearch")
}

#: 真实调用但被设计成失败 —— **留在可评估分母**。
DESIGNED_TO_FAIL: dict[tuple[str, str, str], dict] = {
    ("petsearch", "AccessesData", "s3"): {
        "why": "唯一会走线的 S3 API 是 CreateBucket，且被设计成必然失败",
        "evidence": (
            "SearchController.java:76 `if (Math.random()*9999 < 100)` 约 1% 概率门；"
            ":80 createBucket 对账户已拥有的桶必抛 BucketAlreadyOwnedByYouException，"
            "被 :88 catch 吞掉返回空串；:84 presignGetObject 是纯本地 SigV4 签名，"
            "不发任何网络请求"),
        "searched": ("createBucket", "presignGetObject", "S3Client", "s3Presigner"),
        "note": (
            "稳态下本服务**没有成功的 S3 网络调用** —— 这解释了为什么闸门总是"
            "0% 成功率：不是观测缺口，是设计如此。把它当稳态数据依赖边是失真的，"
            "它是一条概率性、设计为失败的边。修好这个 bug 的结果是**删掉这条边**。"),
    },
}


#: 真实依赖，但只在**引导/管理路径**上被调用 —— 留在分母，且**永远拿不到
#: confirmed**：没有任何用户可见功能依赖它，切断它不会产生业务退化。
#:
#: 这一类必须单列，并进上面任何一类都是错的：
#:   不是 modeling_artifact —— 调用真实存在，端点运行时可达
#:   不是 platform_pull     —— 由应用代码发起，不是平台行为
#:   不是 designed_to_fail  —— 调用会成功，只是不在业务路径上
BOOTSTRAP_ONLY: dict[tuple[str, str, str], dict] = {
    ("payforadoption", "AccessesData", "dynamodb"): {
        "why": "DynamoDB 调用只出现在引导端点 POST /api/triggerseeding，不在业务请求路径上",
        "evidence": (
            "payforadoption/repository.go:499 TriggerSeeding 是唯一的 DynamoDB 调用点"
            "（:528 dynamo.New、:529 db.Table、:531-534 Batch().Write() 批量 Put 宠物目录）；"
            "路由挂在 payforadoption/transport.go:69 POST /api/triggerseeding；"
            "业务端点 /api/completeadoption（transport.go:54）无任何 DynamoDB 调用路径"),
        "searched": ("dynamodb", "dynamo.", "guregu", "db.Table", "DynamoDBTable",
                     "Batch()", "BatchWrite", "PutItem"),
        "note": (
            "此前被标为 confirmed 且无切断手段记录，**那条 confirmed 不成立**。"
            "近 60 分钟活跃负载下 X-Ray 里 payforadoption 没有 DynamoDB 出边"
            "（出边为 HTTP GET 51、API Gateway 102、SSM 51、SQS 51、postgres 150、"
            "PetSearch 52）。这不是观测缺口 —— main.go:141 有 "
            "otelaws.AppendMiddlewares，AWS SDK 客户端确实被插桩，"
            "dynamo.New(awsCfg) 继承该中间件，播种若跑过就会出现节点。"
            "刻意未做切断实验：结果可预知（注入生效但无业务退化 = inconclusive），"
            "而代价是对线上打一个会批量重写宠物目录的管理端点，不成比例。"),
    },
}


def _validate() -> None:
    """import 期就拒绝没有证据的条目 —— 证据不是可选项。"""
    for name, table in (("ARTIFACTS", ARTIFACTS),
                        ("PLATFORM_PULL", PLATFORM_PULL),
                        ("DESIGNED_TO_FAIL", DESIGNED_TO_FAIL),
                        ("BOOTSTRAP_ONLY", BOOTSTRAP_ONLY)):
        for key, v in table.items():
            if len(key) != 3 or not all(key):
                raise ValueError("%s 的键必须是 (service, edge_type, target)：%r"
                                 % (name, key))
            for field in ("why", "evidence", "searched"):
                if not v.get(field):
                    raise ValueError(
                        "%s[%r] 缺 %s —— 没有证据的分类等于伪造来源"
                        % (name, key, field))
            if not isinstance(v["searched"], (tuple, list)) or not v["searched"]:
                raise ValueError(
                    "%s[%r] 的 searched 必须是非空的搜索词清单 —— "
                    "本项目在「搜了不匹配的模式然后相信空结果」上栽过十次，"
                    "这一列是让下一个人判断这个「无」有多可信的唯一依据" % (name, key))


_validate()


def _rows():
    from compliance_export.queries import fetch_function_mapping, _dep_labels
    from neptune import neptune_client as nc
    return fetch_function_mapping(nc, _dep_labels())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="真的写回图谱；缺省只列出将要做什么")
    a = ap.parse_args()

    rows = {(r.get("service"), r.get("edge_type"), r.get("target")): r
            for r in _rows()}
    print("图谱中一跳依赖边：%d 条\n" % len(rows))

    plans = []
    # ARTIFACTS 出可评估分母；BOOTSTRAP_ONLY 留在分母但永远拿不到 confirmed。
    # 两者都要写回状态 —— 只在报告里区分是不够的，图谱本身必须能查出差别，
    # 否则下一个查询者只能看到一个没有依据的 confirmed。
    for status, table in (("modeling_artifact", ARTIFACTS),
                          ("bootstrap_only", BOOTSTRAP_ONLY)):
        for key, v in table.items():
            r = rows.get(key)
            if r is None:
                # 硬失败而不是跳过。**「表里有、图里没有」几乎总是名字写错**，
                # 而静默跳过的后果是「我以为标了、其实一条没标」——
                # 第一版就因为拿被截断到 42 字符的终端显示值当数据用，
                # 漏掉了两条边且只打了一行警告。
                cand = [k[2] for k in rows if k[2].startswith(key[2][:24])]
                raise SystemExit(
                    "图谱里没有这条边：%s\n"
                    "  前缀相近的真实目标名：%s\n"
                    "  （若是从终端输出复制的名字，检查是否被列宽截断）"
                    % (key, cand or "无"))
            plans.append((key, v, r.get("verify_status"), status))

    for status in ("modeling_artifact", "bootstrap_only"):
        sub = [p for p in plans if p[3] == status]
        if not sub:
            continue
        scope = ("出可评估分母" if status == "modeling_artifact"
                 else "留在分母，但永远拿不到 confirmed")
        print("── 将标为 %s（%s）──" % (status, scope))
        for key, v, cur, _ in sub:
            flag = "  ⚠️ 修正假 confirmed" if cur == "confirmed" else ""
            print("  %-16s -%-13s-> %-42s  现状=%s%s"
                  % (key[0][:16], key[1][:13], key[2][:42], cur or "-", flag))
            print("      理由: %s" % v["why"])
            print("      证据: %s" % v["evidence"])
            print("      搜过: %s" % ", ".join(v["searched"]))
            if v.get("note"):
                print("      附注: %s" % v["note"])
        print()
    print("── 留在分母、仅标注语义（不写回状态）──")
    for name, table in (("platform_pull", PLATFORM_PULL),
                        ("designed_to_fail", DESIGNED_TO_FAIL)):
        for key, v in table.items():
            if key in rows:
                print("  [%s] %-16s -> %-42s" % (name, key[0][:16], key[2][:42]))

    if not a.apply:
        print("\n（--list 模式，未写回。加 --apply 生效）")
        return 0

    from runner.edge_verification import write_verdict
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_vd", os.path.join(os.path.dirname(__file__), "verify_via_iam_deny.py"))
    vd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vd)

    n = 0
    for key, v, _cur, status in plans:
        reason = "%s｜证据：%s｜搜过：%s" % (
            v["why"], v["evidence"], ", ".join(v["searched"]))
        if v.get("note"):
            reason += "｜附注：%s" % v["note"]
        # 边 id 必须现查 —— 边标签由图谱给，不由调用方猜
        # （调用方手上的 edge_type 曾与节点类型撞过，导致写回静默失败）
        eids, how = vd._edge_ids(key[0], key[2])
        if not eids:
            raise SystemExit("定位不到边 id：%s（%s）" % (key, how))
        for e in eids:
            ok = write_verdict({
                "edge_id": e["eid"],
                "status": status,
                # 源码审计是**确定性**证据：不是采样、不是统计推断，
                # 而是「这个调用在代码里不存在」。所以给满置信度。
                "confidence": 1.0,
                "verified_at": int(time.time()),
                "experiment_id": "source-audit-20260915",
                "reason": reason[:300],
                "confirm_count": 0,
                "refute_count": 0,
                "severance": "source-audit",
                "evidence_channel": "source-code+iac",
                # 源码审计不做故障注入 —— 生效性「不适用」而非「未知」
                "injection_confirmed": None,
                "dependency_class": status,
                "dependency_class_reason": v["why"][:300],
                "observing_sources": 0,
            })
            print("  %s %s  (edge %s)" % ("✓" if ok else "✗", key, e["eid"][:12]))
            n += 1 if ok else 0
    print("\n已写回 %d 条边。%s" % (n, _ROLE_SNAPSHOT_NOTE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

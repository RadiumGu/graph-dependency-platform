#!/usr/bin/env python3
"""用 IAM deny 切断依赖来验证它 —— 托管服务类依赖的可行手段。

    python3 scripts/verify_via_iam_deny.py --list
    python3 scripts/verify_via_iam_deny.py --edge petsearch:DynamoDBTable:<表名>
    python3 scripts/verify_via_iam_deny.py --edge ... --apply

## 为什么换到 IAM 层：前一轮所有失败都源于切断层选错了

2026-09-09 执行侧实测（见 `todo/task-tier0-fault-injection_20260909.md` 回执），
在**网络层**切断 AWS 托管服务，三条路全部失败：

    NetworkChaos + externalTargets  打不断 —— 区域端点多 IP 轮换；
                                    S3 是 Gateway 端点，封 IP 根本不在路径上
    DNSChaos 单独用                 打不断 —— 长连接 + DNS 缓存，窗口内不重查
    FIS aws:fis:inject-api-internal-error
                                    AWS 直接拒绝 —— 该动作的 service 参数
                                    只支持 `ec2` 与 `kinesis`

**IAM deny 绕开全部四个障碍，只靠一条性质**：SigV4 授权是**每次 API 调用**
评估的，不是每个连接评估的。所以 deny 在下一个请求上立即生效 —— 即使该请求
跑在一条已经建立的 keep-alive 连接上。DNS 缓存、连接池、端点 IP 轮换、
Gateway 与 Interface 端点的差异，在 IAM 层全部不成立。

连带收益：**不需要删 Pod**，所以回执里那个「删 Pod 让观测信号混淆」的问题
直接消失 —— Pod 全程在跑，观测到的退化就是纯粹的依赖退化。

## 这个手段证明什么、不证明什么

**证明**：这条依赖是承重的（切断后消费方业务退化）。这正是 DORA Art. 8(4)
与 SYSC 15A.4.1R 的映射诉求 —— 「哪些依赖是关键的」。

**不证明**：延迟劣化、部分失败、超时重试等场景下的行为。`AccessDenied` 与
`不可用` 的失败模式不同（403 立即返回 vs 超时挂住）。所以判定里必须记录
切断手段，报告不能让读者以为做过了完整的韧性场景测试。

## 半径：恰好一个服务

deny 加在该服务自己的 **IRSA 角色**上（每个业务服务都有独立角色，
2026-09-13 实测 7 个）。别的服务访问同一张表不受影响 —— 这是网络层做不到的
精度（切 Pod 网络会连带切掉它对所有目标的访问）。

## 回滚

删除内联策略。放在 `finally` 里，**任何异常路径都会执行**。
若删除本身失败，脚本会把手工回滚命令打在最后一行 —— 一个加了 deny 没removed
的服务会一直 403，那是真实故障，不能只写进日志。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (_ROOT, _ROOT / "rca", _ROOT / "chaos" / "code",
          _ROOT / "infra" / "lambda" / "shared" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

REGION = os.environ.get("REGION", "ap-northeast-1")

#: 声明式方法表：目标节点类型 → 怎么构造 deny 语句 + 怎么在 X-Ray 里找到这条边。
#:
#: **必须声明式**（不许在流程里内联 if/else）—— 这样「哪类目标用什么手段」
#: 是可审计的一张表，而不是散在代码里的分支。由 test_73 校验。
#:
#: `actions` 用通配是刻意的：我们要证明的是「这条依赖承重」，不是「某个具体
#: API 调用失败会怎样」。收窄到单个 action 会让结论依赖于实现细节
#: （应用今天用 Query 明天换 Scan，结论就不成立了）。
#:
#: `xray_types` 是 X-Ray 服务图里目标节点的 **Type 前缀**。
#: 2026-09-13 实测：图谱用资源名（队列名 `ServicesEks2-sqspetadoption...`），
#: 而 X-Ray 的节点叫 `SQS`（Type=`AWS::SQS`）或队列 URL —— **按名字永远匹配不上**，
#: `collect_edge_flow` 会返回 ok=False，基线闸门于是拒绝开跑。
#: 空元组表示按名字就能匹配上（DynamoDB 表、S3 桶实测可以）。
SEVERANCE_METHODS: dict[str, dict] = {
    "DynamoDBTable": {
        "actions": ["dynamodb:*"],
        "arn": "arn:aws:dynamodb:{region}:{acct}:table/{name}",
        "also": ["arn:aws:dynamodb:{region}:{acct}:table/{name}/index/*"],
        "xray_types": (),
    },
    "S3Bucket": {
        "actions": ["s3:*"],
        "arn": "arn:aws:s3:::{name}",
        "also": ["arn:aws:s3:::{name}/*"],
        "xray_types": (),
    },
    "SQSQueue": {
        "actions": ["sqs:*"],
        "arn": "arn:aws:sqs:{region}:{acct}:{name}",
        "also": [],
        "xray_types": ("AWS::SQS",),
    },
    "SNSTopic": {
        "actions": ["sns:*"],
        "arn": "arn:aws:sns:{region}:{acct}:{name}",
        "also": [],
        "xray_types": ("AWS::SNS",),
    },
    "StepFunction": {
        "actions": ["states:*"],
        "arn": "arn:aws:states:{region}:{acct}:stateMachine:{name}",
        "also": ["arn:aws:states:{region}:{acct}:execution:{name}:*"],
        "xray_types": ("AWS::StepFunctions", "AWS::States"),
    },
    "AWSServiceEndpoint": {
        # 图谱里的名字就是服务名（sns / sts / ssm / stepfunctions / dynamodb），
        # deny 收窄到该服务的全部 action。
        "actions": ["{name}:*"],
        "arn": "*",
        "also": [],
        "xray_types": (),
    },
    "AgentRuntime": {
        "actions": ["bedrock-agentcore:InvokeAgentRuntime"],
        "arn": "*",          # runtime ARN 形态多变，先用 * 再收窄
        "also": [],
        "xray_types": ("AWS::BedrockAgentCore",),
    },
}

#: 内联策略名。固定前缀便于识别与清理遗留。
POLICY_PREFIX = "ChaosDenyProbe"

#: 观测下限。低于它拒绝出判定 —— 判定器对「被测路径本身」的调用数有要求，
#: 少于这个数时任何退化数字都是噪声（回执里那 2 条 inconclusive 的原因）。
MIN_BASELINE_REQUESTS = 20

#: 基线成功率下限。低于它拒绝开跑。
#:
#: 2026-09-13 实测踩过：连续跑两次实验，第二次的「基线」窗口里成功率只有
#: 56.63% —— 因为上一次的 deny 还在生效（会话缓存 + 传播延迟）。
#: 拿一个已经坏了的基线去算退化，delta 毫无意义。
MIN_BASELINE_SUCCESS_RATE = 95.0

#: 连续多少次业务探针正常才算恢复。单次正常不够 —— 恢复期会在多 Pod 之间抖动。
_RECOVERY_STREAK = 8
_RECOVERY_BUDGET_SECONDS = 420

#: 业务服务所在 namespace。
_NAMESPACE = os.environ.get("PETSITE_NAMESPACE", "petadoptions")


def _k8s_workload(service: str) -> str | None:
    """图谱服务名 → 集群里真实存在的 Deployment 名。

    用 `service_names.k8s_candidates()` 取候选，再用集群实际存在的名字确认 ——
    候选表可能过期，集群不会。
    """
    from runner import service_names as sn
    r = subprocess.run(["kubectl", "get", "deploy", "-n", _NAMESPACE,
                        "-o", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        live = {i["metadata"]["name"] for i in json.loads(r.stdout).get("items", [])}
    except Exception:
        return None
    for cand in list(sn.k8s_candidates(service)) + [service]:
        if cand in live:
            return cand
    return None


def _wait_recovery(service: str, biz_base: dict) -> tuple[dict, bool]:
    """轮询该服务的业务探针，直到连续 `_RECOVERY_STREAK` 次全部回到基线。

    为什么要连续而不是单次：恢复期不同 Pod 的凭证刷新进度不同，探针会在正常与
    退化之间抖动（实测 26/0/0/0/26/26/26/0/0/26…）。单次正常就宣布恢复，
    会在服务实际仍半坏的状态下退出，把一个退化状态留给线上而脚本已经结束。

    **全部探针都要回到基线**才算一次正常 —— 只看其中一个会漏掉「首页好了但
    领养还挂着」这种半恢复。
    """
    hist: list[dict] = []
    streak, waited = 0, 0
    base_min = {k: min(v for v in vals if v is not None)
                for k, vals in (biz_base.get("per_probe") or {}).items()
                if any(v is not None for v in vals)}
    while waited < _RECOVERY_BUDGET_SECONDS:
        cur = _probe_business(service, n=1)
        per = cur.get("per_probe") or {}
        got = {k: (v[0] if v else None) for k, v in per.items()}
        hist.append(got)
        all_back = bool(base_min) and all(
            got.get(k) is not None and got[k] >= base_min[k] for k in base_min)
        streak = streak + 1 if all_back else 0
        if streak >= _RECOVERY_STREAK:
            return {"ok": True, "history": hist[-12:], "recovered": True,
                    "detail": "连续 %d 次全部探针回到基线" % _RECOVERY_STREAK}, True
        time.sleep(5)
        waited += 5
    return {"ok": True, "history": hist[-12:], "recovered": False,
            "detail": "%ds 预算内未连续 %d 次恢复"
                      % (_RECOVERY_BUDGET_SECONDS, _RECOVERY_STREAK)}, False


def _aws(*args) -> tuple[dict | None, str]:
    r = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr.strip()[:300]
    return (json.loads(r.stdout) if r.stdout.strip() else {}), ""


def _account_id() -> str | None:
    d, _e = _aws("sts", "get-caller-identity")
    return d.get("Account") if d else None


def _irsa_role_for(service: str) -> tuple[str | None, str]:
    """从 K8s ServiceAccount 的注解现取 IRSA 角色名。

    **不维护服务→角色的映射表。** 本仓库有过五次「凭记忆写名字 → 相信空结果」
    的记录；集群里的注解是唯一不会说谎的来源，而且服务重建后角色名会变。
    """
    r = subprocess.run(["kubectl", "get", "sa", "-A", "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, "kubectl 查 ServiceAccount 失败：%s" % r.stderr.strip()[:120]
    try:
        items = json.loads(r.stdout).get("items", [])
    except json.JSONDecodeError:
        return None, "ServiceAccount 输出不是 JSON"

    # 服务名到 SA 名的常见形态：petsearch → search-service-sa
    cands = []
    for it in items:
        ann = (it["metadata"].get("annotations") or {})
        arn = ann.get("eks.amazonaws.com/role-arn")
        if not arn:
            continue
        cands.append((it["metadata"]["name"], arn.split("/")[-1]))

    from runner import service_names as sn  # 服务名解析的单一来源

    # `petsearch`（图谱名）与 `search-service`（K8s 名）不同 —— 这是本仓库记录过
    # 的坑（另有 payforadoption / pay-for-adoption、trafficgenerator /
    # traffic-generator）。`k8s_candidates()` 是解析这件事的唯一权威入口，
    # **不要自己拼名字**。
    #
    # ⚠️ 我第一版凭记忆猜了 `aliases_for` / `k8s_names_for` 这些函数名，
    # 结果全都不存在、候选集只剩服务名本身，于是角色查不到。
    # 「凭记忆写名字」在这个仓库已经是第六次了 —— 用之前先读模块。
    aliases = set(sn.k8s_candidates(service)) | {service}

    for sa_name, role in cands:
        base = sa_name[:-3] if sa_name.endswith("-sa") else sa_name
        if base in aliases or base.replace("-", "") in {
                a.replace("-", "") for a in aliases}:
            return role, "由 ServiceAccount %s 的注解取得（候选 %s）" % (
                sa_name, sorted(aliases))
    return None, ("找不到 %s 的 IRSA 角色。候选 K8s 名 %s，"
                  "集群里带 IRSA 的 SA: %s。"
                  "没有独立角色的服务无法用本手段 —— 半径会超出一个服务。"
                  % (service, sorted(aliases), sorted({c[0] for c in cands})))


def _deny_document(label: str, target: str, acct: str) -> dict | None:
    m = SEVERANCE_METHODS.get(label)
    if not m:
        return None
    fmt = dict(region=REGION, acct=acct, name=target)
    res = [m["arn"].format(**fmt)] + [a.format(**fmt) for a in m["also"]]
    # AWSServiceEndpoint 的 action 模板含 {name}（服务名即 action 前缀）
    acts = [a.format(**fmt) for a in m["actions"]]
    return {"Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "Action": acts,
                           "Resource": res}]}


#: 业务探针：petsite 首页。petsearch 取不到数据时这里会返回 0 个 petid。
_PETSITE_URL = os.environ.get(
    "PETSITE_INTERNAL_URL",
    "http://internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com")
_PETID_RE = __import__("re").compile(r'class="ps-petid">Pet #([^<]+)<')


def _probe_business(service: str, n: int = 3) -> dict:
    """跑**该源服务**登记的全部业务探针。

    ## 为什么探针由源服务决定

    被验证的命题是「这个服务失去这个依赖后，**它的**业务输出坏不坏」。
    2026-09-13 第一版这里硬编码了 petsite 首页探针，而 28 条待验边里只有 4 条
    在搜索路径上。拿首页探针测 `petsite -PublishesTo-> SQSQueue` 会得到
    「注入生效但业务正常」→ 判成 `inconclusive: 消费方存在降级路径`。
    **那个结论是反的**：SQS 断了不影响首页，但它影响领养提交。
    用错探针不是「测不出」，是得出一个假结论并写进合规报告。

    注册表在 `chaos/code/runner/business_probes.py` 的 `SERVICE_PROBES`。
    petsite 登记了四个（home/adopt/list/waggle），因为某条依赖可能只坏其中一个。

    没登记探针时返回 `no_probe=True` —— 调用方必须据此**拒绝出 confirmed**，
    而不是退回某个默认探针。没有业务证据的 confirmed 是过度声称。
    """
    from runner.business_probes import probes_for, run_probes
    if not probes_for(service):
        return {"ok": False, "no_probe": True, "per_probe": {},
                "detail": "源服务 %s 未登记业务探针" % service}
    raw = run_probes(service, n=n, gap=3.0)
    per: dict[str, list] = {}
    all_ok = True
    for name, results in raw.items():
        vals = []
        for r in results:
            if not r.get("ok"):
                all_ok = False
            vals.append(r.get("value"))
        per[name] = vals
    detail = "; ".join("%s=%s" % (k, v) for k, v in sorted(per.items()))
    return {"ok": all_ok, "no_probe": False, "per_probe": per, "detail": detail}


def _biz_baseline_ok(biz: dict) -> tuple[bool, str]:
    """基线是否可用作判定依据：全部探针采集成功、且每个探针的值稳定且 > 0。

    基线本身抖动或为 0 的探针不能用 —— 故障期的 0 说明不了任何事。
    """
    if biz.get("no_probe"):
        return False, biz["detail"] + " —— 拒绝开跑（没有业务证据的判定不可采信）"
    if not biz.get("ok"):
        return False, "业务探针基线采集失败：%s" % biz["detail"]
    for name, vals in (biz.get("per_probe") or {}).items():
        clean = [v for v in vals if v is not None]
        if not clean or min(clean) <= 0:
            return False, "探针 %s 基线本身为 0/缺值（%s）" % (name, vals)
        if len(set(clean)) > 1:
            return False, "探针 %s 基线抖动（%s）—— 抖动的基线无法与退化区分" % (name, vals)
    return True, biz["detail"]


def _biz_degraded(base: dict, during: dict) -> tuple[bool, bool, str]:
    """比较基线与故障期，返回 (是否退化, 是否有探针归零, 说明)。

    **按每个探针的最小值比**，不用 max —— 采样 [0,26,0,0] 的 max 是 26，
    一个正常样本会盖掉三个退化样本，结论正好反了（2026-09-13 实测踩过）。
    抖动的成因是多 Pod 各自传播策略状态，不是消费方有降级路径。

    **任一探针退化即算业务退化**：petsite 有四个探针，某条依赖可能只坏其中一个
    （SQS 断了领养挂、首页照常）。要求全部退化才算，会把真实影响判成无影响。
    """
    degraded = broke = False
    notes = []
    bp = base.get("per_probe") or {}
    dp = during.get("per_probe") or {}
    for name, bvals in sorted(bp.items()):
        bclean = [v for v in bvals if v is not None]
        dclean = [v for v in (dp.get(name) or []) if v is not None]
        if not bclean or not dclean:
            notes.append("%s：故障期无有效采样" % name)
            continue
        b_min, d_min = min(bclean), min(dclean)
        if d_min == 0 and b_min > 0:
            broke = degraded = True
            notes.append("%s 归零（%s → 0，采样 %s）" % (name, b_min, dclean))
        elif d_min < b_min:
            degraded = True
            notes.append("%s 退化（%s → %s）" % (name, b_min, d_min))
        else:
            notes.append("%s 未退化（%s）" % (name, d_min))
    return degraded, broke, "；".join(notes)


class _LoadDriver:
    """贯穿整场实验的背景流量驱动。

    ## 为什么需要它：候选边的稳态流量够不到判定门槛

    2026-09-13 实测 petsite 的写入类依赖在 180s 窗口里的调用数：
    `→ SNSTopic` 1 次、`→ SQSQueue` 0~8 次，而基线门槛是 20 次。
    领养 cron 每 5 分钟 8 次突发（≈5 次/180s）远远不够。
    闸门拒绝是对的 —— 打不断一个没在跑的东西 —— 所以要造流量，不是放宽闸门。

    ## 为什么是「贯穿」而不是「预热突发」

    `_measure` 查的是**过去** `window` 秒，而基线期和故障期各有一个窗口。
    只在开跑前打一轮突发，只能填满基线窗口；故障期窗口会是空的，
    于是 `during` 采样不足 → 成功率读作「未变化」→ 判定出一个假的
    `inconclusive`。所以流量必须从基线前一直跑到故障期测量结束。

    ## 为什么不复用 `probe_adopt`

    两个原因。一是 `probe_adopt` 用固定 `userId`，而它的 `/housekeeping`
    清理是按 userId 做的 —— 多个并发 worker 共用一个 uid 会互相取消
    对方在途的领养。二是**测量探针必须与负载发生器相互独立**：共用状态时
    负载侧的故障会被读成业务故障，那正是判定要区分的两件事。
    ## 速率怎么定：一个被自己的实验证伪的模型，与它的真正教训

    第一版实测：负载 162 次领养 / 240s（≈40 次/分），服务图上
    `→ SNSTopic` 与 `→ SQSQueue` 各 14 次 / 180s。我据此写下
    「观测数 ≈ 实际调用数 × 采样率（X-Ray 默认 1/秒储备 + 5%）」，
    并把速率提到 120 次/分求 2 倍余量。

    **结果观测数从 14 降到 2** —— 流量翻 3 倍而观测数下降，与该模型直接矛盾。
    真实原因不在采样：6 个 worker 当时都抢同一只宠物 `ids[0]`，把它领养成
    `Unavailable` 之后支付不再成功，**根本没发生 SNS/SQS 发布**。
    观测数下降是因为真实调用消失了。

    教训有两条，第二条更重要：
    - 提速前先确认负载本身是否仍在成功地做业务，否则加的是无效请求。
      驱动器只看 HTTP 异常，一个 200 的表单页会被记成一次成功领养。
    - **别把一次相关性当成机制。** 采样率是个真实存在的机制，所以那个解释
      听起来对；但我从没验证过它，而它恰好掩盖了真正的原因。

    速率暫定 120 次/分并保留，因为它对**修好选宠物之后**的路径无害；
    真正需要多少流量才能过 20 次门槛，要等下一次实测才知道 ——
    在那之前不写数字进来假装知道。
    """

    def __init__(self, rate_per_min: int = 120, workers: int = 6) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._workers = workers
        self._gap = max(1.0, 60.0 * workers / max(1, rate_per_min))
        self._sent = 0
        self._failed = 0
        self._lock = threading.Lock()

    def _drive(self, uid: str, slot: int) -> None:
        import urllib.request
        import urllib.parse
        from runner.business_probes import parse_pets
        base = _PETSITE_URL
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(
                        base + "/?userId=" + uid, timeout=20) as r:
                    html = r.read().decode("utf-8", "replace")
                # 只挑**可用**的宠物，并用**它自己的类型**提交。
                # 原先所有 worker 都取 `ids[0]`（同一只宠物），既让它们互相
                # 取消对方的 housekeeping，也把 `pettype` 写死成 puppy ——
                # 实测把宠物 001 留成了 Unavailable，而领养探针恰好也取
                # `ids[0]`，于是探针报「领养功能坏了」而实际 25 只都能领养。
                usable = [p for p in parse_pets(html) if p["available"]]
                if usable:
                    # 按 worker 序号错开取，避免 6 个 worker 抢同一只。
                    pet = usable[slot % len(usable)]
                    data = urllib.parse.urlencode(
                        {"petId": pet["id"], "pettype": pet["pettype"],
                         "userId": uid}).encode()
                    req = urllib.request.Request(
                        base + "/Payment/MakePayment", data=data,
                        headers={"Content-Type":
                                 "application/x-www-form-urlencoded"})
                    with urllib.request.urlopen(req, timeout=30):
                        pass
                    with self._lock:
                        self._sent += 1
                else:
                    with self._lock:
                        self._failed += 1
            except Exception:
                with self._lock:
                    self._failed += 1
            finally:
                # 每轮都清理：不清理会单调消耗库存，几分钟后可领养的就没了，
                # 而领养探针的前置条件正是「有可用宠物」——
                # 负载会把自己造成的库存枯竭伪装成业务退化。
                try:
                    with urllib.request.urlopen(
                            base + "/housekeeping?userId=" + uid, timeout=20):
                        pass
                except Exception:
                    pass
            self._stop.wait(self._gap)

    def start(self) -> None:
        for i in range(self._workers):
            # 每个 worker 一个独立 uid，见类 docstring 第二条理由。
            t = threading.Thread(target=self._drive,
                                 args=("chaos-load-%d" % i, i), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=15)

    @property
    def stats(self) -> str:
        with self._lock:
            return "已驱动 %d 次领养（失败 %d）" % (self._sent, self._failed)


def _measure(client: str, server: str, window: int,
             xray_types: tuple = ()) -> dict:
    """被测边本身的调用统计 —— 用于判定**注入是否真的生效**。

    采集失败必须体现为 ok=False：`ok=False / success_rate=100 / requests=0`
    这个形态与「真的健康」在结构上无法区分，拿它当基线会把任何后续数字读成退化。
    """
    from runner.xray_metrics import XRayEdgeMetrics
    snap = XRayEdgeMetrics().collect_edge_flow(
        client, server, window_seconds=window, dst_type_prefixes=xray_types)
    return {"ok": bool(getattr(snap, "ok", False)),
            "success_rate": getattr(snap, "success_rate", None),
            "total_requests": getattr(snap, "total_requests", None),
            "p99_ms": getattr(snap, "latency_p99_ms", None)}


def run_probe(service: str, label: str, target: str,
              observer: str, hold_seconds: int, window: int,
              apply: bool, propagation_budget: int = 300,
              warmup: int = 210) -> int:
    acct = _account_id()
    if not acct:
        print("✗ 取不到账号 ID —— 中止（ARN 构造不能猜）")
        return 2

    role, how = _irsa_role_for(service)
    if not role:
        print("✗ %s" % how)
        return 2
    print("IRSA 角色: %s\n  （%s）" % (role, how))

    doc = _deny_document(label, target, acct)
    if doc is None:
        print("✗ 方法表里没有 %r 的切断手段。可用: %s"
              % (label, sorted(SEVERANCE_METHODS)))
        return 2
    print("deny 语句: %s" % json.dumps(doc["Statement"][0], ensure_ascii=False))
    from runner.business_probes import probes_for
    pnames = [n for n, _ in probes_for(service)]
    if not pnames:
        print("✗ 源服务 %s 未登记业务探针 —— 拒绝开跑。" % service)
        print("  没有业务证据的 confirmed 是过度声称：只证明了调用失败，")
        print("  没证明业务受损。请先在 chaos/code/runner/business_probes.py 的")
        print("  SERVICE_PROBES 里为该服务登记探针（并先测出它的稳态基线）。")
        return 2
    print("观测通道: 被测边 X-Ray 统计 + %s 的业务探针 %s（窗口 %ds）"
          % (service, pnames, window))
    print()

    # ── 0. 背景流量（贯穿全场）──
    #
    # 必须在**基线测量之前**就跑满一个观测窗口：`_measure` 查的是过去 window 秒。
    # 见 `_LoadDriver` 的 docstring。
    load = _LoadDriver()
    if apply and warmup > 0:
        print("── 0. 背景流量 ──")
        print("   启动负载驱动（6 worker，各自独立 userId），"
              "lead-in %ds 让基线窗口跑满…" % warmup)
        load.start()
        time.sleep(warmup)
        print("   %s" % load.stats)
        print()

    print("── 1. 基线 ──")
    xray_types = tuple(SEVERANCE_METHODS[label].get("xray_types") or ())
    base = _measure(service, target, window, xray_types)
    print("   被测边 %s -> %s: %s" % (service, target[:26], base))
    biz_base = _probe_business(service)
    print("   业务探针: %s" % biz_base["detail"])
    if not base["ok"]:
        print("✗ 被测边基线采集失败（ok=False）—— 中止。")
        print("  ok=False 表示这个采样点没有数据，不表示指标为 0。"
              "拿它当基线会把任何后续数字读成退化。")
        load.stop()
        return 3
    if (base["total_requests"] or 0) < MIN_BASELINE_REQUESTS:
        print("✗ 基线请求数 %s < 下限 %d —— 拒绝出判定。"
              % (base["total_requests"], MIN_BASELINE_REQUESTS))
        print("  打不断一个没在跑的东西；此时任何退化数字都是噪声。")
        print("  %s" % load.stats)
        load.stop()
        return 3
    if (base["success_rate"] or 0) < MIN_BASELINE_SUCCESS_RATE:
        print("✗ 基线成功率 %.2f%% < 下限 %.0f%% —— 拒绝开跑。"
              % (base["success_rate"] or 0, MIN_BASELINE_SUCCESS_RATE))
        print("  基线本身已经是坏的，拿它算退化 delta 毫无意义。")
        print("  常见原因：上一次实验的 deny 仍在生效（会话缓存 + IAM 传播延迟）。")
        print("  处置：等前一次实验完全恢复（或 rollout restart 该服务）后再跑。")
        load.stop()
        return 3
    biz_ok, biz_why = _biz_baseline_ok(biz_base)
    if not biz_ok:
        print("✗ 业务探针基线不可用：%s" % biz_why)
        print("  基线就坏或会抖的话，故障期的数字说明不了任何事。")
        load.stop()
        return 3

    if not apply:
        print()
        print("[dry-run] 两个通道基线都合格，具备执行条件。加 --apply 真跑。")
        load.stop()
        return 0

    # ── 置位实验期互锁 ──
    #
    # 合成流量 cron（领养 / Waggle）会在故障期发现链路坏了并按设计报警 ——
    # 那是本实验的**预期副作用**，不该进用户的通知渠道。2026-09-13 首发实验
    # 就产生了一条这样的假警报。反向也成立：cron 的突发流量会扰动基线与恢复判定。
    #
    # 标记模块刻意从 `~/.kiro/crew/crons/` 导入而不在这里复制一份路径与格式 ——
    # 本仓库有过「同一份清单四处各抄一份、其中两处漂移到实际错误」的记录。
    _lock = None
    try:
        sys.path.insert(0, os.path.expanduser("~/.kiro/crew/crons"))
        import chaos_lock as _lock  # type: ignore
    except Exception as _e:
        print("⚠️ 取不到实验期互锁模块（%r）—— 合成流量 cron 可能在故障期报假警报"
              % _e)

    # 预算给足余量：restart + 生效轮询 + hold + 恢复确认，宁可多标一会儿。
    budget = 240 + propagation_budget + hold_seconds + _RECOVERY_BUDGET_SECONDS
    if _lock:
        _lock.begin("%s -> %s" % (service, target), budget,
                    note="iam-deny probe")
        print("已置位实验期互锁（%ds 后自动失效）—— 合成流量 cron 本期间整轮跳过"
              % budget)

    policy_name = "%s-%s" % (POLICY_PREFIX, int(time.time()))
    during = biz_during = post = None
    removed = False
    try:
        print()
        print("── 2. 施加 deny（策略 %s）──" % policy_name)
        d, e = _aws("iam", "put-role-policy", "--role-name", role,
                    "--policy-name", policy_name,
                    "--policy-document", json.dumps(doc))
        if d is None:
            print("✗ 施加失败: %s" % e)
            return 4
        print("   已施加。")

        # ── 2a. 立即刷新凭证，让切断**均匀**作用到所有 Pod ──
        #
        # 2026-09-13 第二次实测发现的问题：只施加 deny 时，边成功率停在
        # 51.47% ≈ 2 个 Pod 里只有 1 个被拒 —— 另一个 Pod 的会话仍在用旧的
        # 策略评估结果，业务探针打到它就正常（[26,26,26,26]），于是判定
        # 误写成「业务未退化，消费方有降级路径」。
        #
        # 施加后立即 rollout restart：所有 Pod 换新会话、统一看到 deny，
        # 切断变成均匀的。
        #
        # 这**不会**引入回执里警告过的「删 Pod 让信号混淆」问题，因为：
        #   1) 用 rollout restart（滚动），故障期始终有 Pod 在服务；
        #   2) 等 rollout **完成**后才开始测量 —— 测量窗口里没有重启在进行；
        #   3) 业务探针看的是输出内容（有没有宠物），重启本身不会让它归零。
        k8s_name = _k8s_workload(service)
        if k8s_name:
            print("   刷新凭证使切断均匀：rollout restart deploy/%s" % k8s_name)
            subprocess.run(["kubectl", "rollout", "restart",
                            "deploy/%s" % k8s_name, "-n", _NAMESPACE],
                           capture_output=True, text=True)
            r = subprocess.run(["kubectl", "rollout", "status",
                                "deploy/%s" % k8s_name, "-n", _NAMESPACE,
                                "--timeout=240s"], capture_output=True, text=True)
            print("      %s" % (r.stdout.strip().splitlines()[-1]
                                if r.stdout.strip() else r.stderr.strip()[:120]))
        else:
            print("   ⚠️ 取不到 K8s 工作负载名，切断可能只作用到部分 Pod")

        print("   等待注入生效 —— 不假设固定延迟，轮询到真的生效为止。")

        # ── 2b. 等注入生效（这一步是 2026-09-13 首发实验之后加的）──
        #
        # 我原先写的判据是「SigV4 每次调用评估，所以下一个请求即生效」。
        # **只对了一半**：评估确实是每次调用做的，但评估器看到的**策略状态**
        # 是最终一致的。首发实验实测：deny 施加后 +20s 与 +170s 业务都正常，
        # 业务中断出现在**回滚之后** +25s（首页宠物 26 → 0 → 恢复 26）——
        # 传播延迟约 2~3 分钟，整个观测窗口都错位了，判定成了 inconclusive。
        #
        # 所以不猜延迟：轮询业务探针直到它退化，那才是「注入已生效」的证据。
        # 到预算还不退化就诚实报 inconclusive，绝不把「没等到」写成「不依赖」。
        #
        # **任一探针退化即算生效**：某条依赖可能只坏源服务的一个功能
        # （SQS 断了领养挂、首页照常），要求全部退化会永远等不到。
        effective_at = None
        for i in range(propagation_budget // 15):
            time.sleep(15)
            probe = _probe_business(service, n=1)
            deg, _brk, note = _biz_degraded(biz_base, probe)
            print("      [+%3ds] %s" % ((i + 1) * 15, note))
            if deg:
                effective_at = (i + 1) * 15
                print("      ✓ 注入已生效（第 %ds）" % effective_at)
                break
        if effective_at is None:
            print("      ⚠️ %ds 预算内业务未退化 —— 注入可能未生效或消费方有降级路径"
                  % propagation_budget)

        biz_during = _probe_business(service, n=4)
        print("   业务探针（生效后）: %s" % biz_during["detail"])

        print("   等待 %ds 让 X-Ray 聚合出足够样本…" % hold_seconds)
        time.sleep(hold_seconds)
        during = _measure(service, target, window, xray_types)
        print("   被测边（故障期）: %s" % during)
    finally:
        print()
        print("── 3. 回滚（finally，任何路径都执行）──")
        print("   背景流量：%s" % load.stats)
        d, e = _aws("iam", "delete-role-policy", "--role-name", role,
                    "--policy-name", policy_name)
        removed = d is not None
        print("   %s" % ("已删除内联策略" if removed else "✗ 删除失败: %s" % e))
        if not removed:
            print()
            print("   ⚠️⚠️ 手工回滚（不做的话该服务会一直 403）：")
            print("   aws iam delete-role-policy --role-name %s "
                  "--policy-name %s --region %s" % (role, policy_name, REGION))

        # ── 3b. 强制刷新凭证 ──（2026-09-13 首发实验之后加的，非可选）
        #
        # **删掉策略不足以恢复。** 实测：策略删除后 6 分钟，被测边成功率仍只有
        # 14.08%，petsearch 日志里 5 分钟内 539 条 AccessDenied，业务探针在
        # 26 与 0 之间抖动。AWS 对**已有会话**的策略评估有缓存，删策略不会
        # 立刻作用到正在跑的 Pod 上。
        #
        # 滚动重启后连续 8 次探针全部正常 —— 换取新凭证会立即清除该状态。
        #
        # 所以这一步是**必须的**：不做的话实验会给线上留下一个无界时长的
        # 退化状态，而脚本已经退出、没人知道。用 rollout restart 而不是
        # delete pod：滚动更新保证故障期始终有 Pod 在服务。
        k8s_name = _k8s_workload(service)
        if k8s_name:
            print("   刷新凭证：rollout restart deploy/%s" % k8s_name)
            r = subprocess.run(
                ["kubectl", "rollout", "restart", "deploy/%s" % k8s_name,
                 "-n", _NAMESPACE], capture_output=True, text=True)
            print("      %s" % (r.stdout.strip() or r.stderr.strip()[:120]))
            r = subprocess.run(
                ["kubectl", "rollout", "status", "deploy/%s" % k8s_name,
                 "-n", _NAMESPACE, "--timeout=180s"],
                capture_output=True, text=True)
            print("      %s" % (r.stdout.strip().splitlines()[-1]
                                if r.stdout.strip() else r.stderr.strip()[:120]))
        else:
            print("   ⚠️ 取不到 %s 的 K8s 工作负载名，未能刷新凭证 —— "
                  "服务可能仍处于 403 状态，需手工 rollout restart" % service)

    print()
    print("── 4. 恢复确认（连续 %d 次正常才算恢复）──" % _RECOVERY_STREAK)
    biz_post, recovered = _wait_recovery(service, biz_base)
    print("   %s" % biz_post)
    if not recovered:
        print("   ⚠️⚠️ 业务未在预算内恢复 —— 线上仍处于退化状态，需人工介入：")
        print("      kubectl rollout restart deploy/%s -n %s"
              % (_k8s_workload(service) or service, _NAMESPACE))
    post = _measure(service, target, window, xray_types)

    # ── 判定 ──
    print()
    print("── 判定 ──")
    verdict, why = _verdict(base, during, post, biz_base, biz_during, biz_post)
    print("   %s：%s" % (verdict, why))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = _ROOT / "todo" / ("iam-deny-probe_%s.json" % stamp)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "taken_at": stamp, "service": service, "target_label": label,
        "target": target, "irsa_role": role,
        "severance": "iam-deny", "deny_document": doc,
        "channel_edge": {"baseline": base, "during": during, "post": post},
        "channel_business": {"baseline": biz_base, "during": biz_during,
                             "post": biz_post},
        "policy_removed": removed,
        "verdict": verdict, "why": why,
        "caveat": "IAM deny 证明依赖承重，不等于延迟/部分失败场景测试",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("   记录: %s" % out.relative_to(_ROOT))

    # ── 写回图谱 ──
    # 不写回的话，跑再多实验覆盖率也不会动 —— JSON 记录只有人看得到。
    persisted = _persist_verdict(service, label, target, verdict, why,
                                 base, during or {}, out.stem)
    print("   写回: %s" % persisted)

    if _lock:
        _lock.end()
        print("   已释放实验期互锁 —— 合成流量 cron 下一轮恢复正常。")
    # 恢复确认期间刻意保留负载（更贴近真实），到这里才停。
    load.stop()
    print("   背景流量已停：%s" % load.stats)
    return 0


def _edge_id(service: str, target: str) -> tuple[str | None, str, str]:
    """按 (源, 目标) 精确定位边，返回 (边 id, 边标签, 说明)。

    ## 边标签由图谱返回，不由调用方给

    ⚠️ 第一版让调用方传 `label`，而调用方手上的是 `--edge` 里的
    `TargetLabel`（**节点**类型，如 `DynamoDBTable`，用于查方法表）——
    不是**边**类型（`AccessesData`）。于是查询恒空，写回静默失败：
    「图谱里找不到 petsearch -[DynamoDBTable]-> ...」。

    节点类型与边类型是两个不同的东西，同一个变量名承载两种语义就会撞。
    现在边标签从图谱读，调用方给不出错的东西。

    ## 为什么自己查而不用 `candidate_edges()`

    那个函数对 `injection_target` 先做 `_resolve_graph_name()`（为服务名设计），
    而这里的目标是 DynamoDB 表、S3 桶一类**非服务节点**，解析规则不适用。

    命中 0 条或 >1 条都拒绝写回 —— 写错一条边的 `verify_status` 比不写更糟：
    报告会声称某依赖已确证，而证据其实来自另一条边。
    """
    from runner.neptune_helpers import query_gremlin_parsed
    esc_s = service.replace("'", "")
    esc_t = target.replace("'", "")
    q = ("g.V().has('name','%s').outE().as('e')"
         ".inV().has('name','%s')"
         ".select('e').project('eid','elabel')"
         ".by(__.id()).by(__.label()).fold()" % (esc_s, esc_t))
    try:
        rows = query_gremlin_parsed(q)
    except Exception as e:
        return None, "", "查询边 id 失败: %r" % e
    while isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], list):
        rows = rows[0]
    hits = [r for r in (rows or []) if isinstance(r, dict) and r.get('eid')]
    if not hits:
        return None, "", ("图谱里找不到 %s -> %s 的任何边。空结果必须响 —— "
                          "它可能是真没这条边，也可能是名字对不上，"
                          "而两者在日志里长得一样时后者会被当成前者放过。"
                          % (service, target))
    if len(hits) > 1:
        labels = sorted({h.get('elabel') for h in hits})
        return None, "", ("%s -> %s 之间有 %d 条边（%s），拒绝写回 —— "
                          "写错一条边的 verify_status 比不写更糟。"
                          % (service, target, len(hits), labels))
    return hits[0]['eid'], str(hits[0].get('elabel') or ''), "唯一匹配"


def _persist_verdict(service: str, label: str, target: str,
                     verdict: str, why: str, base: dict, during: dict,
                     record_id: str) -> str:
    """把判定写回图谱边属性。只有 confirmed / inconclusive 才写。

    `observation_only` **刻意不写** —— 它的含义是「采集到的信号不足以判定」，
    写进 `verify_status` 会让它看起来像一个结论。回执里的教训：用混淆信号得出的
    结论会被 DR 影响面分析当真并在预案里降级，比 untested 危险得多。

    入参 `label` 是**节点**类型（用于记录），边标签从图谱读。
    """
    if verdict not in ("confirmed", "inconclusive"):
        return "未写回（%s 不是结论，写进 verify_status 会让它看起来像结论）" % verdict

    eid, elabel, how = _edge_id(service, target)
    if not eid:
        return "未写回：%s" % how

    from runner.edge_verification import write_verdict
    b_sr = base.get("success_rate") or 0.0
    d_sr = during.get("success_rate") or 0.0
    ok = write_verdict({
        "edge_id": eid,
        "label": elabel,
        "observer": service,
        "status": verdict,
        "reason": why,
        "confidence": 0.9 if verdict == "confirmed" else 0.4,
        "degradation_pct": round(b_sr - d_sr, 2),
        # `verified_at` 是 write_verdict 的必填字段（epoch 秒）。
        # 少了它是 KeyError 而不是静默写错，这个失效形状是好的。
        "verified_at": int(time.time()),
        # 双通道：被测边的 X-Ray 统计 + petsite 首页业务输出断言
        "evidence_channel": "xray-edge+business-probe",
        # 切断手段必须落在边上，报告据此披露证据的适用范围
        "severance": "iam-deny",
        "verifier": "iam-deny-probe",
        "experiment_id": record_id,
        "confirm_count": 1 if verdict == "confirmed" else 0,
        "refute_count": 0,
        "observing_sources": 2,
        "dependency_class": None,
        "dependency_class_reason":
            "IAM deny 证明依赖承重，不覆盖延迟/部分失败场景，"
            "不足以给出 hard/soft 分级",
    })
    return ("已写回边 %s（%s，severance=iam-deny）" % (eid[:20], elabel) if ok
            else "写回失败，见日志")


def _verdict(base: dict, during: dict | None, post: dict | None,
             biz_base: dict, biz_during: dict | None,
             biz_post: dict | None) -> tuple[str, str]:
    """双通道判定。任一通道混淆或采集失败就**拒绝出结论**。

    两个通道回答两个不同问题，缺一不可：

        被测边  注入是否真的生效（deny 有没有让调用失败）
        业务探针 消费方业务是否真的退化（首页还能不能列出宠物）

    只有边退化而业务不退化 → 说明消费方有降级路径，那是 `inconclusive`
    而不是 `confirmed`。只有业务退化而边不退化 → 说明退化另有原因，
    不能归给这次注入。

    回执里的教训：用混淆信号得出的 `soft` 会被 DR 影响面分析读成
    「这条依赖不影响可用性」并在预案里降级 —— 比 untested 危险得多。
    """
    if during is None or not during.get("ok"):
        # ── 完全切断：边从服务图上**消失**而不是带错误出现 ──
        #
        # 2026-09-13 实测 `petsite -> SNSTopic`：基线 43 次 → 故障期 0 次且
        # ok=False → 回滚后 32 次。原因是 deny 让 SDK 调用抛异常，而 petsite
        # 的埋点不为失败的 SDK 调用发子段，于是这条边整个不出现在服务图里。
        #
        # 这**不是偶发**，是「IAM deny + 这套埋点」的固有性质：托管服务类依赖
        # 被完全切断时，边通道永远会是 ok=False。若一律判 observation_only，
        # 这一整类边（SNS / SQS / StepFunction）就永远不可确认。
        #
        # 判据不是放宽，而是认出同一证据的另一种形态。边通道的职责是证明
        # **注入真的生效**，而「被前后夹住的消失」比成功率下降更强地满足它：
        #
        #   基线窗口有 ≥MIN 次调用   —— 这条边本来在跑（不是零流量边）
        #   故障期窗口完全消失       —— 调用停止了
        #   回滚后窗口重新出现       —— **这一环排除「聚合延迟/采样波动」**，
        #                              因为同一套采集在前后两个窗口都看得见它
        #
        # 缺了第三环就仍然是 observation_only：单看「消失」无法与「X-Ray 这个
        # 窗口没聚合到」区分。
        b_req = (base.get("total_requests") or 0)
        p_ok = bool(post and post.get("ok"))
        p_req = (post or {}).get("total_requests") or 0
        bracketed = (b_req >= MIN_BASELINE_REQUESTS and p_ok and p_req > 0)
        if not bracketed:
            return "observation_only", (
                "被测边故障期采集失败（ok=False），且未被前后夹住"
                "（基线 %d 次 / 回滚后 ok=%s、%d 次）—— "
                "无法区分「调用停止」与「本窗口没聚合到」" % (b_req, p_ok, p_req))
        if biz_during is None or not biz_during.get("ok"):
            return "observation_only", (
                "被测边完全消失且前后夹住，但业务探针故障期采集失败 —— "
                "缺业务侧证据，不出 confirmed")
        degraded, broke, biz_note = _biz_degraded(biz_base, biz_during)
        rec = ("；回滚后恢复" if (biz_post or {}).get("recovered")
               else "；⚠️ 回滚后未在预算内恢复")
        if broke:
            return "confirmed", (
                "完全切断：被测边基线 %d 次 → 故障期从服务图消失 → 回滚后 %d 次"
                "（前后夹住，排除聚合延迟），且业务归零（%s）%s"
                % (b_req, p_req, biz_note, rec))
        if degraded:
            return "confirmed", (
                "完全切断：被测边基线 %d 次 → 故障期消失 → 回滚后 %d 次，"
                "且业务退化（%s）%s" % (b_req, p_req, biz_note, rec))
        return "inconclusive", (
            "完全切断（基线 %d 次 → 消失 → 回滚后 %d 次）但业务探针全部未退化"
            "（%s）—— 消费方存在降级路径，该依赖非业务关键路径。"
            "**不写 confirmed**：证明了调用停止，没证明业务受损"
            % (b_req, p_req, biz_note))
    if biz_during is None or not biz_during.get("ok"):
        return "observation_only", "业务探针故障期全部请求失败，无法区分「依赖被切断」与「探针本身不可达」"

    b_sr, d_sr = base.get("success_rate"), during.get("success_rate")
    if b_sr is None or d_sr is None:
        return "observation_only", "成功率缺值，无法判定"
    edge_drop = b_sr - d_sr

    # 按**每个探针各自的最小值**比较，见 `_biz_degraded` 的判据说明。
    # 关键两点：不用 max（一个正常样本会盖掉多个退化样本）；
    # 任一探针退化即算业务退化（某条依赖可能只坏源服务的一个功能）。
    degraded, broke, biz_note = _biz_degraded(biz_base, biz_during)

    rec = ""
    if biz_post:
        rec = ("；回滚+刷新凭证后恢复" if biz_post.get("recovered")
               else "；⚠️ 回滚后未在预算内恢复")

    if edge_drop >= 20 and broke:
        return "confirmed", (
            "注入生效（边成功率 %.1f%% → %.1f%%，降 %.1fpp）"
            "且业务归零（%s）%s" % (b_sr, d_sr, edge_drop, biz_note, rec))
    if edge_drop >= 20 and degraded:
        return "confirmed", (
            "注入生效（降 %.1fpp）且业务退化（%s）%s"
            % (edge_drop, biz_note, rec))
    if edge_drop >= 20:
        return "inconclusive", (
            "注入生效（边成功率降 %.1fpp）但业务探针全部未退化（%s）—— "
            "消费方存在降级路径，该依赖非业务关键路径。"
            "**不写 confirmed**：证明了调用失败，没证明业务受损"
            % (edge_drop, biz_note))
    if broke:
        return "observation_only", (
            "业务归零（%s）但被测边成功率只降 %.1fpp —— "
            "退化原因可能不是这次注入，信号混淆，拒绝出判定"
            % (biz_note, edge_drop))
    return "inconclusive", (
        "两个通道都未见明显退化（边降 %.1fpp；%s）。"
        "可能 deny 未覆盖应用实际使用的 action，或注入未在预算内生效"
        % (edge_drop, biz_note))
    if broke:
        return "observation_only", (
            "业务归零但被测边成功率只降 %.1fpp —— "
            "退化原因可能不是这次注入，信号混淆，拒绝出判定" % edge_drop)
    return "inconclusive", (
        "两个通道都未见明显退化（边降 %.1fpp、业务探针 %s）。"
        "可能 deny 未覆盖应用实际使用的 action，或注入未在预算内生效"
        % (edge_drop, counts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", help="service:TargetLabel:targetName")
    ap.add_argument("--observer", help="观测方服务名（默认 petsite）",
                    default="petsite")
    ap.add_argument("--hold", type=int, default=150, help="故障保持秒数")
    ap.add_argument("--window", type=int, default=180, help="观测窗口秒数")
    ap.add_argument("--propagation-budget", type=int, default=300,
                    help="等 IAM 传播生效的预算秒数（默认 300）")
    ap.add_argument("--warmup", type=int, default=210,
                    help="背景流量 lead-in 秒数（必须 ≥ --window，"
                         "否则基线窗口跑不满；0 表示不造流量）")
    ap.add_argument("--apply", action="store_true", help="真跑（默认 dry-run）")
    ap.add_argument("--list", action="store_true", help="列出方法表")
    a = ap.parse_args()

    if a.list or not a.edge:
        print("声明式方法表（目标类型 → 切断手段）:")
        for k, v in sorted(SEVERANCE_METHODS.items()):
            print("  %-18s deny %s" % (k, v["actions"]))
        if not a.edge:
            print()
            print("用法: --edge service:TargetLabel:targetName [--apply]")
        return 0

    parts = a.edge.split(":", 2)
    if len(parts) != 3:
        print("✗ --edge 格式应为 service:TargetLabel:targetName")
        return 2
    return run_probe(parts[0], parts[1], parts[2], a.observer,
                     a.hold, a.window, a.apply, a.propagation_budget, warmup=a.warmup)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""PetSite + AgentCore 全覆盖负载生成器。

只用标准库 + boto3（压测机上都有），不装任何东西 ——
压测机是 Amazon Linux 2023 / ec2-user，且**读不了 CDK 资产桶**（403），
所以本脚本靠 base64 内联进 SSM 命令投递，不要改成走 S3。

## 为什么要重写

原来的压测脚本只覆盖 4 个目标（petsite ×2、search ×2），
而目标是「验证图数据库里的依赖关系」——**没有流量的边根本不会出现在图里**，
更无法验证。所以覆盖面必须与图里的边一一对应：

  HTTP 层：六个 EKS 服务，且要走**带图片的详情路径**才会触发 1% 故障注入
  genai 层：五个 agent，且要走 **orchestrator 的委派路径** ——
           那是 `Delegates` 边的唯一来源（AGENT_TRANSPORT=gateway 的产物）

## 端口可达性（实测，别踩）

从 10.1 段（压测机所在 VPC）经 internal ALB：
  :80   petsite          ✅ 可达
  :8081 search-service   ✅ 可达
  :8082 list-adoptions   ❌ **超时** —— 只对 agentSg 开放，不是故障
  :8083 pay-for-adoption ❌ **超时** —— 同上
所以 :8082/:8083 必须经 petsite 页面间接打，或从 agent 侧打，
直接压会产生一堆假 timeout 掩盖真实信号。
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as cf
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

ALB = os.environ.get(
    "PETSITE_ALB",
    "internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com",
)
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

PET_TYPES = ["puppy", "kitten", "bunny"]
PET_COLORS = ["black", "white", "brown"]


# ── HTTP 目标 ────────────────────────────────────────────────────────────────
def http_targets() -> list[tuple[str, str]]:
    """图里每条 HTTP 依赖边都要有对应流量。

    petsite 是入口，它内部会调 search / listadoptions / payforadoption /
    pethistory / petfood —— 所以打 petsite 的页面能同时给这些边产生流量，
    这比直接压各服务更贴近真实依赖形态（也绕开了 :8082/:8083 的 SG 限制）。
    """
    t: list[tuple[str, str]] = [
        # petsite 入口页与搜索（内部会调 search-service）
        ("petsite-home", f"http://{ALB}/"),
        ("petsite-search", f"http://{ALB}/?selectedPetType={random.choice(PET_TYPES)}"
                           f"&selectedPetColor={random.choice(PET_COLORS)}"),
        # ⚠️ 1% 故障注入（Math.random()*9999 < 100）的触发点在 **search-service
        #    的 getPetUrl**，它在组装每个宠物的 presigned S3 URL 时被调用。
        #    所以触发路径是**搜索**（首页与 /api/search），不是收养列表 ——
        #    我原先把 ("petsite-adoptionlist", "/adoptionlist") 当成注入路径，
        #    既路径错（真实路由是 /PetListAdoptions，旧写法 43/43 全 404），
        #    方向也错（收养列表来自 petlistadoptions 服务，压根不经 getPetUrl）。
        #    上一轮压测实测：搜索流量 1572 次对应 84 次注入日志，比率约 0.36%，
        #    与 100/9999 同量级 —— 证实注入确实由搜索路径触发。
        ("petsite-petlistadoptions", f"http://{ALB}/PetListAdoptions?userId={random.randint(1000, 9999)}"),
        ("petsite-pethistory", f"http://{ALB}/pethistory"),
        # search 直连（图里 petsite -> petsearch 的对端）
        ("search-api", f"http://{ALB}:8081/api/search?pettype={random.choice(PET_TYPES)}"),
        ("search-health", f"http://{ALB}:8081/health/status"),
        # petfood（2026-09-04 已部署并验证：9 条食品、PetTypeIndex ACTIVE）。
        #
        # ⚠️ 真实的食品 UI 是 **/FoodService**，不是 /petfood。
        #    /petfood 是本地保留的遗留桩 PetFoodController（值得保是因为它有
        #    X-Ray subsegment 埋点），而 /FoodService 才是上游新增的正式控制器，
        #    会真正调 /api/foods 与 /api/cart。
        #    /FoodService **必须带 userId**：不带会 302 跳回 /Home/Index，
        #    压出来的是首页流量而不是食品流量 —— 那种假流量最难发现，
        #    因为响应码是 200、字节数也正常。
        ("petsite-foodservice", f"http://{ALB}/FoodService?userId={random.randint(1000, 9999)}"
                                f"&petType={random.choice(['puppy', 'kitten', 'bunny'])}"),
        # 遗留桩也压一下 —— 它的 X-Ray subsegment 是图里 petsite->petfood 边的来源之一
        ("petsite-petfood-legacy", f"http://{ALB}/petfood"),
    ]
    return t


def hit_http(name: str, url: str, timeout: int = 20):
    """打一个 HTTP 目标。

    ⚠️ 只看状态码会**把错误页当成功**。petsite 的异常处理是渲染一个
       "Oops! Something went wrong" 页面并返回 **HTTP 200** ——
       实测 /petfood 在 PetFoodController 打错地址时就是 200 + 错误页，
       压测报表显示 100% 成功，而那个功能其实完全不可用。
       这类假绿最危险：状态码正常、字节数也在合理范围。
       所以额外扫正文里的错误标记，命中就记为 ERRPAGE 而不是成功。
    """
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(65536)
            elapsed = (time.time() - t0) * 1000
            if r.status == 200:
                text = body.decode("utf-8", errors="replace")
                for marker in ("Oops! Something went wrong",
                               "Unable to load",
                               "does not indicate success"):
                    if marker in text:
                        return name, "ERRPAGE", elapsed, len(body)
            return name, r.status, elapsed, len(body)
    except urllib.error.HTTPError as e:
        return name, e.code, (time.time() - t0) * 1000, 0
    except Exception as e:  # noqa: BLE001
        return name, type(e).__name__, (time.time() - t0) * 1000, 0


# ── genai 目标 ───────────────────────────────────────────────────────────────
# 这些 prompt 是**按委派路径设计**的，不是随便问 ——
# 每一条都要让 orchestrator 委派给特定子 agent，才能在图里长出对应的 Delegates 边。
AGENT_PROMPTS = [
    # -> Nutrition（并触发 Retrieves 到 KB）
    "My dog has a sensitive stomach. What food do you recommend?",
    # -> Adoption（并触发 AgentTool search_available_pets -> Microservice petsearch）
    "Show me puppies available for adoption right now.",
    # -> Nutrition + Adoption 双委派（一次产生两条 Delegates）
    "I want to adopt a kitten and need food advice for a young cat.",
    # -> Ordering（petfood 已上线，实调返回真实商品名与价格；
    #    已实测 orchestrator 能拿到 Beef and Turkey Kibbles $12.99 等真实数据）
    "What dog food do you have in stock for a puppy? Add one to my cart.",
    # -> Concierge
    "What services does this pet store offer?",
]


def invoke_agent(runtime_arn: str, prompt: str, timeout: int = 120):
    """经 InvokeAgentRuntime 打 orchestrator。

    刻意不走 Gateway 的 HTTP 端点：Gateway 的 inbound auth 是 AWS_IAM，
    从压测机直接 POST 需要自己签 SigV4；而 InvokeAgentRuntime 由 boto3 签，
    且走的是 orchestrator -> Gateway -> 子 agent 的**同一条委派链**，
    对建图的效果一致而实现简单得多。
    """
    import boto3  # 延迟导入：HTTP-only 模式下不需要

    t0 = time.time()
    try:
        c = boto3.client("bedrock-agentcore", region_name=REGION)
        # session id 有长度下限（实测需 >= 33 字符），太短会被拒
        sid = f"loadgen-{int(time.time()*1000)}-{random.randint(10**12, 10**13)}"
        resp = c.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=sid,
            payload=json.dumps({"prompt": prompt}).encode(),
        )
        body = resp["response"].read()
        return "agent-orchestrator", resp.get("statusCode", 200), (time.time() - t0) * 1000, len(body)
    except Exception as e:  # noqa: BLE001
        return "agent-orchestrator", type(e).__name__, (time.time() - t0) * 1000, 0


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--workers", type=int, default=24, help="HTTP 并发")
    ap.add_argument("--agent-workers", type=int, default=2,
                    help="agent 并发。刻意很低：一次委派要跑几个 LLM 调用，"
                         "并发高只会排队并放大延迟，对建图没有额外收益")
    ap.add_argument("--agent-arn", default="",
                    help="orchestrator 的 runtime ARN；留空则从 SSM 读")
    ap.add_argument("--no-agent", action="store_true", help="只压 HTTP")
    args = ap.parse_args()

    agent_arn = args.agent_arn
    if not agent_arn and not args.no_agent:
        try:
            import boto3
            agent_arn = boto3.client("ssm", region_name=REGION).get_parameter(
                Name="/petstore/agent/waggleairuntimearn"
            )["Parameter"]["Value"]
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 读不到 orchestrator ARN，跳过 agent 压测: {e}")
            args.no_agent = True

    stop_at = time.time() + args.duration
    stats: dict = collections.defaultdict(
        lambda: {"n": 0, "ok": 0, "err": collections.Counter(), "lat": [], "bytes": 0}
    )

    def record(r):
        name, code, lat, nbytes = r
        s = stats[name]
        s["n"] += 1
        s["lat"].append(lat)
        s["bytes"] += nbytes
        if code == 200:
            s["ok"] += 1
        else:
            s["err"][code] += 1

    with cf.ThreadPoolExecutor(max_workers=args.workers + args.agent_workers) as ex:
        inflight: set = set()
        i = 0
        while time.time() < stop_at:
            targets = http_targets()
            while len([f for f in inflight if not f.done()]) < args.workers:
                name, url = targets[i % len(targets)]
                i += 1
                inflight.add(ex.submit(hit_http, name, url))
            if not args.no_agent:
                agent_busy = [f for f in inflight if not f.done()
                              and getattr(f, "_is_agent", False)]
                if len(agent_busy) < args.agent_workers:
                    fut = ex.submit(invoke_agent, agent_arn,
                                    AGENT_PROMPTS[i % len(AGENT_PROMPTS)])
                    fut._is_agent = True  # type: ignore[attr-defined]
                    inflight.add(fut)
            done = {f for f in inflight if f.done()}
            for f in done:
                record(f.result())
            inflight -= done
            time.sleep(0.05)

        for f in cf.as_completed(list(inflight), timeout=180):
            try:
                record(f.result())
            except Exception:  # noqa: BLE001
                pass

    # ── 报告 ──
    def pct(v, p):
        if not v:
            return 0
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * p / 100))]

    alln = sum(s["n"] for s in stats.values())
    allok = sum(s["ok"] for s in stats.values())
    print(f"总请求 {alln}  时长 {args.duration}s  HTTP并发 {args.workers}  agent并发 {args.agent_workers}")
    print()
    print(f"{'目标':<24}{'请求':>7}{'成功率':>9}{'p50':>9}{'p95':>9}  错误分布")
    print("-" * 96)
    for name in sorted(stats):
        s = stats[name]
        rate = 100.0 * s["ok"] / max(s["n"], 1)
        e = ", ".join(f"{k}:{v}" for k, v in s["err"].most_common(3)) or "-"
        print(f"{name:<24}{s['n']:>7}{rate:>8.1f}%{pct(s['lat'],50):>8.0f}ms"
              f"{pct(s['lat'],95):>8.0f}ms  {e}")
    print("-" * 96)
    print(f"{'合计':<24}{alln:>7}{100.0*allok/max(alln,1):>8.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

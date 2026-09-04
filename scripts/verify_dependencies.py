#!/usr/bin/env python3
"""阶段 C：用 Chaos Mesh 逐条验证图数据库里的依赖边。

## 判据

对图里的一条边 `A -[依赖]-> B`：**注入故障让 B 不可用，然后看 A 是否退化。**
- A 退化了  → 边成立，标 **confirmed**
- A 没退化  → 边可疑，标 **refuted**（图声称的依赖在现实中不存在，或不是硬依赖）
- 拿不到判断依据 → 标 **unverifiable** 并记原因，**继续下一条，绝不卡住**

「拿不到依据」和「没退化」是**两件不同的事** —— 这个区分是整个框架的核心，
混起来会把「我测不了」记成「依赖不存在」，那比不测更糟。

## 为什么用 Chaos Mesh 而不是 NetworkPolicy 或 FIS

- **NetworkPolicy 走不通**：实测 VPC CNI 的 `--enable-network-policy=false`，
  策略写进去也不会被执行 —— 那会让每条边都「看起来没退化」，得出全盘 refuted 的假结论。
- **FIS** 账号里已有 33 个模板，但它的粒度在基础设施层（节点/子网/RDS），
  要验证「服务 A 对服务 B 的依赖」需要 pod 级精度。
- **Chaos Mesh** 已装（188 天，3 controller + 4 daemon 全健康）且有 `duration` 字段 ——
  **到期自动恢复**，满足硬约束「故障注入必须可自动恢复」。
  即使本脚本中途崩掉，故障也会自己解除。

## 硬约束遵守

- 每个实验都带 `duration`，**绝不创建无限期故障**
- 结束时无论成败都删除实验对象（双保险）
- 只动 `petadoptions` 命名空间里的应用 Pod，不碰 Neptune / Aurora / ALB
- 对 **agent 边**用 HTTP 层故障而非杀 Pod：AgentCore Runtime 是托管的，
  杀不了它的 Pod（它根本不在这个集群里）—— 见 AGENT_EDGE_NOTE
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict

KUBECTL = "/home/ec2-user/bin/kubectl"
NS = "petadoptions"
ALB = "internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com"

# 图里的服务名 -> k8s 里的 label selector。
# ⚠️ 两套命名不同，直接拿图里的名字去 kubectl 查会全部找不到 ——
#    这个坑在 Stage 6 早期踩过一次（报「14 个全不匹配」）。
GRAPH_TO_K8S = {
    "petsite": "petsite",
    "petsearch": "search-service",
    "petlistadoptions": "list-adoptions",
    "payforadoption": "pay-for-adoption",
    "pethistory": "pethistory",
    "petfood": "petfood",
    # petstatusupdater 是 Lambda，不在集群里 —— 标 unverifiable
}

# 每个服务的「是否健康」探测方式：经 internal ALB 打它的对外路径。
# petsite 是入口，其余通过 petsite 的页面间接观测（也更贴近真实依赖形态）。
PROBE = {
    "petsite": f"http://{ALB}/",
    "petsearch": f"http://{ALB}:8081/api/search?pettype=puppy",
    "pethistory": f"http://{ALB}/pethistory",
    "petfood": f"http://{ALB}/petfood",
}

AGENT_EDGE_NOTE = (
    "AgentCore Runtime 是 AWS 托管的，Pod 不在本集群里，Chaos Mesh 够不到。"
    "要验证 agent 边只能从它依赖的下游入手（断 petsearch 看 adoption agent 是否退化），"
    "或用 Guardrail / 改 SSM 参数指向坏地址等应用层手段。"
)


@dataclass
class Verdict:
    edge_type: str
    src: str
    dst: str
    status: str          # confirmed | refuted | unverifiable
    reason: str
    baseline: dict = field(default_factory=dict)
    during: dict = field(default_factory=dict)


def sh(cmd: str, timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def probe(name: str, n: int = 6) -> dict:
    """探测一个服务的健康度。返回 {ok, total, codes}。

    ⚠️ 用多次采样而不是单次 —— 单次请求命中 1% 故障注入就会误判成「退化」。
    """
    url = PROBE.get(name)
    if not url:
        return {"ok": 0, "total": 0, "codes": {}, "note": "无探测路径"}
    codes: dict = {}
    ok = 0
    for _ in range(n):
        rc, out = sh(f'curl -sSL -o /dev/null -w "%{{http_code}}" --max-time 12 "{url}"', timeout=20)
        code = out.strip()[-3:] if out.strip() else "ERR"
        codes[code] = codes.get(code, 0) + 1
        if code == "200":
            ok += 1
        time.sleep(0.3)
    return {"ok": ok, "total": n, "codes": codes}


def apply_pod_kill(target_k8s: str, duration: str, name: str) -> tuple[bool, str]:
    """用 PodChaos 让目标服务的**全部** Pod 不可用。

    选 pod-failure 而不是 pod-kill：pod-kill 杀掉后 Deployment 会立刻拉起新的，
    故障窗口太短、观测不到；pod-failure 会把 Pod 换成一个不可用的占位镜像并**在
    duration 到期后自动还原**，故障窗口可控。
    """
    manifest = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": {"name": name, "namespace": NS},
        "spec": {
            "action": "pod-failure",
            "mode": "all",
            "duration": duration,   # ⚠️ 必须有：到期自动恢复
            "selector": {"namespaces": [NS], "labelSelectors": {"app": target_k8s}},
        },
    }
    rc, out = sh(f"{KUBECTL} apply -f - <<'EOF'\n{json.dumps(manifest)}\nEOF", timeout=60)
    return rc == 0, out.strip()[:200]


def cleanup(name: str) -> None:
    """双保险：即使 duration 已到期，也显式删掉实验对象。"""
    sh(f"{KUBECTL} delete podchaos {name} -n {NS} --ignore-not-found=true", timeout=60)


def verify_edge(src: str, dst: str, edge_type: str, settle: int = 25) -> Verdict:
    """验证 src 依赖 dst：断 dst，看 src 是否退化。"""
    if edge_type in ("Delegates", "InvokesTool", "Retrieves"):
        return Verdict(edge_type, src, dst, "unverifiable", AGENT_EDGE_NOTE)

    dst_k8s = GRAPH_TO_K8S.get(dst)
    if not dst_k8s:
        return Verdict(edge_type, src, dst, "unverifiable",
                       f"下游 {dst} 不是本集群里的 Deployment（可能是 Lambda / ECR / SQS 等）")
    if src not in PROBE:
        return Verdict(edge_type, src, dst, "unverifiable",
                       f"上游 {src} 没有可用的健康探测路径")

    base = probe(src)
    if base["total"] == 0 or base["ok"] == 0:
        return Verdict(edge_type, src, dst, "unverifiable",
                       f"注入前上游 {src} 本身就不健康（{base['codes']}），基线不可用",
                       baseline=base)

    exp = f"verify-{edge_type.lower()}-{src}-{dst}".replace("_", "-")[:60]
    cleanup(exp)
    okc, msg = apply_pod_kill(dst_k8s, "90s", exp)
    if not okc:
        cleanup(exp)
        return Verdict(edge_type, src, dst, "unverifiable",
                       f"Chaos Mesh 实验创建失败: {msg}", baseline=base)
    try:
        time.sleep(settle)
        during = probe(src)
    finally:
        cleanup(exp)

    base_rate = base["ok"] / base["total"]
    during_rate = during["ok"] / max(during["total"], 1)
    # 退化判据：成功率掉到基线的一半以下。
    # 不用「有任何非 200」当判据 —— 1% 故障注入本身就会产生零星错误。
    if during_rate < base_rate * 0.5:
        return Verdict(edge_type, src, dst, "confirmed",
                       f"断开 {dst} 后 {src} 成功率 {base_rate:.0%} -> {during_rate:.0%}",
                       baseline=base, during=during)
    return Verdict(edge_type, src, dst, "refuted",
                   f"断开 {dst} 后 {src} 成功率仍为 {during_rate:.0%}（基线 {base_rate:.0%}）"
                   f"—— 图声称的依赖不成立或不是硬依赖",
                   baseline=base, during=during)


def main() -> int:
    edges = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else []
    if not edges:
        print("用法: verify_dependencies.py edges.json")
        return 2
    results: list[Verdict] = []
    for e in edges:
        src, dst, typ = e["src"], e["dst"], e["type"]
        print(f"\n─── 验证 {typ}: {src} -> {dst} ───", flush=True)
        try:
            v = verify_edge(src, dst, typ)
        except Exception as exc:  # noqa: BLE001
            v = Verdict(typ, src, dst, "unverifiable", f"验证过程抛异常: {exc!r}")
        results.append(v)
        print(f"    [{v.status}] {v.reason}", flush=True)

    print("\n" + "=" * 90)
    by = {}
    for v in results:
        by.setdefault(v.status, []).append(v)
    for st in ("confirmed", "refuted", "unverifiable"):
        vs = by.get(st, [])
        print(f"{st}: {len(vs)}")
        for v in vs:
            print(f"  {v.edge_type:12s} {v.src:22s} -> {v.dst:26s} {v.reason[:70]}")
    out = "/tmp/dependency-verdicts.json"
    with open(out, "w") as f:
        json.dump([asdict(v) for v in results], f, ensure_ascii=False, indent=2)
    print(f"\n判定已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

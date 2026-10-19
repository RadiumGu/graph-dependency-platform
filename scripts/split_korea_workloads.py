#!/usr/bin/env python3.11
"""split_korea_workloads.py — 把 15-korea-workloads.yaml 拆成每个服务一个文件。

    python3.11 scripts/split_korea_workloads.py            # 写 infra/dr-korea/workloads/
    python3.11 scripts/split_korea_workloads.py --check    # 只核对，不一致就返回 1（CI 用）

## 为什么拆（2026-10-18）

切换计划要求「每个原子步骤都要人确认」。原来一条 `kubectl apply -f 15-korea-workloads.yaml`
同时起 6 个服务：其中一个起不来时，只能整体回滚，也说不清是哪一个没过。拆开后切换计划
里每个服务一步：单独 apply、单独核实（Deployment 可用副本 + 有目标组的再核目标组健康）、
单独 delete 回滚。

## 为什么 15 号文件仍然保留

它是 `gen_korea_workloads.py` 从东京活集群生成的**权威产物**，test_101/103/104/107/118 与
18-korea-addons 的扫描都读它。拆分文件是它的**派生物**，按原文逐字切出来（不重新序列化，
不改任何一个字节），`--check` 与 test_137 保证两边不漂移：改了 15 号文件不重跑本脚本，CI 就红。

## 分组规则

- 每个 Deployment 一个文件，带上它的 ServiceAccount（spec.template.spec.serviceAccountName）
  和选中它的 Service（spec.selector ⊆ Pod 模板标签）
- 其余对象（petsite-sa、LB controller 的 SA、共享的 otel-config）进 `00-shared.yaml`，
  切换时**第一个**部署 —— petsite-korea-drill.yaml 用的 petsite-sa 也在这里
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
OUT = ROOT / "infra" / "dr-korea" / "workloads"
SHARED = "00-shared.yaml"


def split_raw(text: str) -> tuple[str, list[str]]:
    """(文件头注释, [每个文档的原文])。按行首 `---` 切，保留原文。"""
    parts: list[list[str]] = [[]]
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == "---":
            parts.append([])
        else:
            parts[-1].append(line)
    head = "".join(parts[0])
    docs = ["".join(p) for p in parts[1:] if "".join(p).strip()]
    return head, docs


def service_key(deploy_name: str) -> str:
    return deploy_name.removesuffix("-deployment")


def plan_split(text: str) -> dict[str, list[str]]:
    """{文件名: [文档原文...]}，顺序与 15 号文件一致。"""
    _, docs = split_raw(text)
    parsed = [yaml.safe_load(d) for d in docs]
    owner: dict[int, str] = {}
    for i, d in enumerate(parsed):
        if d.get("kind") != "Deployment":
            continue
        key = service_key(d["metadata"]["name"])
        owner[i] = key
        tpl = d["spec"]["template"]
        sa = tpl["spec"].get("serviceAccountName")
        labels = (tpl.get("metadata") or {}).get("labels") or {}
        for j, e in enumerate(parsed):
            if e.get("kind") == "ServiceAccount" and e["metadata"]["name"] == sa:
                owner[j] = key
            if e.get("kind") == "Service":
                sel = (e.get("spec") or {}).get("selector") or {}
                if sel and all(labels.get(k) == v for k, v in sel.items()):
                    owner[j] = key
    out: dict[str, list[str]] = {SHARED: []}
    for i, raw in enumerate(docs):
        name = f"{owner[i]}.yaml" if i in owner else SHARED
        out.setdefault(name, []).append(raw)
    return out


def render(name: str, docs: list[str]) -> str:
    what = "共享对象（先于各服务部署）" if name == SHARED else f"服务 {name[:-5]}"
    head = (
        f"# {what} —— 由 scripts/split_korea_workloads.py 从 15-korea-workloads.yaml **逐字**切出。\n"
        "# ⚠️ 不要手改：改 15 号文件（它本身由 scripts/gen_korea_workloads.py 生成），然后重跑拆分脚本。\n"
        "# 切换计划里每个服务一步：单独 apply、单独核实、单独 delete 回滚。\n"
    )
    return head + "".join("---\n" + d for d in docs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只核对已提交的拆分文件是否与 15 号文件一致")
    a = ap.parse_args()
    want = {n: render(n, d) for n, d in plan_split(SRC.read_text(encoding="utf-8")).items()}
    if a.check:
        have = {p.name for p in OUT.glob("*.yaml")} if OUT.exists() else set()
        bad = sorted(set(want) ^ have) + sorted(
            n for n in want if n in have and (OUT / n).read_text(encoding="utf-8") != want[n])
        if bad:
            print("拆分文件与 15-korea-workloads.yaml 不一致：" + ", ".join(bad)
                  + "\n重跑：python3.11 scripts/split_korea_workloads.py", file=sys.stderr)
            return 1
        print(f"一致：{len(want)} 个文件")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.yaml"):
        if old.name not in want:
            old.unlink()
    for n, body in want.items():
        (OUT / n).write_text(body, encoding="utf-8")
    print(f"已写 {len(want)} 个文件到 {OUT.relative_to(ROOT)}/：{', '.join(sorted(want))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

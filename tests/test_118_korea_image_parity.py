"""test_118_korea_image_parity.py — 首尔两侧不能跑与东京不同版本的代码。

## 守什么

2026-09-26 的教训不是「有个 bug」，而是**「已合并 ≠ 已部署」这类交付缺口
在灾备站点上会变成「切过去时一切正常，只有数据不对」**。

具体形态：东京把 petsite 与 search-service 换成含修复的镜像，而首尔清单
仍指向复制过来的**旧 asset 镜像**。切换时不会报错、不会 CrashLoop、
页面照常渲染 —— 只有行为退回缺陷版本。这是灾备最难发现的一类不一致。

`test_98` 只守 `petsite-korea-drill.yaml`，不守 `15-korea-workloads.yaml`。
所以在把 search-service 钉到热修镜像之后，如果不补这个门禁，
没有任何东西阻止它漂移回去。

## 为什么要求 digest 而不是 tag

灾备切换时唯一能证明「两侧跑的是同一份代码」的东西是 digest。
tag 可以被重新指向（`:latest` 是最坏的例子，但任何 tag 都可以被 re-push），
而 digest 是内容哈希 —— 同一个 digest 才是同一个东西。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
REPL = ROOT / "infra" / "dr-korea" / "09-ecr-replication.yaml"

# 东京线上正在跑的 digest（2026-09-27 核实）。两侧必须逐字相同。
TOKYO_LIVE = {
    "search-service": "sha256:309a9797ed509683ec06920d7eca9cd247e973a4a6849143d0f1f24cb8b18b10",
}


def _containers(path: Path):
    """把多文档 YAML 里所有 Deployment 的业务容器摊平成 (名字, image)。"""
    out = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"].get("containers", []):
            out.append((c["name"], c.get("image", "")))
    return out


@pytest.fixture(scope="module")
def containers():
    return _containers(WORKLOADS)


class TestHotfixedServicesArePinnedToTokyoDigest:
    def test_search_service_matches_tokyo(self, containers):
        imgs = [img for name, img in containers if name == "search-service"]
        assert imgs, "15-korea-workloads.yaml 里找不到 search-service 容器"
        for img in imgs:
            assert TOKYO_LIVE["search-service"] in img, (
                f"首尔 search-service 镜像是 {img}，与东京线上 digest 不一致 —— "
                "灾备切过去会退回不含「残缺记录容错」的版本，而且不会报错"
            )

    def test_pinned_by_digest_not_tag(self, containers):
        """热修镜像必须按 digest 钉死；tag 在灾备场景下证明不了同版本。"""
        for name, img in containers:
            if "-hotfix" in img:
                assert "@sha256:" in img, (
                    f"{name} 用的是热修仓库但按 tag 引用（{img}）—— "
                    "tag 可以被重新指向，只有 digest 能证明两侧是同一份代码"
                )

    def test_images_come_from_korea_registry(self, containers):
        """所有业务镜像必须从首尔 ECR 拉 —— 跨区拉会在东京不可用时一起挂掉。"""
        for name, img in containers:
            if img.startswith("public.ecr.aws/"):
                continue  # sidecar（otel collector）走公共仓库，可接受
            assert "ap-northeast-2" in img.split("/")[0], (
                f"{name} 的镜像 {img} 不是从首尔 registry 拉的 —— "
                "灾备站点不能依赖主站点所在区域的 registry"
            )


class TestReplicationCoversWhatTheManifestsUse:
    """清单引用了某个热修仓库，复制配置里就必须有它 —— 否则镜像根本不会到首尔。"""

    def test_every_hotfix_repo_is_replicated(self, containers):
        repl = REPL.read_text(encoding="utf-8")
        used = set()
        for _, img in containers:
            if "-hotfix" in img:
                # 926…ecr.ap-northeast-2.amazonaws.com/<repo>@sha256:…
                used.add(img.split("/", 1)[1].split("@")[0].split(":")[0])
        assert used, "清单里没有任何热修镜像 —— 这个断言本身失去了意义，需要复查"
        for repo in sorted(used):
            assert repo in repl, (
                f"清单用了 {repo}，但 09-ecr-replication.yaml 的过滤器里没有它 —— "
                "镜像不会被复制到首尔，切过去会 ImagePullBackOff"
            )

    def test_replication_backfill_caveat_is_recorded(self):
        """复制不追溯存量 —— 这条踩过两次，必须留在模板里。"""
        assert "不追溯存量" in REPL.read_text(encoding="utf-8") or \
               "追溯" in REPL.read_text(encoding="utf-8")

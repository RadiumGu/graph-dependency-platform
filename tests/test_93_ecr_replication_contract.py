"""
test_93_ecr_replication_contract.py — ECR 跨 region 复制的契约。

## 守的是什么

镜像仓库和要灾备的那个 region 是同一个 region,这是 petsite 灾备方案里最致命的
一个洞:东京挂了就拉不到镜像,韩国节点扩起来了也全是 ImagePullBackOff。

而这个功能有一个**极易误判**的地方,官方文档
(`AmazonECR/latest/userguide/replication.html`)原文:

    "Only repository content pushed or restored to a repository after
     replication is configured is replicated. **Any preexisting content in a
     repository isn't replicated.**"

2026-09-25 实测印证:对已有镜像调 `describe-image-replication-status`,
`replicationStatuses` 返回**空数组**。

所以「配完复制」和「镜像在灾备侧」是两件事。这个文件防的就是有人把复制栈
当成完整方案 —— 判据是「模板里有没有把这件事说清楚」,因为回填是一次性操作,
没法用模板结构断言。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPL = ROOT / "infra" / "dr-korea" / "09-ecr-replication.yaml"
REPOS = ROOT / "infra" / "dr-korea" / "10-ecr-korea-repos.yaml"


def _load(path: Path) -> dict:
    class _L(yaml.SafeLoader):
        pass

    for tag in ("!Sub", "!Ref", "!GetAtt", "!Join", "!Select", "!Split"):
        _L.add_constructor(tag, lambda loader, node: loader.construct_scalar(node))
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_L)


@pytest.fixture(scope="module")
def repl() -> dict:
    return _load(REPL)


@pytest.fixture(scope="module")
def repos() -> dict:
    return _load(REPOS)


class TestReplicationRuleShape:
    def test_resource_type_is_registry_level(self, repl: dict):
        types = [r["Type"] for r in repl["Resources"].values()]
        assert "AWS::ECR::ReplicationConfiguration" in types

    def test_filter_type_is_the_only_valid_enum(self, repl: dict):
        """`FilterType` 的枚举只有 PREFIX_MATCH —— 查 CFN schema 得到的。"""
        text = REPL.read_text(encoding="utf-8")
        # 照抄模板里的真实写法：`FilterType: PREFIX_MATCH`
        assert "FilterType: PREFIX_MATCH" in text
        for wrong in ("SUFFIX_MATCH", "EXACT_MATCH", "WILDCARD"):
            assert wrong not in text, f"{wrong} 不是合法枚举值"

    def test_destination_has_registry_id(self, repl: dict):
        """`RegistryId` 在 schema 里是**必填**，不是可选。"""
        for r in repl["Resources"].values():
            if r["Type"] != "AWS::ECR::ReplicationConfiguration":
                continue
            rules = r["Properties"]["ReplicationConfiguration"]["Rules"]
            for rule in rules:
                for dest in rule["Destinations"]:
                    assert "Region" in dest
                    assert "RegistryId" in dest, (
                        "ReplicationDestination 的 RegistryId 是必填 —— "
                        "同账号复制时填自己的账号 ID"
                    )

    def test_filters_are_scoped_not_whole_registry(self, repl: dict):
        """刻意只复制 petsite 切换用得到的仓库。

        那 11 个仓库里有 waggle-ai-*、translator-ws、yace-cloudwatch-exporter
        等切换用不到的东西，全量复制只是白花存储和跨 region 流量。
        """
        for r in repl["Resources"].values():
            if r["Type"] != "AWS::ECR::ReplicationConfiguration":
                continue
            for rule in r["Properties"]["ReplicationConfiguration"]["Rules"]:
                filters = rule.get("RepositoryFilters")
                assert filters, (
                    "没有 RepositoryFilters 等于复制整个 registry —— "
                    "要显式选出 petsite 切换需要的仓库"
                )


class TestBackfillIsNotForgotten:
    def test_template_documents_that_existing_images_are_not_replicated(self):
        """模板必须写明「已有镜像不会被复制」。

        这是判据里唯一能守住的形式 —— 回填是一次性操作，没有模板结构可断言。
        少了这句话，下一个人会把这个栈当成完整方案。
        """
        text = REPL.read_text(encoding="utf-8")
        assert "preexisting" in text, (
            "模板里要引用官方文档原文 —— 转述容易把「不会复制」说软"
        )
        assert "回填" in text, "要指明还需要一次性回填"
        assert "4.17" in text, "要指向 deployment-record 里记了回填过程的那一节"

    def test_korea_repos_exist_for_backfill(self, repos: dict):
        """ECR **不会**在 push 时自动建仓库，回填前必须先有仓库。"""
        types = [r["Type"] for r in repos["Resources"].values()]
        assert types.count("AWS::ECR::Repository") >= 2

    def test_korea_repo_names_match_source(self, repos: dict):
        """复制不支持改名 —— 韩国侧仓库名必须与东京一致。

        源仓库是 CDK bootstrap 建的
        `cdk-hnb659fds-container-assets-<acct>-ap-northeast-1`，
        那串 `ap-northeast-1` 是**名字的一部分**，不是 region 参数。
        改掉它会让复制过来的镜像和回填的镜像落进两个不同仓库。
        """
        params = repos.get("Parameters", {})
        defaults = " ".join(str(p.get("Default", "")) for p in params.values())
        assert "ap-northeast-1" in defaults, (
            "CDK asset 仓库名里必须保留 ap-northeast-1 —— "
            "看着别扭，但改名会让复制失效"
        )

    def test_backfilled_images_survive_stack_deletion(self, repos: dict):
        """仓库要 Retain —— 回填的镜像是灾备能否起来的前提。"""
        for name, r in repos["Resources"].items():
            if r["Type"] != "AWS::ECR::Repository":
                continue
            assert r.get("DeletionPolicy") == "Retain", (
                f"{name} 没有 DeletionPolicy: Retain —— "
                "删栈时顺手删掉回填的镜像等于悄悄废掉灾备能力"
            )


class TestTagMutabilityMatchesHowImagesAreReferenced:
    def test_cdk_asset_repo_is_immutable(self, repos: dict):
        """内容哈希 tag 天然不冲突，IMMUTABLE 能挡住最难查的那种漂移。"""
        text = REPOS.read_text(encoding="utf-8")
        assert "ImageTagMutability: IMMUTABLE" in text

    def test_named_repo_is_mutable_with_reason(self, repos: dict):
        """pethistory 引用的是 `:latest`，IMMUTABLE 会让第二次推送直接失败。"""
        text = REPOS.read_text(encoding="utf-8")
        assert "ImageTagMutability: MUTABLE" in text
        # 这本身是个灾备隐患（可变 tag 两侧可能指向不同镜像），要留下记录。
        assert "latest 是可变 tag" in text, (
            "用 MUTABLE 就要写明它带来的风险，否则下一个人不知道这是权衡"
        )

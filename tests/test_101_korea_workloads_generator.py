"""
test_101_korea_workloads_generator.py — 工作负载生成器的契约。

## 这个文件守 2026-09-25 在我自己的生成器里找到的三个缺陷

三个都有同一个形状:**我拷了契约的一半**，而失效表现指不到真因。

### ① ServiceAccount 对象没建 —— IRSA 只验了 IAM 那一侧

    ReplicaFailure | FailedCreate |
      pods "list-adoptions-…" is forbidden:
      serviceaccount "list-adoptions-sa" not found

**pod 根本不会被创建**，所以 `kubectl get pod` 一个异常都看不到 ——
任何遍历 pod 的健康检查对它完全失明。

修法:SA 对象与 IRSA 信任策略**同源**（都出自 irsa-korea-mapping.json）。

### ② 白名单拷贝丢了 enableServiceLinks

petfood CrashLoopBackOff，报错说 `port` 期望整数却得到
`tcp://172.20.21.75:80` —— 那是 k8s 的 service-link 环境变量，
因为 namespace 里有名为 `petfood` 的 Service，而应用配置前缀正好是 `PETFOOD_`。

**报错指不到「你丢了 enableServiceLinks」。**

修法:改用**黑名单** —— 深拷整个 pod spec，只去掉明确有害的字段。
白名单的问题是「忘了的字段静默消失」。

### ③ 拷了卷，没拷卷引用的 ConfigMap

    FailedMount: configmap "otel-config" not found

pod 永远 `ContainerCreating`，而原因**只在 pod 事件里** ——
容器状态和 Deployment conditions 都不提。

修法:扫描 volumes / envFrom / env.valueFrom，把 ConfigMap 搬过来；
Secret **只列名字不拷内容**。

## 一条贯穿的教训

②③ 都只让 6 个里的 1 个出问题（petfood / pethistory），
另外几个照样 Running —— **「其它几个起来了」完全不能说明这一个也会起来。**
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml

from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "scripts" / "gen_korea_workloads.py"
OUT = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
MAPPING = ROOT / "infra" / "dr-korea" / "irsa-korea-mapping.json"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(OUT.read_text(encoding="utf-8")) if d]


@pytest.fixture(scope="module")
def gen_src() -> str:
    return GEN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gen_tree(gen_src: str) -> ast.Module:
    return ast.parse(gen_src)


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


class TestServiceAccountsComeFromTheIrsaMapping:
    def test_every_mapped_sa_has_an_object(self, docs: list[dict]):
        """映射里的每一项都必须有对应的 ServiceAccount 对象。

        少了的表现是 ReplicaSet FailedCreate、**pod 根本不被创建** ——
        遍历 pod 的检查看不见。
        """
        mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
        want = {(e["namespace"], e["name"]) for e in mapping["service_accounts"]}
        have = {
            (d["metadata"]["namespace"], d["metadata"]["name"])
            for d in _by_kind(docs, "ServiceAccount")
        }
        assert want <= have, f"映射里有但清单里没有的 SA:{want - have}"

    def test_sa_annotation_matches_mapping_role(self, docs: list[dict]):
        """注解里的角色必须与映射一致 —— 少了注解的表现是每次调用 403。"""
        mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
        by_name = {(e["namespace"], e["name"]): e["role"] for e in mapping["service_accounts"]}
        for sa in _by_kind(docs, "ServiceAccount"):
            key = (sa["metadata"]["namespace"], sa["metadata"]["name"])
            arn = (sa["metadata"].get("annotations") or {}).get(
                "eks.amazonaws.com/role-arn", ""
            )
            assert arn, f"{key} 没有 role-arn 注解"
            assert arn.rsplit("/", 1)[-1] == by_name[key], f"{key} 的角色与映射不一致"

    def test_service_accounts_come_before_deployments(self, docs: list[dict]):
        """kubectl apply 按文件顺序 —— SA 必须在 Deployment 之前。"""
        first_dep = next(i for i, d in enumerate(docs) if d["kind"] == "Deployment")
        last_sa = max(i for i, d in enumerate(docs) if d["kind"] == "ServiceAccount")
        assert last_sa < first_dep, "有 ServiceAccount 排在 Deployment 之后"

    def test_generator_actually_calls_the_mapping_reader(self, gen_tree: ast.Module, gen_src: str):
        """判据查**调用**，不查字符串出现。

        ⚠️ 第一版写的是 `assert "service_accounts_from_mapping" in gen_src`。
        反向验证时我把 `objs = service_accounts_from_mapping()` 改成 `objs = []`
        —— 函数定义还在，字符串还在，**门禁挂靶 0**。
        「函数存在」和「函数被调用」是两件事，而只有后者是要守的性质。
        """
        assert "irsa-korea-mapping.json" in gen_src

        main_fn = next(
            n for n in ast.walk(gen_tree)
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        called = {
            n.func.id
            for n in ast.walk(main_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "service_accounts_from_mapping" in called, (
            "main() 里没有调用 service_accounts_from_mapping —— "
            "SA 对象与 IRSA 信任策略就不再同源了"
        )

    def test_mapping_reader_returns_every_mapped_sa(self):
        """**行为**测试:直接调那个函数，看它真的产出全部 SA。

        这条比上面那条「有没有被调用」强:它验的是函数做对了事，
        而且不需要 AWS（只读本地映射文件），离线 CI 里真的执行。
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location("genmod_sa", GEN)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        got = mod.service_accounts_from_mapping()
        mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
        want = {(e["namespace"], e["name"]) for e in mapping["service_accounts"]}
        have = {(o["metadata"]["namespace"], o["metadata"]["name"]) for o in got}
        assert have == want, f"少了 {want - have}，多了 {have - want}"
        for o in got:
            assert o["kind"] == "ServiceAccount"
            assert (o["metadata"]["annotations"] or {}).get(
                "eks.amazonaws.com/role-arn"
            ), f"{o['metadata']['name']} 没有 role-arn 注解"

    def test_known_limitation_of_source_inspection(self, gen_src: str):
        """⚠️ 这一组门禁有一个**证明不到的边界**，写在这里免得被误以为严密。

        反向验证时我把 `objs = service_accounts_from_mapping()` 改成 `objs = []`，
        门禁**挂靶 0** —— 因为 `main()` 里另有两处调用同一个函数（算切片位置），
        所以「被调用」这个判据仍然成立。

        结论:**源码检查证明不了「生成的清单里真有 SA」**。
        真正兜住这件事的是两条别的门禁:
          - `test_mapping_reader_returns_every_mapped_sa`（函数行为，离线可跑）
          - `test_every_mapped_sa_has_an_object`（产出文件的内容）
        后者在清单被重新生成时会立刻发现回归 —— 这就是这组门禁的实际边界。

        这条测试自己只做一件事:确保那段说明还在，别让后人以为源码检查是严密的。
        """
        # 照抄生成器里的真实文字（第 223 行的 docstring 标题）
        assert_contains(gen_src, "为什么必须与信任策略同源")

    def test_generator_actually_calls_the_config_scanner(self, gen_tree: ast.Module):
        """同理:referenced_configs 必须真被调用。"""
        main_fn = next(
            n for n in ast.walk(gen_tree)
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        called = {
            n.func.id
            for n in ast.walk(main_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "referenced_configs" in called, (
            "main() 里没有调用 referenced_configs —— 卷引用的 ConfigMap 不会被搬过来"
        )


class TestPodSpecIsCopiedByDenylistNotAllowlist:
    def test_enable_service_links_is_preserved(self, docs: list[dict]):
        """东京显式设了 enableServiceLinks: False，必须带过来。

        丢了它 → k8s 注入 service-link 环境变量 → petfood 的 `PETFOOD_PORT`
        被写成 `tcp://…` → 配置解析失败 CrashLoopBackOff。
        而报错指不到这个字段。
        """
        deps = {d["metadata"]["name"]: d for d in _by_kind(docs, "Deployment")}
        # 这 5 个在东京是显式 False 的（实测 2026-09-25）
        for name in (
            "list-adoptions",
            "pay-for-adoption",
            "petfood",
            "search-service",
            "traffic-generator",
        ):
            ps = deps[name]["spec"]["template"]["spec"]
            assert ps.get("enableServiceLinks") is False, (
                f"{name} 丢了 enableServiceLinks=False —— "
                "会让 service-link 环境变量污染应用配置"
            )

    def test_generator_uses_deepcopy_of_whole_pod_spec(self, gen_src: str):
        """判据查**结构**:必须是深拷整个 pod spec，再 pop 掉有害字段。

        白名单式的「for k in (...) if tpl['spec'].get(k)」会静默丢字段。
        """
        assert 'pod_spec = json.loads(json.dumps(tpl["spec"]))' in gen_src, (
            "没有深拷整个 pod spec —— 白名单式拷贝会静默丢掉没想到的字段"
        )
        assert 'pod_spec.pop("nodeName", None)' in gen_src, (
            "没去掉 nodeName —— 会把 pod 钉在东京的节点上"
        )

    def test_node_name_is_never_carried_over(self, docs: list[dict]):
        for d in _by_kind(docs, "Deployment"):
            assert "nodeName" not in d["spec"]["template"]["spec"], (
                f"{d['metadata']['name']} 带了 nodeName"
            )


class TestReferencedConfigsAreCarriedOver:
    def test_every_referenced_configmap_is_included(self, docs: list[dict]):
        """卷/env 引用的 ConfigMap 必须在清单里。

        少了的表现是 pod 永远 ContainerCreating，
        而 FailedMount 只出现在**pod 事件**里。
        """
        present = {d["metadata"]["name"] for d in _by_kind(docs, "ConfigMap")}
        needed: set[str] = set()
        for d in _by_kind(docs, "Deployment"):
            spec = d["spec"]["template"]["spec"]
            for v in spec.get("volumes") or []:
                if (v.get("configMap") or {}).get("name"):
                    needed.add(v["configMap"]["name"])
            for c in spec.get("containers") or []:
                for ef in c.get("envFrom") or []:
                    if (ef.get("configMapRef") or {}).get("name"):
                        needed.add(ef["configMapRef"]["name"])
        needed.discard("kube-root-ca.crt")
        assert needed <= present, f"引用了但清单里没有的 ConfigMap:{needed - present}"

    def test_generator_scans_all_three_reference_sites(self, gen_src: str):
        """三处引用都要扫:volumes / envFrom / env.valueFrom。"""
        fn = next(
            n for n in ast.walk(ast.parse(gen_src))
            if isinstance(n, ast.FunctionDef) and n.name == "referenced_configs"
        )
        body = ast.get_source_segment(gen_src, fn) or ""
        for site in ("volumes", "envFrom", "valueFrom", "projected"):
            assert site in body, f"referenced_configs 没扫 {site}"

    def test_secrets_are_listed_not_copied(self, docs: list[dict], gen_src: str):
        """Secret **只列名字不拷内容** —— 不把密钥写进 git。"""
        assert not _by_kind(docs, "Secret"), (
            "生成的清单里出现了 Secret —— 密钥内容不该进仓库"
        )
        assert_contains(gen_src, "刻意不拷 Secret 的内容")


class TestGeneratedFileSaysItIsGenerated:
    def test_header_warns_not_to_hand_edit(self):
        text = OUT.read_text(encoding="utf-8")
        assert_contains(text, "这个文件是**生成的，不要手改**")
        assert_contains(text, "scripts/gen_korea_workloads.py")

    def test_header_records_source_and_timestamp(self):
        text = OUT.read_text(encoding="utf-8")
        assert_contains(text, "本次生成:")
        assert_contains(text, "活集群")

    def test_images_are_korea_registry(self, docs: list[dict]):
        for d in _by_kind(docs, "Deployment"):
            for c in d["spec"]["template"]["spec"]["containers"]:
                img = c["image"]
                host = img.split("/")[0]
                if ".dkr.ecr." not in host:
                    continue  # 公共镜像，每 region 可拉
                assert "ap-northeast-2" in host, (
                    f"{d['metadata']['name']}/{c['name']} 的私有镜像还指着东京:{img}"
                )

    def test_repo_name_keeps_tokyo_suffix(self, docs: list[dict]):
        """仓库**名**里仍带 ap-northeast-1 —— ECR 复制不支持改名。"""
        found = False
        for d in _by_kind(docs, "Deployment"):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if "cdk-hnb659fds-container-assets" in c["image"]:
                    repo = c["image"].split("/", 1)[1].rsplit(":", 1)[0]
                    assert repo.endswith("ap-northeast-1")
                    found = True
        assert found, "一个 CDK asset 镜像都没找到 —— 判据本身失效了"


class TestImagePrecheckDistinguishesPrivateFromPublic:
    def test_is_private_ecr_exists_and_is_used(self, gen_src: str):
        """第一版预检假设所有镜像都在私有 ECR，把公共镜像误报成缺失。"""
        assert "def is_private_ecr(" in gen_src
        assert_contains(gen_src, "假设所有镜像都在私有 ECR")

    def test_private_detection_logic(self):
        """直接测那个判断函数，别只看它存在。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location("genmod", GEN)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        assert mod.is_private_ecr(
            "926093770964.dkr.ecr.ap-northeast-2.amazonaws.com/foo:bar"
        )
        assert not mod.is_private_ecr(
            "public.ecr.aws/aws-observability/aws-otel-collector:v0.47.0"
        )
        assert not mod.is_private_ecr("nginx:latest")


class TestRecordKeepsTheThreeDefects:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_sa_defect_and_its_blindness(self, record: str):
        assert_contains(record, "我只验了 IRSA 的一半")
        assert_contains(record, "pod 根本不会被创建")
        assert_contains(record, "完全失明")

    def test_allowlist_defect(self, record: str):
        assert_contains(record, "白名单拷贝悄悄丢了 `enableServiceLinks`")
        assert_contains(record, "指不到「你丢了 enableServiceLinks」")

    def test_configmap_defect(self, record: str):
        assert_contains(record, "拷了卷，没拷卷引用的 ConfigMap")
        assert_contains(record, "只出现在 pod 事件里")

    def test_one_of_many_lesson(self, record: str):
        """「其它几个起来了」不能说明这一个也会起来 —— 这条必须留着。"""
        assert_contains(record, "完全不能说明这一个也会起来")

    def test_tokyo_fragility_noted(self, record: str):
        assert_contains(record, "东京的潜在脆弱点")

    def test_pethistory_root_cause_marked_undetermined(self, record: str):
        """pethistory 根因**未确定** —— 不许写成已确定。"""
        assert_contains(record, "根因**未确定**")
        assert_contains(record, "不声称已确定根因")

    def test_my_own_criterion_errors_recorded(self, record: str):
        assert_contains(record, "判据必须按就绪数，不按 phase")

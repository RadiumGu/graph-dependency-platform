"""
test_91_ci_gates_must_actually_run.py — 门禁必须在 CI 里真的执行。

## 这个文件守的是什么

2026-09-24 发现:**此前写的所有门禁在 CI 里一条都没跑过。**

根因在 `tests/conftest.py`:`cleanup_test_data` 是 `autouse=True` +
`scope='session'`,而它在**参数里**要 `neptune_rca`,后者有一句
`assert isinstance(result, list)`。pytest 会在第一个测试 setup 时急切求值它
→ 没有 Neptune 就等于整套测试在 setup 阶段全灭,连「读一个 YAML、
断言里面有某个字段」这种纯离线门禁也一样。

实测证据:

    main                   26 passed, 1263 skipped
    加了 12 条纯离线门禁后   26 passed, 1275 skipped   ← 通过数一个没涨

**「门禁存在」和「门禁在 CI 里拦得住」是两件事**,而前者极易被误当成后者 ——
本地有凭据时它们全绿,看起来毫无问题。

这个文件防的就是这三条退化路径。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = ROOT / "tests" / "conftest.py"
WORKFLOW = ROOT / ".github" / "workflows" / "migration-checks.yml"


@pytest.fixture(scope="module")
def conftest_src() -> str:
    return CONFTEST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


class TestAutouseFixturesMustNotRequireCloud:
    """⚠️ 这一组有一个**它自己抓不到的场景**，必须说清楚。

    如果 conftest 退化回 `cleanup_test_data(neptune_rca)`，那么在无 Neptune 的
    环境里**这一组自己也会被 skip** —— 它的 setup 同样依赖那个 fixture，
    而 offline 处理器把 setup ERROR 转成 skip。
    **门禁被它要抓的缺陷本身禁用了。**

    2026-09-24 反向验证时实测到:退化后这一组「0 个挂靶」,不是正则写错。

    所以真正的机械防线是 `GDP_OFFLINE_MIN_PASSED`:退化后通过数从 1069
    掉到 26，低于下限 1040 → pytest **退出码 1**（实测），CI 变红。

    这一组的价值在**有 Neptune 的开发机上**:那里 fixture 能解析成功，
    于是它能在提交前就指出参数写错了。两条防线针对的是不同环境。
    """

    def test_cleanup_fixture_does_not_take_neptune_param(self, conftest_src: str):
        m = re.search(
            r"@pytest\.fixture\([^)]*autouse=True[^)]*\)\s*\ndef cleanup_test_data\(([^)]*)\)",
            conftest_src,
        )
        assert m, "找不到 cleanup_test_data 的 autouse 定义"
        params = m.group(1).strip()
        # 空参数才安全。写上 neptune_rca 就等于让每个测试的 setup 都要活 Neptune。
        assert params == "", (
            f"cleanup_test_data 的参数是 `{params}` —— autouse+session 的 fixture "
            "在参数里要云资源，会让**每一个**测试的 setup 都依赖它，"
            "包括纯读文件的门禁。清理逻辑本来就有 try/except，"
            "把 client 解析挪到 yield 之后即可。"
        )

    def test_no_autouse_fixture_requires_a_cloud_fixture(self, conftest_src: str):
        # 泛化版：任何 autouse fixture 都不许在参数里拿这些云 fixture。
        cloud = ("neptune_rca", "neptune_dr")
        for m in re.finditer(
            r"@pytest\.fixture\(([^)]*autouse=True[^)]*)\)\s*\ndef (\w+)\(([^)]*)\)",
            conftest_src,
        ):
            name, params = m.group(2), m.group(3)
            for c in cloud:
                assert c not in params, (
                    f"autouse fixture `{name}` 在参数里要 `{c}`。"
                    "这会把整套测试的可运行性绑在一个活的云连接上。"
                )


class TestOfflineFloorIsMeaningful:
    def _offline_env(self, workflow: dict) -> dict:
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                env = step.get("env") or {}
                if "GDP_OFFLINE_MIN_PASSED" in env:
                    return env
        pytest.fail("workflow 里找不到设 GDP_OFFLINE_MIN_PASSED 的步骤")

    def test_floor_is_set(self, workflow: dict):
        env = self._offline_env(workflow)
        assert env.get("GDP_OFFLINE") == "1"

    def test_floor_is_not_token(self, workflow: dict):
        env = self._offline_env(workflow)
        floor = int(str(env["GDP_OFFLINE_MIN_PASSED"]))
        # ⚠️ 判据是「下限得与套件规模同量级」，不是「大于某个魔数」。
        #
        # 24 这个值曾经合理，但它配上「实测 26 通过」等于什么都不挡。
        # 全量约 1300 个测试里离线可跑的有 1069 个，下限低于 900 就说明
        # 要么大面积 skip 被接受了，要么 conftest 又把测试卡在 setup 上。
        assert floor >= 900, (
            f"离线通过下限是 {floor}，对一个 1069 个测试离线可跑的套件来说太松。"
            "下限是唯一能挡住「全部 skip 伪装成全部通过」的东西 —— "
            "它松下来的时候没有任何报错。"
        )


class TestCiInstallsAuthoritativeRequirements:
    def test_offline_job_installs_requirements_dev(self, workflow: dict):
        runs = [
            step.get("run", "")
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
        ]
        installs = [r for r in runs if "pip install" in r and "pytest" not in r.split("\n")[-1][:40] or "requirements-dev" in r]
        assert any("requirements-dev.txt" in r for r in runs), (
            "CI 应当装 requirements-dev.txt 而不是手列包名。"
            "手列必然漂移：此前有人从 CI 日志里逐个追加了 pydantic/structlog/"
            "streamlit/moto 四个，而 strands-agents 仍然缺着，"
            "造成 40 个 ModuleNotFoundError('strands')。"
        )

    def test_does_not_handlist_packages(self, workflow: dict):
        runs = [
            step.get("run", "")
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
        ]
        for r in runs:
            if "pip install" in r and "requirements" not in r:
                # 允许单独装一个工具（如 PyYAML 给 deadline 检查用），
                # 但不许再出现「一长串手列的测试依赖」。
                pkgs = re.findall(r"[a-z][a-z0-9\-]{2,}", r.split("pip install")[-1])
                assert len(pkgs) <= 2, (
                    f"这一行又在手列测试依赖：{r.strip()[:80]} —— "
                    "改成 -r requirements-dev.txt"
                )


class TestStaleTestIsGone:
    def test_no_test_imports_deleted_package(self):
        """没有测试再 import 已被项目删除的包。

        test_59 的 test_m05 原来 import `st_link_analysis`，而那个包
        2026-09-13 就被删了（缩放低于 0.625 时节点标签整体消失）。
        它一直没被发现，是因为开发机上还残留着卸载前装的副本，而 CI 里
        所有测试都卡在 setup 上从没真跑。
        """
        dead = ("st_link_analysis", "pyvis", "networkx")
        offenders = []
        for f in (ROOT / "tests").glob("test_*.py"):
            # ⚠️ 排除本文件自己：上面的注释里写了字面的
            # `importlib.import_module("st_link_analysis")` 当反例，
            # 不排除的话门禁会把自己举的例子判成违规（实测挂了一次）。
            # 这是「禁止某个模式」类门禁的通病 —— 它自己得能谈论那个模式。
            if f.name == Path(__file__).name:
                continue
            text = f.read_text(encoding="utf-8")
            for d in dead:
                # 只看真正的 import 语句，注释里提到它是允许的
                # （说明为什么不用某样东西是有价值的文档）。
                # ⚠️ 不要给 importlib 那一支加 `^\s*` 锚点：实际写法是
                # `mod = importlib.import_module("st_link_analysis")`，
                # 调用在赋值号右边，锚到行首就永远匹配不到。
                # 第一版就是这么写的，反向验证时没挂靶才发现。
                if re.search(rf"^\s*(?:import|from) {d}\b", text, re.MULTILINE) or re.search(
                    rf"importlib\.import_module\(['\"]{d}['\"]\)", text
                ):
                    offenders.append(f"{f.name} → {d}")
        assert not offenders, (
            f"这些测试仍在 import 已删除的包：{offenders}。"
            "守一个项目已决定不用的依赖，它的绿色是假的。"
        )

"""
test_104_korea_stepfn_and_env_sweep.py — 领养工作流 + 环境变量兜底检查的契约。

## 守的第一件事:环境变量白名单会漏，必须有兜底

2026-09-25 的教训。`REWRITE_ENV` 只列了 `AWS_REGION` 与 `S3_REGION`，
**漏了 `PETFOOD_REGION`** —— 于是 petfood 仍去查东京的表。

表现极具误导性:`/Checkout` 页面报

    Error fetching cart data for user: user00911
    HttpRequestException: 500 (Internal Server Error)

**报错指向 petsite，真因在 petfood。** 而 petfood 的 Deployment 是 1/1 就绪。

修法不是「把 PETFOOD_REGION 也加进白名单」（下一个漏的仍然看不见），
而是**兜底扫描**:凡是值里含 `ap-northeast-1` 的环境变量都报出来，
让人显式决定。加上之后立刻多抓出 5 处 otel 配置里的东京 region。

## 守的第二件事:Lambda 的层与 wrapper 必须成对

东京那 3 个函数带 `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`，
而 wrapper 来自 ADOT 层。**只带环境变量不带层，函数起不来**。
韩国**两个都不带** —— 这个决定必须留着理由，否则有人会「补上」环境变量。

## 守的第三件事:一次性数据复制的局限

DynamoDB 数据是一次性复制的，**不是持续同步**。东京改了数据韩国不会跟着变，
而且**看不出来**（表里有数据、查询能返回，只是旧的）。
正确答案是全局表，但需要改动东京生产表 —— 必须先问用户。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from cfn_yaml import load_cfn
from zh_text import assert_contains

ROOT = Path(__file__).resolve().parents[1]
STEPFN = ROOT / "infra" / "dr-korea" / "17-korea-stepfn.yaml"
GEN = ROOT / "scripts" / "gen_korea_workloads.py"
DDB_SYNC = ROOT / "scripts" / "sync_korea_ddb_items.py"
RECORD = ROOT / "docs" / "runbooks" / "deployment-record.md"


@pytest.fixture(scope="module")
def stack() -> dict:
    return load_cfn(STEPFN)


@pytest.fixture(scope="module")
def stack_text() -> str:
    return STEPFN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gen_src() -> str:
    return GEN.read_text(encoding="utf-8")


class TestEnvSweepCatchesWhatTheAllowlistMisses:
    def test_generator_has_a_catch_all_sweep(self, gen_src: str):
        """**本文件最重要的一条。**

        白名单只能挡住想到的名字。兜底扫描让漏项至少可见。
        """
        assert "TOKYO_REGION in str(e.get(\"value\") or \"\")" in gen_src, (
            "缺少兜底扫描 —— 白名单漏掉的环境变量会静默指向东京，"
            "而表现可能是完全不相干的服务报 500"
        )

    def test_sweep_runs_for_names_not_in_either_list(self, gen_src: str):
        """判据查**结构**:兜底必须只跳过已显式处理的名字。

        如果兜底也按名字白名单过滤，它就没有兜底作用了。
        """
        tree = ast.parse(gen_src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "fix_env"
        )
        body = ast.get_source_segment(gen_src, fn) or ""
        assert "name not in REWRITE_ENV" in body
        assert "name not in DROP_ENV_FOR_PARAM_STORE_FALLBACK" in body

    def test_lesson_is_recorded_in_the_generator(self, gen_src: str):
        assert_contains(gen_src, "漏了 PETFOOD_REGION")
        assert_contains(gen_src, "而那个报错指向 petsite，不是 petfood")

    def test_generated_manifest_has_no_unhandled_tokyo_env(self):
        """产出物核对:剩下的东京 region 环境变量必须是**已知且被记下**的。

        目前已知的只有 otel sidecar 的 AOT_CONFIG_CONTENT（观测配置，
        灾备 region 不需要）和 PETFOOD_REGION（待建韩国表后改写）。
        出现别的名字就说明有新的漏项。
        """
        import yaml

        out = ROOT / "infra" / "dr-korea" / "15-korea-workloads.yaml"
        docs = [d for d in yaml.safe_load_all(out.read_text(encoding="utf-8")) if d]
        known = {"AOT_CONFIG_CONTENT", "PETFOOD_REGION"}
        found: set[str] = set()
        for d in docs:
            if d["kind"] != "Deployment":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                for e in c.get("env") or []:
                    if "ap-northeast-1" in str(e.get("value") or ""):
                        found.add(e["name"])
        unexpected = found - known
        assert not unexpected, (
            f"出现了未记录的东京 region 环境变量:{unexpected} —— "
            "要么改写它，要么把它写进已知清单并说明为什么保留"
        )


class TestLambdaWrapperAndLayersAreHandledAsAPair:
    def test_no_exec_wrapper_without_layers(self, stack: dict):
        """只带 wrapper 不带层 = 函数起不来。"""
        for name, res in stack["Resources"].items():
            if res["Type"] != "AWS::Lambda::Function":
                continue
            props = res["Properties"]
            env = (props.get("Environment") or {}).get("Variables") or {}
            has_wrapper = "AWS_LAMBDA_EXEC_WRAPPER" in env
            has_layers = bool(props.get("Layers"))
            assert has_wrapper == has_layers, (
                f"{name}: wrapper={has_wrapper} layers={has_layers} —— "
                "两者必须同时有或同时没有"
            )

    def test_decision_is_documented(self, stack_text: str):
        assert_contains(stack_text, "只带环境变量不带层，函数会起不来")
        assert_contains(stack_text, "两个都不带")

    def test_runtime_and_arch_match_tokyo(self, stack: dict):
        """运行时与架构照东京实测:python3.12 / arm64。"""
        fns = [
            r for r in stack["Resources"].values()
            if r["Type"] == "AWS::Lambda::Function"
        ]
        assert len(fns) == 3, f"应当是 3 个 Lambda，实际 {len(fns)}"
        for f in fns:
            assert f["Properties"]["Runtime"] == "python3.12"
            assert f["Properties"]["Architectures"] == ["arm64"]


class TestStateMachineReferencesKoreaLambdas:
    def test_definition_has_no_tokyo_arn(self, stack_text: str):
        """定义里不许残留东京的函数 ARN —— 照抄会调到东京去。"""
        i = stack_text.index("DefinitionString")
        definition = stack_text[i:]
        assert "ap-northeast-1" not in definition, "状态机定义里还有东京的 ARN"
        assert "arn:aws:states:::lambda:invoke" in definition

    def test_state_machine_role_scoped_to_three_functions(self, stack: dict):
        """不用 `*` —— 只放行这三个函数。"""
        pol = stack["Resources"]["StateMachineRole"]["Properties"]["Policies"][0]
        st = pol["PolicyDocument"]["Statement"][0]
        res = st["Resource"]
        assert isinstance(res, list) and len(res) == 3, f"资源不是三个:{res}"
        assert st["Action"] == "lambda:InvokeFunction"

    def test_lambda_role_is_least_privilege(self, stack: dict):
        """Lambda 角色只需要读 SSM 参数 + 查那张表。"""
        role = stack["Resources"]["StepFnLambdaRole"]["Properties"]
        managed = role["ManagedPolicyArns"]
        assert managed == [
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        ], f"托管策略过多:{managed}"
        acts: list[str] = []
        for st in role["Policies"][0]["PolicyDocument"]["Statement"]:
            a = st["Action"]
            acts += [a] if isinstance(a, str) else a
        assert set(acts) == {"ssm:GetParameter", "dynamodb:Query", "dynamodb:GetItem"}

    def test_type_is_standard(self, stack: dict):
        assert stack["Resources"]["AdoptionStateMachine"]["Properties"][
            "StateMachineType"
        ] == "STANDARD"


class TestDataSyncKnowsItsLimits:
    def test_script_says_it_is_a_stopgap(self):
        src = DDB_SYNC.read_text(encoding="utf-8")
        assert_contains(src, "这是权宜之计，不是正确答案")
        assert_contains(src, "DynamoDB 全局表")

    def test_script_names_the_two_production_changes(self):
        """转全局表要改东京生产表的哪两处，必须写明 —— 那是要问用户的。"""
        src = DDB_SYNC.read_text(encoding="utf-8")
        assert_contains(src, "开启 DynamoDB Streams")
        assert_contains(src, "必须先问用户")

    def test_script_warns_stale_data_is_invisible(self):
        """最危险的局限:数据旧了看不出来。"""
        src = DDB_SYNC.read_text(encoding="utf-8")
        assert_contains(src, "看不出来")
        assert_contains(src, "返回的是旧的")

    def test_script_reverifies_after_write(self):
        """写完要重新扫两边比对，不能看 put-item 的返回。"""
        src = DDB_SYNC.read_text(encoding="utf-8")
        assert "src2, dst2 = scan(" in src


class TestRecordIsHonestAboutCheckout:
    @pytest.fixture(scope="class")
    def record(self) -> str:
        return RECORD.read_text(encoding="utf-8")

    def test_checkout_root_cause_was_elsewhere(self, record: str):
        """建了状态机不等于 Checkout 好了 —— 真因在 petfood。"""
        assert_contains(record, "真因原来不在 StepFunctions")
        assert_contains(record, "是 petfood 的购物车 API 返回 500")

    def test_remaining_petfood_resources_are_listed(self, record: str):
        assert_contains(record, "ddbpetfoodfoods")
        assert_contains(record, "ddbpetfoodcarts")
        assert_contains(record, "EventBridge 总线")

    def test_my_own_probe_error_recorded(self, record: str):
        """用错端口得到 connection refused —— 那是没测到，不是坏了。"""
        assert_contains(record, "那是**我测错了**")
        assert_contains(record, "不是「应用坏了」")

    def test_state_machine_proof_is_a_real_execution(self, record: str):
        assert_contains(record, "真跑一次状态机")
        assert_contains(record, "SUCCEEDED")

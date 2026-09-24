"""
test_84_worker_provisioning_in_iac.py — worker provisioning 的门禁。

守的核心判据:**worker 必须能从 IaC 重建出来**。

为什么:worker 最初是用 SSM 手工装的。重启能保留它,但实例真被重建时
它不会自动回来 —— 而 worker 不在时,切换 workflow 会一直排队并且
**看起来是 RUNNING**(实测过),不报任何错。那是最糟的失效形态:
你以为切换在进行,其实什么都没发生。

次要判据:provisioning 脚本必须幂等,否则它只能在重建时跑一次,
等于一段永远没被验证过的代码。
"""
from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dr-plan-generator" / "worker" / "provision-worker.sh"
TPL = ROOT / "infra" / "dr-korea" / "02-temporal.yaml"
PERMS = ROOT / "infra" / "dr-korea" / "07-worker-permissions.yaml"


@pytest.fixture(scope="module")
def script() -> str:
    assert SCRIPT.exists(), f"{SCRIPT} 不存在 —— worker provisioning 必须在仓库里"
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tpl() -> str:
    return TPL.read_text(encoding="utf-8")


class TestUserDataCallsProvisioning:
    """UserData 必须调用 provisioning —— 否则重建出来的实例没有 worker。"""

    def test_userdata_invokes_the_script(self, tpl: str):
        assert "provision-worker.sh" in tpl, (
            "UserData 没有调用 provision-worker.sh。重建出来的实例将没有 worker，"
            "而那时切换 workflow 会一直排队并看起来是 RUNNING，不报任何错。"
        )

    def test_runs_after_temporal_is_up(self, tpl: str):
        # 顺序要紧：脚本最后要查任务队列的 poller，那需要 Temporal 在应答。
        i_temporal = tpl.index("enable --now temporal.service")
        i_worker = tpl.index("provision-worker.sh")
        assert i_temporal < i_worker, (
            "provisioning 必须在 temporal.service 起来之后 —— "
            "它的核实步骤要查 Temporal 的 HTTP API"
        )

    def test_failure_does_not_abort_bootstrap(self, tpl: str):
        # worker 缺失可以事后补；让整个引导失败会让 CFN 回滚掉一台
        # 其实可用的 Temporal。
        seg = tpl[tpl.index("provision-worker.sh") :][:600]
        assert "|| true" in seg or "|| echo" in seg, (
            "provisioning 失败不该让整个引导失败"
        )

    def test_code_bucket_is_a_parameter(self, tpl: str):
        assert "WorkerCodeBucket" in tpl, "代码桶应当是参数而不是硬编码"


class TestScriptIsIdempotent:
    """幂等是它能被验证的前提。"""

    def test_python_install_is_guarded(self, script: str):
        # ⚠️ 断言要照抄脚本的真实写法。脚本用的是变量 "$PY"（值 python3.12），
        # 不是字面量 —— 第一版这条断言写成匹配 "python3.12" 就挂了，
        # 那是「按想象中的实现写判据」，本项目最高频的错法。
        assert re.search(r'command -v "\$PY"', script), (
            "装解释器前要先看是不是已经有了"
        )
        assert 'PY=python3.12' in script, "PY 变量应当钉死到 3.12"

    def test_venv_creation_is_guarded(self, script: str):
        assert re.search(r'if \[ ! -x "\$VENV/bin/python"', script), (
            "建 venv 前要先看是不是已经存在"
        )

    def test_pip_install_is_skipped_when_unchanged(self, script: str):
        # pip install 不快，而 provisioning 可能被反复跑。
        assert "sha256sum" in script, "依赖未变时应跳过安装（用 requirements 的哈希判断）"

    def test_service_restart_is_conditional(self, script: str):
        # 无谓重启会打断正在跑的切换。
        assert "cmp -s" in script, "systemd 单元未变时不该重启服务"

    def test_does_not_replace_system_python(self, script: str):
        # 系统 python3 是 3.9，aws-cfn-bootstrap 与 ec2-utils 依赖它。
        assert "系统 python3 被动过" in script or "python3 --version" in script, (
            "脚本要守住「系统 python3 没被动过」"
        )


class TestVerificationUsesRightCriteria:
    """脚本自己的核实步骤也要守本项目的判据纪律。"""

    def test_checks_poller_field_presence_not_count(self, script: str):
        # 字段缺失时服务端什么都没报，报成「0 个 worker」是把未测量写成测量值。
        assert '"pollers"' in script
        assert "不是「数量是否为 0」" in script or "字段是否存在" in script, (
            "判据必须是「pollers 字段是否存在」而不是数量"
        )

    def test_timeout_is_reported_as_inconclusive(self, script: str):
        # 等不到 poller 不等于没有 worker —— 也可能是 Temporal 还没起来。
        assert "不等于" in script, "超时未见 poller 时要说明它不等于「没有 worker」"

    def test_task_queue_type_uses_prefixed_enum(self, script: str):
        # 裸 WORKFLOW 会被服务端拒绝（实测 code 3）。
        assert "TASK_QUEUE_TYPE_WORKFLOW" in script
        assert "?taskQueueType=WORKFLOW" not in script


class TestPermissionsCoverWorkerPrefix:
    def test_role_can_read_worker_prefix(self):
        text = PERMS.read_text(encoding="utf-8")
        assert "worker/*" in text, "实例角色要能读 worker/ 前缀才能取到代码"

    def test_worker_prefix_is_read_only(self):
        text = PERMS.read_text(encoding="utf-8")
        # 能写自己的代码就等于能改变自己下一次启动后的行为。
        assert "s3:PutObject" not in text
        assert "s3:DeleteObject" not in text


def test_script_is_executable():
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "provision-worker.sh 应当可执行"

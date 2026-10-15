"""
test_137_mcp_runtime_not_stale.py

守「AgentCore 上的 graph MCP server 跑的代码必须与 main 一致」。

## 为什么需要（2026-10-15 实测）

验证「东京 DevOps Agent 调用 graph MCP 加速 RCA」是否可行时发现：
线上 MCP Runtime v3 部署于 2026-09-05，而它打包的依赖代码此后
`rca/neptune/neptune_queries.py` 改了 5 次、`profiles/graph_contract.yaml`
改了 14 次 —— **MCP 一次都没重新部署**。

其中 1c1a1b7（2026-09-06）修了 q1/q3 的**方向反了 + 只看 2/7 种依赖边**。
修复到了 Lambda 和本地，**从没到 MCP**。实测后果：

| 查询 | 线上 v3 | main |
|---|---|---|
| `q1_blast_radius(ddbpetadoption)` 影响面 | **0** | **21** |
| `q1_blast_radius(payforadoption, live)` | petstatusupdater, petsearch（其实是它**依赖**的） | petsite, trafficgenerator（真正调用它的） |

DevOps Agent 问「X 挂了影响谁」，拿到的是「X 依赖谁」—— 而且带着
`_provenance` 与「确定性查询，无 LLM 参与」的权威标签。
**根因与受害者被颠倒，而调用方毫无办法察觉。**

漂移 40 天无人发现的原因很简单：没有部署脚本（每次手敲
`update-agent-runtime`），也没有任何东西比对线上与仓库。

## 判据

对 MCP 包里**会影响查询结果**的源码文件，计算内容哈希，与线上
Runtime 当前 artifact 解包后的同名文件比对。不比对整个 zip 的哈希 ——
zip 里有 vendored 依赖与时间戳，每次重打都会变，那是噪音。

## 何时运行

需要 AWS 凭证与 S3 读权限，所以标 `neptune`（与本仓其它活环境测试同口径）。
`GDP_OFFLINE=1` 时 skip。拿不到线上 artifact 时**失败而不是 skip** ——
一个「因为探测不到所以永远不报警」的门禁比没有更坏（test_135 的同款教训）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import subprocess
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ID = 'graph_dependency_mcp-12Vg2Z9XXu'
REGION = 'ap-northeast-1'

# 会影响 MCP 返回内容的源码。改了任何一个而不重新部署，线上答案就会过期。
# 刻意不含 vendored 依赖与 __init__.py。
TRACKED = [
    'rca/neptune/neptune_queries.py',
    'rca/neptune/neptune_client.py',
    'profiles/graph_contract.yaml',
    'graph_mcp/server.py',
    'graph_mcp/catalog_tools.py',
    'graph_mcp/provenance.py',
]

# 包内路径：graph_mcp/* 平铺在 zip 根（见 build_package.sh），其余保持相对路径
def _pkg_path(repo_path: str) -> str:
    return repo_path.split('/', 1)[1] if repo_path.startswith('graph_mcp/') else repo_path


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _live_artifact() -> tuple[str, str, int]:
    out = subprocess.run(
        ['aws', 'bedrock-agentcore-control', 'get-agent-runtime',
         '--agent-runtime-id', RUNTIME_ID, '--region', REGION,
         '--query', '{s3:agentRuntimeArtifact.codeConfiguration.code.s3,v:agentRuntimeVersion}',
         '--output', 'json'],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"取不到 Runtime 配置：{out.stderr[:300]}"
    d = json.loads(out.stdout)
    return d['s3']['bucket'], d['s3']['prefix'], int(d['v'])


@pytest.mark.neptune
def test_t137_01_live_mcp_matches_main():
    if os.environ.get('GDP_OFFLINE') == '1':
        pytest.skip('GDP_OFFLINE=1')
    bucket, key, ver = _live_artifact()
    r = subprocess.run(['aws', 's3', 'cp', f's3://{bucket}/{key}', '-', '--only-show-errors'],
                       capture_output=True, timeout=180)
    assert r.returncode == 0 and r.stdout, (
        f"取不到线上 artifact s3://{bucket}/{key}：{r.stderr[:300]!r}。"
        "这不是 skip 的理由 —— 探测不到就永远不会报警。"
    )
    z = zipfile.ZipFile(io.BytesIO(r.stdout))
    names = set(z.namelist())

    stale = []
    for repo_path in TRACKED:
        local = (ROOT / repo_path).read_bytes()
        pp = _pkg_path(repo_path)
        if pp not in names:
            stale.append(f"  {repo_path}: 线上包里没有 {pp}")
            continue
        live = z.read(pp)
        if _sha(live) != _sha(local):
            stale.append(f"  {repo_path}: 线上 {_sha(live)} ≠ main {_sha(local)}")

    assert not stale, (
        f"AgentCore 上的 graph MCP（Runtime v{ver}，{key}）跑的代码与 main 不一致：\n"
        + "\n".join(stale)
        + "\n\nDevOps Agent 正在用它做 RCA。这类漂移的真实代价见本文件 docstring："
          "q1 的方向修复到了 Lambda 却没到 MCP，影响面查询把根因与受害者颠倒了 40 天。"
          "\n重新部署：bash graph_mcp/deploy_runtime.sh"
    )


def test_t137_02_deploy_script_exists_and_omits_immutable_field():
    """必须有部署脚本，且不得回传 requireServiceS3Endpoint。

    2026-10-15 实测：对 2026-06-08 后创建的 agent，update-agent-runtime
    只要请求里**出现** requireServiceS3Endpoint 就报 ValidationException ——
    即使传的就是它当前的值 false。所以「get 回来原样 update 回去」这个直觉
    做法会失败。脚本必须显式不带这个键。
    """
    s = ROOT / 'graph_mcp' / 'deploy_runtime.sh'
    assert s.is_file(), "缺 graph_mcp/deploy_runtime.sh —— 手敲 update-agent-runtime 正是漂移的根源"
    txt = s.read_text(encoding='utf-8')
    code = '\n'.join(l for l in txt.splitlines() if not l.lstrip().startswith('#'))
    assert 'requireServiceS3Endpoint' not in code, (
        "部署脚本在请求里带了 requireServiceS3Endpoint —— 对本 agent 必然 ValidationException"
    )
    # 全量重传：只传 artifact 会清掉 authorizer / network（部署记录里踩过）
    for flag in ('--authorizer-configuration', '--network-configuration',
                 '--protocol-configuration', '--environment-variables', '--role-arn'):
        assert flag in code, f"部署脚本缺 {flag} —— update-agent-runtime 不传就会把它清掉"

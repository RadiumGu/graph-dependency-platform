"""分页上限必须抛异常，不能静默截断；ETL 的 boto3 client 必须带显式退避。

## 守的是什么（2026-10-06 立）

`infra/lambda/etl_aws/graph_gc.py` 的 `_gc_vertices` 做的是：

    stale = set(graph_map.keys()) - aws_ids
    for pid in stale:
        neptune_query(f"g.V('{graph_map[pid]}').drop()")   # 真删

而 `aws_ids` 来自 `run_gc` 里一串 `for page in client.get_paginator(...).paginate()`。

也就是说 **分页少收 == 真实存在的资源被当成 ghost 节点删掉**。

所以给分页加「页数上限」这件事有一个陷阱：如果实现成「到上限就 return，
调用方拿到部分结果」，这道保护本身就成了数据丢失的来源，而且是静默的 ——
比不加上限更糟。

本测试把「上限触发时必须抛异常」钉成不变量，并顺带守住：
  · 截止时间触发时同样抛异常
  · ETL 创建的 boto3 client 必须走 make_client（带显式 standard 重试）
  · 新模块已列进 shared/build.sh 的 Layer 必需项校验

同源的既有门禁：test_128_cdk_layer_asset_must_carry_deps.py
（Layer 的 fromAsset 必须自带依赖）。
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SHARED_PY = REPO / "infra" / "lambda" / "shared" / "python"
LAMBDA_DIR = REPO / "infra" / "lambda"

sys.path.insert(0, str(SHARED_PY))

import aws_resilience as ar  # noqa: E402


# ── 测试替身 ────────────────────────────────────────────────────────────


class _FakeInnerPaginator:
    """产出无限多页 —— 用来逼出上限行为。"""

    def __init__(self, pages: int | None = None):
        self._pages = pages

    def paginate(self, **kwargs):
        i = 0
        while self._pages is None or i < self._pages:
            i += 1
            yield {"Items": [f"item-{i}"]}


class _FakeClient:
    def __init__(self, pages: int | None = None):
        self._pages = pages
        self.config_seen = None

    def get_paginator(self, operation_name):
        return _FakeInnerPaginator(self._pages)


def _bounded(pages=None, max_pages=3, deadline=None):
    return ar._BoundedPaginator(
        _FakeInnerPaginator(pages),
        service="fake",
        operation="list_things",
        max_pages=max_pages,
        deadline=deadline or ar.Deadline.none(),
    )


# ── 核心不变量 ──────────────────────────────────────────────────────────


def test_page_cap_raises_instead_of_truncating():
    """超出页数上限必须抛异常，而不是静默返回部分结果。"""
    p = _bounded(pages=None, max_pages=3)
    collected = []
    with pytest.raises(ar.PaginationTruncated):
        for page in p.paginate():
            collected.extend(page["Items"])
    # 已产出的页数不超过上限 —— 但关键是调用方拿到了异常，
    # 不可能把这 3 页当成完整结果。
    assert len(collected) == 3


def test_page_cap_not_triggered_when_within_limit():
    """页数在上限内时正常走完，不抛异常。"""
    p = _bounded(pages=3, max_pages=3)
    collected = [i for page in p.paginate() for i in page["Items"]]
    assert collected == ["item-1", "item-2", "item-3"]


def test_deadline_raises_instead_of_stopping_early():
    """触达截止时间必须抛异常，而不是提前 return 部分结果。"""

    class _Expired(ar.Deadline):
        def expired(self):  # type: ignore[override]
            return True

    p = _bounded(pages=None, max_pages=1000, deadline=_Expired(None))
    with pytest.raises(ar.PaginationDeadlineExceeded):
        list(p.paginate())


def test_truncation_error_message_is_greppable():
    """报错信息要带固定前缀，便于告警与 Logs Insights 过滤。"""
    p = _bounded(pages=None, max_pages=1)
    with pytest.raises(ar.PaginationTruncated) as exc:
        list(p.paginate())
    assert ar.LOG_PREFIX_TRUNCATED in str(exc.value)
    assert "list_things" in str(exc.value)


def test_retry_config_declares_standard_mode_explicitly():
    """不依赖 boto3 默认的 legacy 模式 —— 必须显式声明 standard。"""
    cfg = ar.retry_config()
    assert cfg.retries["mode"] == "standard"
    assert cfg.retries["max_attempts"] >= 3
    assert cfg.connect_timeout > 0
    assert cfg.read_timeout > 0


def test_make_client_resolved_config_is_not_legacy():
    """断言的是**解析后**落到 client 上的配置，而不只是我们传进去的 Config。

    botocore 会规范化 retries（`max_attempts: 5` → `total_max_attempts: 6`），
    所以必须验最终形态。对照：不传 config 的 client 是 `{'mode': 'legacy'}`、
    read_timeout 60 —— 那正是改动前 6 条 ETL 的状态。

    不需要 AWS 凭证：只构造 client 读它的 meta.config，不发任何请求。
    """
    import boto3 as _boto3

    session = _boto3.Session(region_name="ap-northeast-1")
    client = ar.make_client("ec2", region_name="ap-northeast-1", session=session)
    resolved = client._client.meta.config
    assert resolved.retries["mode"] == "standard", (
        f"解析后的 retry 模式是 {resolved.retries!r}，不是 standard"
    )
    assert resolved.retries.get("total_max_attempts", 0) >= 4
    assert resolved.read_timeout == ar.DEFAULT_READ_TIMEOUT
    assert resolved.connect_timeout == ar.DEFAULT_CONNECT_TIMEOUT
    # 包装层不得改变 client 的其余行为
    assert client.meta.service_model.service_name == "ec2"
    assert callable(client.describe_instances)
    assert isinstance(client.get_paginator("describe_instances"), ar._BoundedPaginator)


def test_deadline_from_context_without_getter_is_unbounded():
    """本地跑 / 测试桩没有 get_remaining_time_in_millis 时不设限，
    而不是拍一个假的截止时间。"""
    dl = ar.Deadline.from_lambda_context(object())
    assert dl.expired() is False
    assert dl.remaining() is None


def test_deadline_reserves_budget_for_teardown():
    class _Ctx:
        @staticmethod
        def get_remaining_time_in_millis():
            return 120_000

    dl = ar.Deadline.from_lambda_context(_Ctx(), reserve_seconds=60)
    rem = dl.remaining()
    assert rem is not None and 55 <= rem <= 60


def test_resilient_client_passes_through_non_paginated_calls():
    """describe_* / list_* 等直接调用必须透传，不被包装层改变行为。"""
    sentinel = {"ok": True}

    class _C:
        def describe_things(self, **kw):
            return sentinel

        def get_paginator(self, op):
            return _FakeInnerPaginator(1)

    rc = ar._ResilientClient(_C(), service="fake", max_pages=10,
                             deadline=ar.Deadline.none())
    assert rc.describe_things() is sentinel


def test_paginate_all_raises_on_cap():
    """paginate_all 同样不得返回部分结果。"""
    with pytest.raises(ar.PaginationTruncated):
        ar.paginate_all(_FakeClient(pages=None), "list_things", "Items", max_pages=2)


# ── 仓库级不变量 ────────────────────────────────────────────────────────


def test_build_sh_verifies_the_new_layer_module():
    """新 shared 模块必须列进 Layer 必需项校验。

    否则 `cdk deploy` 会打出一个缺它的 Layer，而挂这个 Layer 的 6 个 ETL
    会在 import 阶段全部失败 —— 2026-10-04 / 10-05 已经各发生过一次同类事故。
    """
    build_sh = (LAMBDA_DIR / "shared" / "build.sh").read_text()
    assert "aws_resilience.py" in build_sh, (
        "infra/lambda/shared/build.sh 的必需项校验里没有 aws_resilience.py"
    )


def _etl_source_files():
    skip = ("__pycache__", "/certifi/", "/requests/", "/urllib3/", "/idna/",
            "/charset_normalizer/", "/bin/", "/_yaml/")
    for p in LAMBDA_DIR.glob("etl_*/**/*.py"):
        s = str(p)
        if any(k in s for k in skip):
            continue
        yield p


# 任何 `X.client(` 形态都要管住，不只是 `boto3.client(`。
#
# 2026-10-06 教训：这条正则最初只写了 `boto3\.client\(`，于是门禁通过，
# 而 `etl_aws/handler.py:76-87` 的 12 个主 client 全是 `session.client(...)`
# 创建的 —— 恰好就是喂给 graph_gc 那条误删路径的那些。
# **不变量写窄了，等于没写。**
_RAW_CLIENT = re.compile(r"(?<!make_)\b[A-Za-z_][A-Za-z_0-9]*\.client\s*\(")


def test_etl_clients_go_through_make_client():
    """ETL 不得直接创建 boto3 client —— 那样就没有显式退避，也没有分页护栏。

    覆盖 `boto3.client(...)` 与 `session.client(...)` 两种形态。
    确实需要裸 client 的地方，在同一行或上一行写
    `# aws-resilience: exempt — <理由>`。
    """
    offenders = []
    for p in _etl_source_files():
        lines = p.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not _RAW_CLIENT.search(line):
                continue
            if line.lstrip().startswith("#"):
                continue
            context = line + (lines[i - 1] if i else "")
            if "aws-resilience: exempt" in context:
                continue
            offenders.append(f"{p.relative_to(REPO)}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "这些调用点仍在直接创建 boto3 client，缺少退避与分页护栏：\n  "
        + "\n  ".join(offenders)
    )


# graph_gc 里凡是喂给 _gc_vertices 的 AWS 清单，都不得来自未分页调用。
# 未分页 == 可能少收 == 把真实存在的资源当 ghost 节点删掉。
_GC_UNPAGINATED = re.compile(
    r"\b[a-z_]+_client\.(list|describe)_[a-z_]+\s*\("
)


def test_graph_gc_never_builds_staleness_set_from_unpaginated_call():
    """graph_gc 不得用未分页的 list_*/describe_* 构造陈旧判定集合。

    必须走 paginate_all(...) 或 get_paginator(...).paginate()，
    两者在超限时都抛异常而非返回部分结果。

    例外（例如 get_bucket_location 这种单资源查询，不产生清单）
    写 `# gc-pagination: exempt — <理由>`。
    """
    gc_py = LAMBDA_DIR / "etl_aws" / "graph_gc.py"
    lines = gc_py.read_text().splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _GC_UNPAGINATED.search(line)
        if not m:
            continue
        if "get_paginator" in line or "paginate_all" in line:
            continue
        context = line + (lines[i - 1] if i else "")
        if "gc-pagination: exempt" in context:
            continue
        offenders.append(f"graph_gc.py:{i + 1}: {line.strip()}")
    assert not offenders, (
        "这些调用未分页，少收会让 _gc_vertices 删掉真实存在的资源：\n  "
        + "\n  ".join(offenders)
    )


def test_graph_gc_does_not_silently_swallow_inventory_errors():
    """graph_gc 里不得有裸的 `except Exception: pass`。

    原 S3 分支就是这个形状：get_bucket_location 一次瞬时失败 → 桶不进
    aws_s3 → 被判为 ghost 节点删除。也就是一次限流能让真实存在的桶
    从图里消失，且无任何日志。
    """
    import ast as _ast

    gc_py = LAMBDA_DIR / "etl_aws" / "graph_gc.py"
    tree = _ast.parse(gc_py.read_text())
    silent = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.ExceptHandler):
            continue
        if all(isinstance(s, (_ast.Pass, _ast.Continue, _ast.Break)) for s in node.body):
            silent.append(node.lineno)
    assert not silent, (
        f"graph_gc.py 这些行的 except 块静默吞错（行号 {silent}）。"
        "清单类错误必须记日志并放弃该类的 GC，不能当作「资源不存在」。"
    )


def test_resilience_fallback_is_not_silent():
    """防御式 import 的降级分支必须说出来。

    降级意味着「无退避、无分页护栏、graph_gc 误删防护不生效」。
    静默降级会让人分不出「护栏在起作用」和「护栏根本没加载」——
    那正是本仓第一条设计原则要防的：判据必须能区分「否」与「我不知道」。

    2026-10-06：这个缺陷是我自己写出来的。第一版 shim 的 except 分支只有
    `make_client = boto3.client`，部署后从 Lambda 日志里无法判断 Layer
    到底带没带 aws_resilience。
    """
    offenders = []
    for p in _etl_source_files():
        src = p.read_text(errors="replace")
        if "from aws_resilience import" not in src:
            continue
        # 取 except ImportError 之后、到下一个顶层语句之前的那一段
        for m in re.finditer(r"except ImportError:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)+)", src):
            block = m.group(1)
            if "AWS_RESILIENCE_UNAVAILABLE" not in block:
                offenders.append(str(p.relative_to(REPO)))
    assert not offenders, (
        "这些文件的降级分支是静默的，必须记一条带 AWS_RESILIENCE_UNAVAILABLE "
        "前缀的 warning：\n  " + "\n  ".join(sorted(set(offenders)))
    )

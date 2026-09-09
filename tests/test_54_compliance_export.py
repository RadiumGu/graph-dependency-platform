"""tests/test_54_compliance_export.py — 合规导出层的四条验收要求

## 这些断言对应文档 4.1 的四条要求

    m01  依赖边清单必须来自契约，不得内联字面量
    m02  verify_status 与 dependency_kind 是强制列
    m03  三栏分列不得合并成单一「覆盖率」
    m04  承载层必须单列且注明性质

要求写在 `todo/decks/合规依赖报告-能力评估与补齐路线.md` 4.1 节。**文档里的要求
不会自己生效** —— 本仓库已经有过多次「文档写了纪律、代码悄悄违反」的记录
（四处各抄一份依赖边清单、drift 查询查一个不存在的标签），所以每条都要有断言。

## 为什么大部分是静态扫描而非运行时断言

违反这些要求的代码路径压根不会调用任何校验函数。运行时断言对「有人内联了一份
标签清单」这件事结构性地无能为力 —— 与 test_51::m01（漏写 source）、
test_53::m01（drift 兜底清单漂移）同一判据类型。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PKG = _ROOT / "compliance_export"
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "infra" / "lambda" / "shared" / "python"))


@pytest.fixture(scope="module")
def dep_labels() -> frozenset:
    from graph_contract import dependency_edge_labels  # type: ignore
    labels = frozenset(dependency_edge_labels())
    assert labels, "契约里应有依赖边；取不到说明测试自身失效"
    return labels


def _pkg_sources():
    for p in sorted(_PKG.rglob("*.py")):
        yield p, p.read_text(encoding="utf-8")


# ─── m01：依赖边清单必须来自契约 ─────────────────────────────────────────────


def test_m01_依赖边清单不得内联字面量(dep_labels):
    """导出层不得出现依赖边标签的硬编码集合。

    判据：任何 .py 文件里若同时出现 **两个以上**契约依赖边标签的字符串字面量，
    就视为内联了一份清单。单个标签允许出现（如 docstring 里举例说明），
    两个以上就构成「另抄一份」。

    ## 为什么阈值是 2 而不是 1

    `Implements`、`LocatedIn` 这类非依赖边标签必须能写字面量（它们不在契约的
    依赖集里，本判据也不管）。而 docstring 里说明「PublishesTo 于 2026-09-08
    改判」这种单点引用是合理的。真正危险的是成组出现 —— 那才是清单。
    """
    offenders = []
    for path, src in _pkg_sources():
        # 逐「逻辑行」看：同一处出现多个依赖边标签才算成组
        for lineno, line in enumerate(src.splitlines(), 1):
            found = {l for l in dep_labels
                     if re.search(r"""['"]%s['"]""" % re.escape(l), line)}
            if len(found) >= 2:
                offenders.append(
                    "%s:%d 同时内联了 %s"
                    % (path.relative_to(_ROOT), lineno, sorted(found)))
    assert not offenders, (
        "导出层内联了依赖边清单：\n  " + "\n  ".join(offenders)
        + "\n\n必须调用 graph_contract.dependency_edge_labels()。"
          "本仓库曾因四处各抄一份而产生分歧，其中两处漂移到实际错误。")


def test_m01b_必须真的调用契约函数():
    """光是「没有内联」不够 —— 还得证明它确实从契约取。

    防的是「把清单挪到别的文件」这种绕过：m01 只看字面量，
    若有人把清单写进 profiles 或另一个模块，m01 抓不到但 m01b 会。
    """
    joined = "".join(src for _p, src in _pkg_sources())
    assert "dependency_edge_labels" in joined, (
        "导出层没有引用 dependency_edge_labels()。依赖边清单的单一来源是契约，"
        "不接受任何其他出处。")
    assert "from graph_contract import" in joined, (
        "没有从 graph_contract 导入。若改为间接导入，请同步更新本断言。")


# ─── m02：强制列 ─────────────────────────────────────────────────────────────


def test_m02_verify_status_与_dependency_kind_是强制列():
    """两列缺任何一列，报告就退化成填表工具的产出。

    `verify_status` 回答 SYSC 15A.5.3R 的「你对这条依赖做过场景测试吗」；
    `dependency_kind` 回答「这是配置声明的还是运行时观测到的」。
    """
    from compliance_export import CSV_COLUMNS

    for col in ("verify_status", "dependency_kind"):
        assert col in CSV_COLUMNS, (
            "CSV_COLUMNS 缺少强制列 %r。这一列是本平台相对填表工具的差异化所在，"
            "不是可选的展示字段。" % col)

    # 查询也必须真的取这两个字段，否则列在但永远为空
    src = (_PKG / "queries.py").read_text(encoding="utf-8")
    for col in ("verify_status", "dependency_kind"):
        assert "d.%s AS %s" % (col, col) in src, (
            "queries.py 的功能映射查询没有取 d.%s —— "
            "CSV 里那一列会永远是空的，比没有这一列更糟（看起来已验证过而实际没查）。"
            % col)


def test_m02b_缺失值不得折叠成零():
    """`None` 表示「未验证 / 尚未打标」，与 0 或空档语义不同。

    折叠会丢掉「我们知道自己不知道」这个信息，而那正是审计要看的诚实度。
    """
    from compliance_export import MISSING_LABEL
    from compliance_export.breakdown import Breakdown

    rows = [
        {"verify_status": "confirmed", "dependency_kind": "dynamic", "target_scope": "observed"},
        {"verify_status": None, "dependency_kind": None, "target_scope": "external"},
    ]
    bd = Breakdown(rows)
    b = bd.bucket("verify_status", MISSING_LABEL)
    assert b is not None and b.count == 1, (
        "缺失的 verify_status 应单独成档（%r），不得并入其它取值或消失。"
        % MISSING_LABEL)
    assert bd.bucket("verify_status", "0") is None, "缺失值不得折叠成 0"


# ─── m03：不得合并成单一覆盖率 ───────────────────────────────────────────────


def test_m03_不得提供合并成单一覆盖率的接口():
    """`Breakdown` 不得有任何返回「总体覆盖率」的方法。

    三个维度回答三个不同问题；平均会让「已声明但从未观测」与
    「已观测但从未验证」互相抵消 —— 这是监管审查最容易挑的点。

    判据是**接口层面**的：扫 Breakdown 的公开方法名，出现聚合语义的词就拦。
    这比写在 docstring 里的纪律强，因为它拦得住「后来有人觉得加个总分很方便」。
    """
    from compliance_export.breakdown import Breakdown

    banned = ("overall", "coverage", "score", "aggregate", "combined",
              "summary_ratio", "total_ratio", "grade")
    public = [n for n in dir(Breakdown) if not n.startswith("_")]
    bad = [n for n in public if any(b in n.lower() for b in banned)]
    assert not bad, (
        "Breakdown 出现了合并语义的公开接口：%s\n"
        "三栏必须分列。需要单一指标的场合应显式指定维度（dimension('verify_status')）"
        "并自行解读。" % bad)

    # 报告里也不得出现「总覆盖率」这类合并表述
    md_src = (_PKG / "report.py").read_text(encoding="utf-8")
    assert "总覆盖率" not in md_src and "综合覆盖率" not in md_src, (
        "report.py 出现了合并覆盖率的表述。")


def test_m03b_三栏各自求和必须等于总数():
    """分列统计的自检 —— 比例算错的报告比没有报告更糟。"""
    from compliance_export.breakdown import Breakdown

    rows = [
        {"verify_status": "confirmed", "dependency_kind": "dynamic", "target_scope": "observed"},
        {"verify_status": None, "dependency_kind": "static", "target_scope": "external"},
        {"verify_status": "inconclusive", "dependency_kind": None, "target_scope": "observed"},
    ]
    bd = Breakdown(rows)
    assert bd.self_check() == [], "自检应通过：%s" % bd.self_check()
    assert bd.total == 3
    for field_name, _t, _w, buckets in bd.dimensions():
        assert sum(b.count for b in buckets) == 3, field_name


# ─── m04：承载层单列且注明性质 ───────────────────────────────────────────────


def test_m04_承载层必须单列且与依赖边不重叠(dep_labels):
    """承载/放置关系不得出现在依赖集里，且必须带「性质」说明。

    承载边普遍为真、不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。
    但它们也不能被静默丢弃 —— EKS 与 LB 在 DORA 视角下确实是关键 ICT 服务。
    """
    from compliance_export import HOSTING_EDGE_LABELS

    overlap = sorted(set(HOSTING_EDGE_LABELS) & dep_labels)
    assert not overlap, (
        "承载层标签与契约依赖边重叠：%s。同一种边不能既是承载又是依赖 —— "
        "若契约改了分类，这里要跟着改，并重新审视报告口径。" % overlap)

    src = (_PKG / "queries.py").read_text(encoding="utf-8")
    assert '"nature"' in src or "'nature'" in src, (
        "fetch_hosting_layer 没有输出 nature 字段。承载层单列了但不注明性质，"
        "读者会以为它们是依赖。")

    md_src = (_PKG / "report.py").read_text(encoding="utf-8")
    assert "非服务消费关系" in md_src, (
        "报告模板里没有「非服务消费关系」的说明。")


# ─── 快照纪律（文档 2.5 节）───────────────────────────────────────────────────


def test_m05_快照时刻必须唯一且写进每份产出():
    """一次导出只取一次时刻，且 Markdown 与 CSV 都要带。

    DORA 用 12/31 统一基准日，正是为了让所有模板可交叉校验；ESAs 点名的失败
    模式里就有「跨模板基准日不一致」。实测本环境数分钟内 LocatedIn 从 1060
    变 1054 —— 分次拼接的报告会自相矛盾。
    """
    src = (_PKG / "queries.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "take_snapshot"), None)
    assert fn is not None, "找不到 take_snapshot"
    now_calls = sum(
        1 for n in ast.walk(fn)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("now", "utcnow")
    )
    assert now_calls == 1, (
        "take_snapshot 里取当前时刻 %d 次，应恰好 1 次。多次取时刻会让同一份"
        "报告的不同表落在不同基准日上。" % now_calls)

    rep = (_PKG / "report.py").read_text(encoding="utf-8")
    assert "snap.taken_at" in rep, "报告没有写入快照时刻"
    assert rep.count("snap.taken_at") >= 3, (
        "快照时刻应出现在 Markdown 页首、CSV 注释行与文件名中（至少 3 处），"
        "实际 %d 处。" % rep.count("snap.taken_at"))


def test_m06_文件名必须带快照时刻():
    """SYSC 15A.6.2R 要求保存 **each version** 的记录 6 年 —— 同名覆盖会让历史消失。"""
    rep = (_PKG / "report.py").read_text(encoding="utf-8")
    fn_src = rep[rep.index("def write_bundle"):]
    assert "stamp" in fn_src, "write_bundle 的文件名没有带时间戳"
    assert "%s.md" % "" not in fn_src or "_%s.md" in fn_src, (
        "Markdown 文件名应包含快照时刻。")


# ─── 只读保证 ────────────────────────────────────────────────────────────────


def test_m07_导出层不得写图谱():
    """导出是只读操作。出现任何写操作关键字即视为违规。

    合规报告的生成过程若会改动被报告的对象，报告本身就不可采信。
    """
    banned = ("CREATE ", "MERGE ", "DELETE ", "SET ", "REMOVE ", "DETACH",
              "addV(", "addE(", "property(", "drop(")
    offenders = []
    for path, src in _pkg_sources():
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            for b in banned:
                if b in line:
                    offenders.append("%s:%d 含 %r"
                                     % (path.relative_to(_ROOT), lineno, b))
    assert not offenders, (
        "导出层出现写操作：\n  " + "\n  ".join(offenders)
        + "\n\n生成合规报告的过程不得改动被报告的对象。")


# ─── 样例与文档 ──────────────────────────────────────────────────────────────


_SAMPLES = _PKG / "samples"


def test_m08_样例产出必须存在且结构与当前代码一致():
    """样例是这个目录的交付物之一，不能悄悄过期。

    判据不是「内容一字不差」（活图谱的数字每次都会变，锁内容会让门禁天天红），
    而是**结构一致**：CSV 表头必须等于当前 `CSV_COLUMNS`，Markdown 必须含当前
    模板的全部章节标题。代码改了列或章节而样例没重生成，这条就会红。
    """
    from compliance_export import CSV_COLUMNS

    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    csv_p = _SAMPLES / "SAMPLE-compliance-dependency-report.csv"
    assert md.exists(), (
        "缺少样例 Markdown。重新生成：\n"
        "  python3 -m compliance_export --out-dir compliance_export/samples --sample")
    assert csv_p.exists(), "缺少样例 CSV"

    # CSV 表头 == 当前 CSV_COLUMNS
    header = None
    for line in csv_p.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            header = line
            break
    assert header is not None, "样例 CSV 只有注释行，没有表头"
    assert header.split(",") == list(CSV_COLUMNS), (
        "样例 CSV 的表头与当前 CSV_COLUMNS 不一致 —— 代码改了列但样例没重生成。\n"
        "  样例: %s\n  当前: %s" % (header, ",".join(CSV_COLUMNS)))

    # Markdown 必须含全部章节
    md_text = md.read_text(encoding="utf-8")
    for section in ("## 三栏分列统计", "## 功能映射表", "## 技术集中度",
                    "## 基础设施承载层", "## 业务能力与容忍度阈值",
                    "## 必须随报告一同披露的局限"):
        assert section in md_text, (
            "样例 Markdown 缺少章节 %r —— 模板改了但样例没重生成。" % section)

    # 快照时刻必须写进样例（即使文件名固定）
    assert "快照时刻" in md_text, "样例没有记录快照时刻"
    assert "snapshot_taken_at=" in csv_p.read_text(encoding="utf-8"), (
        "样例 CSV 没有记录快照时刻")


def test_m09_样例不得出现未越界表述():
    """impact tolerance 全为 null 时，报告不得出现任何「未越界」表述。

    SYSC 15A.2.5R 要求 firm *must* set an impact tolerance。没设就是没设，
    不能用「没检测到越界」掩盖「压根没有阈值可比」—— 这是本会话反复清理的
    那类失效模式（一个看起来在工作、实际永远给同一个答案的检查）。

    ## 判据要排除禁令句本身（第一版是假阳性）

    第一版朴素子串匹配，结果被披露文本自己触发了 ——
    报告里那句「本报告**不得**出现任何『未越界』表述」含有被禁短语。
    假阳性会训练人忽略告警，比没有门禁更糟（与 test_53::m03 第一版把
    30+ 处节点标签报成违规是同一教训）。

    所以跳过**本身即禁令**的行：含「不得 / 不能 / 禁止 / 禁用」的行是在
    声明纪律，不是在下越界结论。
    """
    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")

    prohibition_markers = ("不得", "不能", "禁止", "禁用")
    banned = ("未越界", "无越界", "未超出容忍度", "均在容忍度内", "容忍度充足")

    offenders = []
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if any(m in line for m in prohibition_markers):
            continue                      # 这行是在声明纪律，不是下结论
        for phrase in banned:
            if phrase in line:
                offenders.append("第 %d 行: %s" % (lineno, line.strip()[:90]))
    assert not offenders, (
        "样例出现了越界结论，而 impact tolerance 未设定：\n  "
        + "\n  ".join(offenders)
        + "\n\n没有阈值就是没有阈值，不能用「没检测到越界」掩盖。")


def test_m10_唯一入口():
    """CLI 入口只能有一个。

    本仓库有过「同一份清单四处各抄一份、其中两处漂移到实际错误」的记录；
    两个 CLI 入口是同类风险 —— 一个改了另一个没改，而两者都还能跑。
    """
    stray = _ROOT / "scripts" / "export_compliance_report.py"
    assert not stray.exists(), (
        "存在第二个 CLI 入口 %s。入口应只有 compliance_export/__main__.py。"
        % stray.relative_to(_ROOT))
    assert (_PKG / "__main__.py").exists(), "缺少 compliance_export/__main__.py"
    assert (_PKG / "README.md").exists(), (
        "缺少 compliance_export/README.md —— 这个目录是交付物，要能自解释。")

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

    # Markdown 必须含全部章节。章节名带编号 —— 正式文档要能被「见 §5.2」这样引用。
    md_text = md.read_text(encoding="utf-8")
    for section in ("## 文档控制",
                    "## 1 报告性质与适用范围",
                    "## 2 报告基准与数据来源",
                    "## 3 适用标准",
                    "## 4 术语与取值定义",
                    "## 5 依赖关系映射",
                    # §6 是 2026-09-15 新增：这 16 条 confirmed 的证据是异质的
                    # （iam-deny 全程失败 vs rds-reboot 仅约 16~18 秒瞬时中断），
                    # 不逐手段披露范围就是过度声称。
                    "## 6 切断手段与证据范围",
                    "## 7 证据状态分列统计",
                    "## 8 技术集中度",
                    "## 9 基础设施承载层",
                    "## 10 业务功能与容忍度阈值",
                    "## 11 重大固有局限性与范围排除",
                    "## 附录 A"):
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


# ─── 文档形制（把「看起来正规」变成可校验的约束）─────────────────────────────


def test_m11_不得声称是独立鉴证报告():
    """报告必须声明自己不是鉴证报告，且不得作出三条它做不出的声明。

    ## 这条门禁防的是什么

    形制改造有一个具体的失效方向：为了让报告「看起来正规」而照抄 ISAE 3000
    §69 的全部要素。但 §69(h)(i)(j) 要求声明「本业务按本 ISAE 执行」、
    「适用 ISQC 1 质量控制」、「遵守 IESBA Code 独立性要求」—— 一份由自有工具
    自动生成、未经独立执业者鉴证的管理层记录，这三条一条都做不出。

    照抄的结果是一份**暗示存在独立鉴证的文件**。那不是形制粗糙，是虚假陈述，
    危害远大于原先的「山寨感」。所以：必须有免责声明，且不得出现那三类断言。

    这是形制改造里唯一有法律风险的一步，因此单独立一条门禁锁住。
    """
    rep = (_PKG / "report.py").read_text(encoding="utf-8")

    assert "不是鉴证报告" in rep, (
        "报告模板没有声明「本报告不是鉴证报告」。ISAE 3000 §69(f) 要求提醒读者"
        "适用范围；缺了这句，形制上的正规会变成误导。")
    assert "ISAE 3000" in rep, (
        "既然借用了 ISAE 3000 的要素纪律，就应明确说明借用范围与不适用之处。")

    # 不得出现「我们依准则执行了鉴证业务」这类断言
    #
    # ## 判据的对象是**渲染产出**，不是源码；切分单位是**句**，不是行
    #
    # 前两版都失败在同一个地方，值得记下来：
    #
    # 第一版朴素子串匹配整份源码 —— 被自己的免责声明触发（§1.2 写着「不含……
    # 『适用 ISQC 1 质量控制』……声明」，含被禁短语但语义正好相反）。
    #
    # 第二版改逐行扫描 + 跳过含否定标记的行 —— 仍失败，因为源码里那些句子是
    # **跨行折行**的，否定标记（不含 / 未经）落在上一行，被禁短语落在下一行。
    #
    # 第三版换判据对象：该管的是交付给监管的那份产出，不是源码。渲染后折行已
    # 消失，一个句子就是一个连续字符串，按句号切分即可稳定判断语义方向。
    #
    # 这是本仓库第三次踩「凭朴素子串匹配写门禁」的坑（test_53::m03 把节点标签
    # 报成违规、test_54::m09 被披露文本自己触发）。假阳性会训练人忽略告警。
    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")

    negation_markers = ("不是", "未按", "未经", "不得", "不含", "做不出",
                        "无法", "误用", "不构成")
    banned_claims = (
        "按照 ISAE 3000 执行了",
        "我们已按 ISAE 3000",
        "本业务按本准则执行",
        "适用 ISQC 1",
        "遵守 IESBA Code 的独立性",
        "独立鉴证结论",
        "我们的意见是",
        "无保留意见",
    )
    hits = []
    for sentence in re.split(r"[。！\n]", md.read_text(encoding="utf-8")):
        if any(m in sentence for m in negation_markers):
            continue                      # 这句在免责，不是在断言
        for claim in banned_claims:
            if claim in sentence:
                hits.append("%r 出现在：%s" % (claim, sentence.strip()[:70]))
    assert not hits, (
        "样例报告出现了它做不出的鉴证声明：\n  " + "\n  ".join(hits)
        + "\n\n本报告由自有工具自动生成、未经独立执业者鉴证，不得作出上述断言。")


def test_m12_缺失值措辞必须对齐法定枚举且不得泄漏实现词():
    """`None` / `null` / `（无此字段）` 一类实现细节不得出现在正式产出里。

    法定填报模版（ITS (EU) 2024/2956 B_06.01.0050）把「未评估」规定为一个
    **显式枚举取值**（码 3 `Assessment not performed`），而不是空值。数据库的
    null 直接渲染出来，读者无从判断「没有这一列」与「这一列没值」的区别 ——
    而这两件事在审计上完全不同。
    """
    from compliance_export import MISSING_LABEL

    assert "Assessment not performed" in MISSING_LABEL, (
        "MISSING_LABEL 未对齐 ITS B_06.01.0050 的枚举措辞，当前为 %r。"
        % MISSING_LABEL)

    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")
    md_text = md.read_text(encoding="utf-8")

    leaked = [w for w in ("（无此字段）", "| None |", "| null |", "| nan |")
              if w in md_text]
    assert not leaked, (
        "样例出现了实现细节泄漏：%s。缺失值应渲染为 %r。" % (leaked, MISSING_LABEL))

    # 报告必须在术语定义节解释这个取值，否则显式枚举也只是另一个黑话
    assert "## 4 术语与取值定义" in md_text, "缺少术语与取值定义节"
    assert MISSING_LABEL.split("（")[0] in md_text, (
        "术语定义节没有定义缺失值取值的含义。")


def test_m13_表格与章节必须编号():
    """正式文档的表要连续编号并带题注，章节要能被「见 §5.2」引用。

    编号不是装饰：没有编号，审计意见里就无法精确指向某一张表或某一节，
    只能描述「那个讲集中度的表」，而报告一改内容就对不上了。
    """
    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")
    md_text = md.read_text(encoding="utf-8")

    captions = re.findall(r"\*\*表 (\d+)：", md_text)
    assert captions, (
        "样例里没有任何表题注（形如 `**表 1：...**`）。正式文档的表必须编号。")
    nums = [int(n) for n in captions]
    assert nums == list(range(1, len(nums) + 1)), (
        "表编号不连续：%s。编号器应在一次渲染内自成一体。" % nums)

    # 章节编号：至少要有 §1 到 §10 的二级标题
    for n in range(1, 11):
        assert re.search(r"^## %d " % n, md_text, re.M), (
            "缺少编号章节 `## %d `。" % n)


def test_m15_confirmed必须逐手段披露证据范围():
    """`Confirmed` 不能是一个同质标签 —— 手段与适用范围必须落在报告里。

    ## 实测依据

    2026-09-15 的 16 条 confirmed 里：
      4 条 `iam-deny`   注入期间调用全程失败，覆盖「依赖不可用」
      3 条 `rds-reboot` 实例重启，实测中断仅约 **16~18 秒**
      9 条 未记录手段   适用范围不明（早期实验写入）

    此前 `_evidence()` 对全部 confirmed 返回同一句
    `Confirmed — no exceptions noted`。用 SOC 2 这句措辞描述一次 16 秒的重启
    测试，读者会以为该依赖被完整验证过 —— 那是过度声称，而且存
    `verify_severance` 的全部意义就是让报告披露这个差别。

    这条门禁盯三件事：瞬时手段不得用「no exceptions noted」；
    §6 必须存在且写出每种手段**不覆盖**什么；未记录手段的 confirmed
    必须在局限节里被点出数量。
    """
    from compliance_export.report import (
        SEVERANCE_SCOPE, EVIDENCE_CHANNEL_NOTE, _evidence, _note)

    # 1) 瞬时手段不得用「已完整验证」的措辞
    for sev, (_name, scope, transient) in SEVERANCE_SCOPE.items():
        _m, concl = _evidence({"verify_status": "confirmed",
                               "verify_severance": sev})
        if transient:
            assert "no exceptions noted" not in concl, (
                "瞬时手段 %r 的结论用了 SOC 2 的「no exceptions noted」—— "
                "那是「已完整验证」的措辞，会让读者以为覆盖了长时中断。" % sev)
            assert "范围受限" in concl, (
                "瞬时手段 %r 的结论没有标出范围受限。" % sev)
        assert scope, "%r 没有定义证据范围" % sev
        assert ("不覆盖" in scope or "无法判断" in scope), (
            "%r 的范围定义只说了覆盖什么、没说**不**覆盖什么。"
            "监管审查挑的正是「没覆盖到的场景被算作已验证」。" % sev)

    # 2) 瞬时手段与非标准通道必须写进备注列
    n = _note({"verify_status": "confirmed", "verify_severance": "rds-reboot",
               "verify_evidence_channel": "rds-event+business-probe"})
    assert "范围限制" in n, "备注列没有写出瞬时手段的范围限制"
    assert "无调用遥测" in n, "备注列没有披露消费方侧遥测缺失"

    # 3) 报告必须有 §6，且未记录手段的 confirmed 要被点数
    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")
    md_text = md.read_text(encoding="utf-8")
    assert "## 6 切断手段与证据范围" in md_text, "缺少切断手段与证据范围节"
    assert "不覆盖" in md_text, "报告没有任何「不覆盖」的范围声明"
    # 样例里若存在未记录手段的 confirmed，报告必须点出来
    if "`unspecified`" in md_text or "未声明" in md_text:
        assert "未记录切断手段" in md_text, (
            "样例里有未记录手段的 confirmed，报告却没有披露 —— "
            "读者会把它们与实测同等看待。")

    # 4) 两套词汇都要登记，且新旧必须能区分
    assert "throughput_only" in EVIDENCE_CHANNEL_NOTE, (
        "早期词汇 throughput_only 未登记 —— 会被报告标成「未登记通道」，"
        "而它其实是有定义的，只是与本轮词汇不同轴。")
    assert "【退化指标】" in EVIDENCE_CHANNEL_NOTE["throughput_only"], (
        "没有标明早期词汇描述的是「哪个指标退化」而非「哪个观测来源」。"
        "同一字段承载两套不同轴的词汇，不标明就会被误读。")
    assert "【观测来源】" in EVIDENCE_CHANNEL_NOTE["xray-edge+business-probe"], (
        "没有标明本轮词汇描述的是观测来源。")


def test_m14_章节标题不得在页面里硬编码():
    """demo 页面按章节标题切片取局限披露节，该标题必须从包里导入。

    ## 失效形状比「报错」更坏

    页面原先硬编码 `"## 必须随报告一同披露的局限"`。章节改名后，页面的
    `if _marker in _md else "（未生成）"` 分支会**静默显示「（未生成）」**——
    不报错、不缺页、健康检查通过，只有真的展开那一栏的人才看到空白。

    这与部署时 `demo/Dockerfile` 漏 COPY `compliance_export` 是同一失效类型
    （构建成功、启动成功、点开才炸），也与本仓库「同一份清单四处各抄一份、
    其中两处漂移到实际错误」是同一判据类型：**第二份副本必须消除，不是校准。**
    """
    from compliance_export import LIMITATIONS_HEADING

    page = _ROOT / "demo" / "pages" / "10_Compliance_Report.py"
    assert page.exists(), "找不到合规报告页面"
    src = page.read_text(encoding="utf-8")

    assert "LIMITATIONS_HEADING" in src, (
        "页面没有导入 LIMITATIONS_HEADING。章节标题的单一来源是 report.py，"
        "页面不得自己写一份。")

    # 页面里不得出现形如 "## X" 的报告章节标题字面量（st.markdown 的 "### " 小标题
    # 是页面自己的排版，不在此列 —— 判据只拦二级标题，那是报告的章节层级）
    offenders = [line.strip()[:70] for line in src.splitlines()
                 if '"## ' in line or "'## " in line]
    assert not offenders, (
        "页面硬编码了报告章节标题：\n  " + "\n  ".join(offenders)
        + "\n\n请改为从 compliance_export 导入常量。")

    # 常量必须真的出现在渲染产出里，否则导入了也白搭
    md = _SAMPLES / "SAMPLE-compliance-dependency-report.md"
    if not md.exists():
        pytest.skip("样例不存在，由 m08 报错")
    assert LIMITATIONS_HEADING in md.read_text(encoding="utf-8"), (
        "LIMITATIONS_HEADING (%r) 在样例报告里找不到 —— 常量与渲染器已脱节，"
        "页面会静默显示「（未生成）」。" % LIMITATIONS_HEADING)

def test_m16_建模产物分类必须带证据且不得删边():
    """剔除分母会**抬高被考核的覆盖率** —— 所以这条路径要三重约束。

    一个能自己调整分母的指标不是指标。约束：
      1. 声明式白名单，每条必带 evidence（file:line）与 searched（搜过哪些模式）
      2. 绝不删边 —— 只改 verify_status，边仍在图里、仍在总数里
      3. searched 必填：本项目在「搜了不匹配的模式然后相信空结果」上栽过十次，
         最近一次差点删掉一条**真实**依赖（payforadoption -> DynamoDB 经
         guregu/dynamo 的 db.Table 调用，搜 `dynamodb.` 搜不到）
    """
    src = pathlib.Path("scripts/classify_modeling_artifacts.py").read_text(
        encoding="utf-8")
    # 不得有任何删边操作
    for bad in ("drop()", ".remove()", "DELETE ", "detach"):
        assert bad not in src, (
            "分类器里出现删边操作 %r —— 剔出分母必须靠状态标注，"
            "删边会让总数也变，读者无法审计剔了什么" % bad)
    # 证据字段必填由 import 期校验兜住
    assert "_validate()" in src, "没有 import 期校验 —— 没有证据的条目应当写不进表"
    assert "searched" in src and "evidence" in src, "证据字段不完整"
    # 「表里有、图里没有」必须硬失败
    assert "raise SystemExit" in src, (
        "定位不到边时没有硬失败 —— 静默跳过的后果是「我以为标了、其实没标」")
    # 三类语义必须分开，不能一律叫产物
    for name in ("PLATFORM_PULL", "DESIGNED_TO_FAIL"):
        assert name in src, (
            "%s 缺失 —— ECR 拉镜像与「设计成失败」的 S3 调用都是**真边**，"
            "把它们塞进 modeling_artifact 是用分类掩盖问题" % name)


def test_m17_写回成败不得由记日志决定():
    """写入成功后因日志缺键返回 False，会产出与事实相反的合规陈述。

    2026-09-15 实测：源码审计那轮 4 条边全部写成功（图上 modeling_artifact=4、
    confirmed 17→16），而脚本报告「已写回 0 条边」—— 原因是 logger.info 引用了
    两个**可选**键，KeyError 被与写入共用的 except 吞掉。方向是少报，
    但少报同样是错的，而且会诱使调用方重试。
    """
    src = pathlib.Path("chaos/code/runner/edge_verification.py").read_text(
        encoding="utf-8")
    # 切到下一个**顶层** def/class，而不是固定字符数窗口。
    # 第一版用 src[i:i+4000]，而 write_verdict 的 docstring 就超过 4000 字符，
    # 于是 index() 抛 ValueError —— 又一次「切片判据比意图窄」。
    i = src.index("def write_verdict")
    m = re.search(r"\n(?=(?:def |class )\w)", src[i + 1:])
    body = src[i:i + 1 + m.start()] if m else src[i:]
    j = body.index("query_gremlin_parsed(q)")
    # 写入之后到 return True 之间不得再有能抛 KeyError 的必需键下标访问。
    #
    # ⚠️ **先剥注释再断言。** 本会话已第五次栽在同一件事上：判据撞在我自己写的
    # 解释性注释里（这里的注释就必须引用 `v['label']` 才能说明白问题），
    # 于是指着一段解释报违规。同源的前四次：
    #   t73_03  docstring 提到 SERVICE_PROBES → hunk 被误分类，暂存出半个修复
    #   t73_04  '\\ndef ' 找函数体结尾，把紧随的 class 圈进来
    #   t73_10  index() 取到注释里的 import chaos_lock
    #   m17     固定 4000 字符窗口比函数体短
    # 判据要看的是**代码**，不是关于代码的说明。
    seg = body[j:body.index("return True", j)]
    code = "\n".join(ln for ln in seg.splitlines()
                     if not ln.lstrip().startswith("#"))
    for key in ("label", "observer"):
        assert "v['%s']" % key not in code and 'v["%s"]' % key not in code, (
            "写入后仍用必需键下标取可选字段 %r —— 用 v.get()，"
            "或把日志移出写入的 try。那正是 2026-09-15 那次少报的直接原因。" % key)
    # 日志必须有自己的 try：记账失败最多让日志缺一行，不能改变写入的成败
    assert body[j:].count("try:") >= 1, (
        "日志没有独立的 try —— 记账失败会伪装成写回失败")

def test_m18_四类不可确认边必须各自单列():
    """「真实但拿不到 confirmed」有四种成因，混成一类就掩盖了问题。

        modeling_artifact  源码证明调用不存在        → 出可评估分母
        bootstrap_only     只在引导/管理端点上       → 留在分母，永远拿不到 confirmed
        platform_pull      平台行为（kubelet 拉镜像）→ 留在分母
        designed_to_fail   调用会成功但被设计成失败  → 留在分母

    2026-09-17：`payforadoption -> dynamodb` 曾是无手段记录的 confirmed。
    源码显示 DynamoDB 只在 `POST /api/triggerseeding` 上被调用
    （repository.go:499），业务端点无该调用路径；活跃负载下 X-Ray 无 DynamoDB
    出边，且这不是观测缺口（main.go:141 有 otelaws.AppendMiddlewares）。
    把它标成 modeling_artifact 会抹掉一条真实依赖，留着 confirmed 是过度声称。
    """
    from compliance_export.report import _evidence, SEVERANCE_SCOPE
    seen = {}
    for st in ("modeling_artifact", "bootstrap_only"):
        method, wording = _evidence({"verify_status": st})
        assert method == "EXAMINE", (
            "%s 的取证方法应是 EXAMINE（检查）而不是 %s —— "
            "写 TEST 会让读者以为做过故障注入" % (st, method))
        assert wording not in seen, (
            "%s 与 %s 的结论措辞相同 —— 读者无法分辨"
            "「调用不存在」与「调用真实但不在业务路径上」" % (st, seen.get(wording)))
        seen[wording] = st
    # bootstrap_only 的措辞必须说明它是**真实**依赖，否则等同于抹掉它
    _, w = _evidence({"verify_status": "bootstrap_only"})
    assert "真实" in w, (
        "bootstrap_only 的措辞没说明调用真实存在 —— "
        "读者会当成建模产物，那是把一条真实依赖从清单里抹掉")
    assert "source-audit" in SEVERANCE_SCOPE, "源码审计手段未登记范围"

def test_m19_三个轴不许合并到一个字段():
    """一个字段承载两套不同轴的词汇，本项目已付过两次代价。

        verify_status             取到了什么证据
        verify_dependency_class   失效时业务坏到什么程度（hard/degraded/soft）
                                  —— graph_confidence.py:75、:410，**有下游消费者**
                                  （DR 影响面分析；retract_false_soft_verdicts.py
                                  与 reclassify_blocked_edges.py 的存在就是因为
                                  `soft` 曾被错写、必须撤回）
        verify_assessability      为什么（不）能靠切断实验拿到 confirmed

    2026-09-17：我第一版把 platform_pull / designed_to_fail 写进了
    verify_dependency_class —— 往一个有既定词汇且有消费者的字段里塞另一个轴。
    与 verify_evidence_channel 那次同源；上次补救是加前缀，这次改成分字段：
    **加前缀只是让人能分辨，分字段才让消费者不会读错。**
    """
    src = pathlib.Path("scripts/classify_modeling_artifacts.py").read_text(
        encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "write_assessability" in code, "可评估性没有走独立写入路径"
    # 可评估性词汇不得出现在 dependency_class 的赋值处
    import re
    for m in re.finditer(r'"dependency_class":\s*([^,\n]+)', code):
        val = m.group(1).strip()
        assert val in ("None", "v.get('dependency_class')", 'v.get("dependency_class")'), (
            "往 dependency_class 写了 %r —— 那个字段的既定词汇是 "
            "hard/degraded/soft 且被 DR 影响面分析消费，"
            "可评估性词汇必须走 verify_assessability" % val)
    # 写入端与读取端必须成对：写了不取等于没写
    q = pathlib.Path("compliance_export/queries.py").read_text(encoding="utf-8")
    for field in ("verify_assessability", "verify_dependency_class",
                  "verify_severance", "verify_evidence_channel"):
        assert field in q, (
            "%s 写入了但查询没取 —— 与 §6 那次「每条 confirmed 都渲染成同一句」"
            "是同一个缺陷：落在合规产物上的字段，写入端与读取端必须成对出现。"
            % field)

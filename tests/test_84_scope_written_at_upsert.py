"""test_84_scope_written_at_upsert.py — 常量 scope 必须由写入方就地写。

## 这批门禁守什么

`scope` 有两个来源，能力不同（见 `tests/test_48_node_scope.py::t306_11` 的
docstring）：

  · **写入方就地写** —— 值不依赖任何外部查询时，upsert 时手上就有。
  · **对账脚本定期补** —— 需要查 CloudFormation 栈归属的，只能由
    `scripts/label_node_scope.py` 周期性补。

契约 `node_scope.type_map` 列出的类型属于**第一类**：它们的 scope 是
按节点类型就确定的常量，注释写得很直白 ——
「平台自己产出的分析件：按构造即 platform，**无需查任何外部系统**」、
「AWS 托管服务端点：按定义就是被观测系统之外的 AWS 服务」。

## 为什么需要这道门禁

2026-09-21 实测：`TopologyChange` / `Incident` / `AWSServiceEndpoint` 三类
节点的写入方都**没写** scope，于是 `test_48::t306_11` 把它们全列为
「缺 scope 且超出宽限窗口」。那道门禁自己给出了判据：

    若某类节点反复出现，说明它的写入方该在 upsert 时就写 scope

`TopologyChange` 18 个、`Incident` 18 个 —— 正是「反复出现」。

t306_11 是**活图谱**核验（需 `GRAPH_LIVE_AUDIT=true`，本地与 CI 默认 skip），
所以它发现不了「代码里又漏写了」这件事，只能在有 Neptune 的环境里事后报警。
本文件是它的**静态对应物**：纯读源码、不连 Neptune，因此能在离线 CI 里跑，
在代码合并前就挡住回归。

⚠️ 写入方修复只对**新建**节点生效（`onCreate` / `ON CREATE SET`）。
历史遗留节点仍需 `scripts/label_node_scope.py --apply` 补一次。
这两件事不可互相替代：只补历史、不修写入方，下一轮 ETL 又会产出缺 scope
的节点；只修写入方、不补历史，t306_11 会一直红。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 每项：(说明, 源文件, 节点标签, 该文件里必须出现的 scope 字面量)
#:
#: 刻意写成「文件 + 期望值」而不是去解析 Gremlin/Cypher 语法 —— 解析器会比
#: 被测代码更容易出错，而这里要守的事实很简单：这个写入点有没有带上那个常量。
#:
#: ⚠️ 2026-09-21 更正：`Incident` 一项原先指向
#: `infra/lambda/rca_window_flush/actions/incident_writer.py` —— **那是构建产物，
#: 不是权威源**。`build.sh` 会 `find $DEST_DIR -delete` 后从 `rca/` 单向重拷
#: （见该脚本第 14/29/36 行），所以打在产物上的修复下次构建即被抹掉，
#: 而门禁守着产物会一直是绿的。
#:
#: 这正是本文件要防的那类错误，却在本文件自己身上发生了一次。
#: 现改为守 `rca/actions/incident_writer.py`，并由 t84_04 断言产物与源一致。
WRITERS = [
    (
        "DeepFlow ETL 写 TopologyChange",
        "infra/lambda/etl_deepflow/neptune_etl_deepflow.py",
        "TopologyChange",
        "platform",
    ),
    (
        "RCA window flush 写 Incident（权威源）",
        "rca/actions/incident_writer.py",
        "Incident",
        "platform",
    ),
    (
        "X-Ray ETL 写 AWSServiceEndpoint",
        "infra/lambda/etl_xray/neptune_etl_xray.py",
        "AWSServiceEndpoint",
        "external",
    ),
    (
        "AppSignals ETL 写 AWSServiceEndpoint",
        "infra/lambda/etl_appsignals/neptune_etl_appsignals.py",
        "AWSServiceEndpoint",
        "external",
    ),
]

#: `rca/` 下的权威源 → `infra/lambda/rca_window_flush/` 下的构建产物。
#: build.sh 从前者单向拷到后者，所以两者必须一致；不一致意味着有人改了产物。
BUILD_ARTIFACT_PAIRS = [
    ("rca/actions/incident_writer.py",
     "infra/lambda/rca_window_flush/actions/incident_writer.py"),
]


@pytest.mark.parametrize(
    "desc,rel,label,expected",
    WRITERS,
    ids=[w[0] for w in WRITERS],
)
def test_t84_01_常量scope必须在写入点就地写(desc, rel, label, expected):
    """每个写入点都必须带上契约规定的 scope 常量。"""
    p = ROOT / rel
    assert p.exists(), f"{desc}: 源文件不存在 {rel} —— 文件被移动了就要同步改本门禁"
    src = p.read_text(encoding="utf-8")

    assert label in src, (
        f"{desc}: 源文件里找不到节点标签 {label!r}。"
        f"写入点可能已挪走，本门禁的定位失效了 —— 请更新 WRITERS 而不是删掉这条。")

    # 允许 'scope': 'x' / .property(single,'scope','x') / inc.scope = 'x' 三种写法
    pat = re.compile(
        r"""(?:['"]scope['"]\s*:\s*|['"]scope['"]\s*,\s*|\.scope\s*=\s*)['"]"""
        + re.escape(expected)
        + r"""['"]""")
    assert pat.search(src), (
        f"{desc}: 没有在写入时写 scope={expected!r}。\n"
        f"契约 node_scope.type_map 把 {label} 定为 {expected!r}，"
        f"这个值不依赖任何外部查询，写入方手上就有 —— 不该留给 "
        f"scripts/label_node_scope.py 事后补。\n"
        f"不写的后果：tests/test_48_node_scope.py::test_t306_11 会把每个新建的 "
        f"{label} 都列为「缺 scope 且超出宽限窗口」。")


def test_t84_02_期望值必须与契约一致():
    """本文件写死的 scope 期望值必须来自契约，不能各自漂移。

    这道断言守的是**门禁自己**：如果契约改了 type_map 而本文件的期望值没跟上，
    上面那批断言就会要求写入方写一个契约不认的值 —— 门禁反过来制造缺陷。
    """
    import yaml

    contract = yaml.safe_load(
        (ROOT / "profiles/graph_contract.yaml").read_text(encoding="utf-8"))
    type_map = (contract.get("node_scope") or {}).get("type_map") or {}
    assert type_map, "契约里读不到 node_scope.type_map —— 结构变了就要同步改本门禁"

    for desc, _rel, label, expected in WRITERS:
        assert type_map.get(label) == expected, (
            f"{desc}: 本门禁期望 {label}={expected!r}，"
            f"但契约 type_map 说是 {type_map.get(label)!r}。"
            f"契约是权威，请改本文件的 WRITERS。")


def test_t84_03_契约里的常量scope类型都该有人负责():
    """契约 type_map 列出的类型，凡是图里真会写入的，都该在 WRITERS 里有一条。

    这条是**提醒而非硬门禁**：type_map 里有些类型（如 Region /
    AvailabilityZone）由 bootstrap 一次性写入，不走 ETL 热路径。
    所以断言只要求「本文件覆盖的那几类确实在契约里」，
    并把未覆盖的类型列出来供人判断，不直接判失败 ——
    否则每次契约新增一个类型，这道门禁都会红，而
    「反复闪红的门禁会被无视」（t306_11 docstring 的原话）。
    """
    import yaml

    contract = yaml.safe_load(
        (ROOT / "profiles/graph_contract.yaml").read_text(encoding="utf-8"))
    type_map = (contract.get("node_scope") or {}).get("type_map") or {}
    covered = {w[2] for w in WRITERS}
    uncovered = sorted(set(type_map) - covered)

    # 不判失败，只保证信息可见（-s 或失败时可读）
    print(f"\n  本门禁已覆盖的常量 scope 类型: {sorted(covered)}")
    print(f"  契约里其余常量 scope 类型（多为 bootstrap 一次性写入，"
          f"未走 ETL 热路径）: {uncovered}")
    assert covered <= set(type_map), (
        f"这些类型不在契约 type_map 里: {sorted(covered - set(type_map))}")


@pytest.mark.parametrize(
    "src_rel,artifact_rel", BUILD_ARTIFACT_PAIRS,
    ids=[p[0].split("/")[-1] for p in BUILD_ARTIFACT_PAIRS])
def test_t84_04_构建产物必须与权威源一致(src_rel: str, artifact_rel: str):
    """`infra/lambda/rca_window_flush/` 下的文件是 build.sh 从 `rca/` 拷来的产物。

    ## 为什么需要这条

    `infra/lambda/rca_window_flush/build.sh` 的流程是
    `find "$DEST_DIR" -mindepth 1 -delete` 然后从 `$RCA_DIR`（= `rca/`）重拷，
    而 `DEST_DIR` 默认就是那个**被 git 跟踪的目录**。所以：

      · 改产物不改源 → 下次 `bash build.sh` 抹掉改动
      · 而如果测试守的是产物，它会一直绿，改动消失也没人知道

    2026-09-21 这件事真的发生了：`scope='platform'` 的修复被打在产物上，
    本文件的 t84_01 又恰好指向产物，于是门禁绿着、源里没有修复。
    修正后由本条断言两者一致，让「只改了产物」这件事本身可被发现。

    ⚠️ 这条比对的是**字节一致**。若将来 build.sh 开始做转换（改写 import
    路径、注入版本号等），这条会失败 —— 那时应当改为比对语义或只比关键片段，
    而不是删掉它。
    """
    src = ROOT / src_rel
    artifact = ROOT / artifact_rel
    assert src.exists(), f"权威源不存在: {src_rel}"
    if not artifact.exists():
        pytest.skip(f"构建产物不存在（尚未构建过）: {artifact_rel}")

    s = src.read_text(encoding="utf-8")
    a = artifact.read_text(encoding="utf-8")
    assert s == a, (
        f"构建产物与权威源不一致：\n"
        f"  源:   {src_rel} ({len(s)} 字节)\n"
        f"  产物: {artifact_rel} ({len(a)} 字节)\n\n"
        f"build.sh 从源单向重拷到产物，所以产物里多出来的改动会在下次构建时丢失。\n"
        f"若改动是你想要的，请把它搬到源；若源是新的，跑一次 "
        f"`DEST_DIR=$KIROCREW_SCRATCH/wf-build bash "
        f"infra/lambda/rca_window_flush/build.sh` 重建产物。")

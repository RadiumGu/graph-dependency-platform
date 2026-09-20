"""Layer2 的 probe_neptune 必须真的能查到图谱，不能永远 ImportError。

## 这组门禁守的是什么

2026-09-20 从**线上日志**里发现，`probe_neptune` 一直是这样：

    | ⚠️ probe_neptune | ERROR | +0 | ImportError — NeptuneGraphManager missing |

它两条路径都不通：

  · 主路径 `from runner.neptune_helpers import query_topology` —— `runner/` 是
    chaos 模块的代码，**不在任何 rca 部署包清单里**（build.sh 只复制
    core/neptune/actions/collectors/data/search/engines），在 Lambda 里必然
    ImportError；而且 rca 的 collector 依赖 chaos 的 runner 是方向错误的耦合。
  · fallback `from neptune.neptune_queries import NeptuneGraphManager` ——
    那个类**整个仓库里不存在**，`# type: ignore` 把警告一并压掉了。

与 `classify_group` / `analyze_group` 同一形状：调用不存在的符号、靠 except
兜住。区别是它的失败被如实记成 ERROR 且 +0 分，所以线上日志里查得到 ——
这也是它最终被发现的唯一原因。

## 为什么门禁要断言"查得到数据"而不只是"不抛异常"

原实现也不抛异常（except 兜住了），照样能通过任何"不崩就算过"的测试。
所以这里必须断言**拿到了真实字段**，否则门禁本身就是摆设。
"""
import json
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RCA = ROOT / "rca"
for p in (str(RCA), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _probe(svc):
    from collectors import layer2_tools as lt
    return json.loads(lt.probe_neptune(svc))


# ─────────────────────────────────────────────────────────────
# T82-01: 不得再引用不存在的符号
# ─────────────────────────────────────────────────────────────

def test_t82_01_不得引用不存在的NeptuneGraphManager():
    """源码里不得再出现 NeptuneGraphManager —— 它从来不存在。

    这条是静态门禁：即使运行时被 except 兜住、测试照绿，只要这个名字还在，
    那条代码路径就仍然是死的。
    """
    src = (RCA / "collectors" / "layer2_tools.py").read_text(encoding="utf-8")
    # 允许出现在解释历史的注释里，但不得出现在 import 语句中
    bad = [ln for ln in src.split("\n")
           if "NeptuneGraphManager" in ln and ln.strip().startswith(("from ", "import "))]
    assert not bad, (
        "layer2_tools 仍在 import 不存在的 NeptuneGraphManager：\n  "
        + "\n  ".join(bad)
    )


def test_t82_02_不得依赖不在部署包里的runner模块():
    """rca 的 collector 不得 import chaos 的 `runner.*`。

    `runner/` 不在任何 rca 部署包清单里（build.sh 的复制清单是
    core/neptune/actions/collectors/data/search/engines），所以这类 import
    在 Lambda 里必然 ImportError —— 本地跑得通、线上必挂，是最难发现的一类。
    """
    src = (RCA / "collectors" / "layer2_tools.py").read_text(encoding="utf-8")
    bad = [ln for ln in src.split("\n")
           if ln.strip().startswith(("from runner", "import runner"))]
    assert not bad, (
        "rca/collectors 不得依赖 chaos 的 runner/（它不在部署包里）：\n  "
        + "\n  ".join(bad)
    )


# ─────────────────────────────────────────────────────────────
# T82-03: 必须真的查到数据（不只是"不抛异常"）
# ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    not __import__("os").environ.get("NEPTUNE_ENDPOINT"),
    reason="需要 NEPTUNE_ENDPOINT（活图谱核验）",
)
def test_t82_03_必须查到真实拓扑数据():
    """petsite 在图谱里，探针必须返回 tier 与依赖 —— 不能是 None/unknown。

    原实现"不抛异常"但永远返回 topology=None，任何只检查
    "没有异常" 的测试都会放过它。所以这里断言**字段有值**。
    """
    out = _probe("petsite")

    assert not out.get("error"), f"探针不应报错：{out.get('error')}"
    topo = out.get("topology")
    assert topo, f"必须返回 topology，实得 {out}"
    assert topo.get("tier") and topo["tier"] != "unknown", (
        f"petsite 在图谱里是 Tier0，tier 不该是 {topo.get('tier')!r}。"
        "（原实现取的是图谱里不存在的 'tier' 键，即使类存在也永远是 unknown。）"
    )
    assert topo.get("deps"), f"petsite 有上游依赖，deps 不该为空：{topo}"
    assert topo.get("dep_count") == len(topo["deps"])


@pytest.mark.skipif(
    not __import__("os").environ.get("NEPTUNE_ENDPOINT"),
    reason="需要 NEPTUNE_ENDPOINT（活图谱核验）",
)
def test_t82_04_图谱里没有的服务要说出来而不是装作无异常():
    """服务不在图谱里是有意义的发现，必须留下痕迹。

    若只返回 topology=None 且 error=None，调用方会把「查不到」误读成
    「查过了、没异常」—— 那是比报错更糟的结果。
    """
    out = _probe("definitely-not-a-real-service-xyz")

    assert out.get("note"), (
        "服务不在图谱中时必须给出 note，否则 topology 为空会被误读成「无异常」。"
        f"实得 {out}"
    )
    assert "不在图谱" in out["note"], out["note"]

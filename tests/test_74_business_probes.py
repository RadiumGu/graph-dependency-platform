"""业务功能探针注册表的门禁。

## 为什么这些断言值得单独立一个文件

探针是切断实验的**业务侧证据来源**。探针错了不会报错，会安静地产出一个
错误的结论并写进合规报告 —— 2026-09-13 实测到两个各自独立的缺陷，
两个都会把「前置条件不满足」伪装成「业务功能坏了」，
进而让实验写出假 `confirmed`：

1. `probe_adopt` 取 `ids[0]`，而那只宠物**可能已被领养**（`ps-unavailable`）。
   此时支付返回 HTTP 200 但只渲染表单页，探针报 `value=0`，
   而当时另外 25 只宠物都能正常领养。
2. `pettype` 硬编码 `"puppy"`，只是碰巧第一只是狗才一直有效。
   实测 26 只里有 7 只 kitten、4 只 bunny，类型不符时支付必然不成功。

假 `confirmed` 比 `未评估` 危险得多：它会被 DR 影响面分析当成结论。
所以这里的断言盯的都是「**能区分前置失败与业务失败**」这一件事。
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "chaos" / "code" / "runner" / "business_probes.py"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def _body_of(src: str, decl: str) -> str:
    """取一个顶层定义的源码体，切到下一个**顶层** def/class。

    只找 `\\ndef ` 会把紧随其后的 class 整个圈进来（它的方法是缩进的
    `    def `，匹配不到），于是类里无关的代码触发断言 ——
    本项目已经因为这个切法出过一次门禁假阳性。
    """
    i = src.index(decl)
    m = re.search(r"\n(?:def |class )", src[i + len(decl):])
    return src[i:i + len(decl) + (m.start() if m else len(src))]


def test_t74_01_注册表必须声明式且探针按源服务分组():
    """`SERVICE_PROBES` 必须是一张可审计的表，不是散在代码里的分支。

    探针 = **源服务提供的业务功能**。命题是「这个服务失去这个依赖后，
    **它的**业务输出坏不坏」，所以探针必须按源服务分组，
    而不是按被切断的目标类型。
    """
    src = _src()
    assert "SERVICE_PROBES" in src, "缺少声明式探针注册表"

    from runner.business_probes import SERVICE_PROBES, probes_for
    assert isinstance(SERVICE_PROBES, dict) and SERVICE_PROBES, "注册表为空"
    for svc, probes in SERVICE_PROBES.items():
        assert isinstance(probes, tuple) and probes, "%s 的探针组为空" % svc
        for item in probes:
            assert isinstance(item, tuple) and len(item) == 2, (
                "%s 的探针项必须是 (名字, 函数) 二元组" % svc)
            name, fn = item
            assert isinstance(name, str) and name, "%s 有探针缺名字" % svc
            assert callable(fn), "%s 的探针 %s 不可调用" % (svc, name)

    # petsite 必须登记多个探针：某条依赖可能只坏其中一个功能
    # （SQS 断了领养挂、首页照常）。只登记一个会把真实影响判成无影响。
    assert len(SERVICE_PROBES.get("petsite", ())) >= 3, (
        "petsite 只登记了少于 3 个探针。它对外提供搜索/领养/列表/问答多个功能，"
        "某条依赖可能只坏其中一个 —— 探针不全会漏掉真实影响。")

    assert probes_for("不存在的服务") == (), (
        "probes_for 对未登记服务没有返回空 —— 调用方要靠它拒绝出 confirmed。")


def test_t74_02_探针必须区分前置失败与业务失败():
    """`ok=False`（探针/前置失败）与 `value=0`（业务失败）不能混。

    混掉的后果是双向的：前置失败被读成业务失败 → 假 confirmed；
    业务失败被读成探针失败 → 真实影响被漏掉。
    """
    src = _src()
    for fn in ("probe_home", "probe_adopt", "probe_list",
               "probe_history", "probe_waggle"):
        body = _body_of(src, "def %s" % fn)
        assert '"ok"' in body, "%s 没有返回 ok 字段" % fn
        assert '"value"' in body, "%s 没有返回 value 字段" % fn
        assert '"detail"' in body, (
            "%s 没有返回 detail —— 判定为什么这样出必须能复核。" % fn)
        # 前置失败必须走 ok=False + value=None，不能写成 value=0
        assert '"ok": False' in body, (
            "%s 没有 ok=False 的出口。任何探针都可能遇到前置条件不满足，"
            "把它写成 value=0 就是把「测不出」伪装成「坏了」。" % fn)


def test_t74_03_领养探针必须挑可用宠物且用真实类型():
    """这条门禁对应两个实测缺陷，见模块 docstring。"""
    src = _src()
    body = _body_of(src, "def probe_adopt")

    assert "parse_pets" in body, (
        "probe_adopt 没有解析宠物卡片。取 ids[0] 会挑到已被领养的宠物，"
        "支付只会渲染表单页，探针于是报出一个不存在的业务故障。")
    assert '"available"' in body or "available" in body, (
        "probe_adopt 没有按可用性筛选宠物。")
    assert '"pettype": "puppy"' not in body and "'pettype': 'puppy'" not in body, (
        "probe_adopt 仍硬编码 pettype=puppy。实测 26 只里 7 只 kitten、"
        "4 只 bunny，类型不符时支付必然不成功 —— 那是参数错，不是业务坏。")
    assert 'pet["pettype"]' in body, "probe_adopt 没有使用宠物自己的类型"

    # 一只都不可用时必须是前置失败，不能报 value=0
    assert "无可领养对象" in body or '"ok": False' in body, (
        "没有可用宠物时必须返回 ok=False（前置条件不满足），"
        "而不是 value=0（领养功能坏了）。")


def test_t74_04_解析器必须按卡片切分而不是整页findall():
    """三个独立的 findall 会静默错位，必须按卡片把三个字段绑到同一只宠物。"""
    src = _src()
    body = _body_of(src, "def parse_pets")
    assert "split" in body, (
        "parse_pets 没有按卡片切分。整页三次 findall 得到三个等长列表，"
        "某只宠物缺一个字段就整体错位，而错位是静默的。")
    # 可用性判断必须限定在卡片自己的范围内
    assert "[:1200]" in body or "seg" in body, (
        "可用性判断没有限定在卡片自己的片段内 —— 整页搜索会让一只不可用的"
        "宠物污染它后面所有卡片的判断。")

    from runner.business_probes import parse_pets
    # 两张卡片：第一张不可用的 bunny，第二张可用的 puppy
    html = (
        '<div class="ps-cardbody"><a aria-label="View full size photo of '
        'brown bunny"></a><span class="ps-petid">Pet #001</span>'
        '<span class="ps-unavailable">Unavailable</span></div>'
        '<div class="ps-cardbody"><a aria-label="View full size photo of '
        'black puppy"></a><span class="ps-petid">Pet #002</span>'
        '<button class="pet-button">adopt</button></div>')
    pets = parse_pets(html)
    assert len(pets) == 2, "解析出 %d 只，应为 2 只" % len(pets)
    assert pets[0] == {"id": "001", "pettype": "bunny", "available": False}, pets[0]
    assert pets[1] == {"id": "002", "pettype": "puppy", "available": True}, pets[1]


def test_t74_05_waggle探针必须看回答内容而不是状态码():
    """agent 依赖的唯一可行观测通道是回答内容。

    HTTP 调用无论委派成功与否都返回 200 —— 只看状态码测不出 AgentRuntime
    被切断，会把「agent 完全不工作」判成「业务正常」。
    """
    src = _src()
    body = _body_of(src, "def probe_waggle")
    assert "200" not in body.split('"value"')[0] or "兜底" in body or "fallback" in body.lower(), (
        "probe_waggle 似乎只看状态码。委派失败时 HTTP 仍是 200，"
        "必须检查回答内容（是否是兜底文案）。")

def test_t74_11_waggle判据必须拒绝结构化错误体():
    """200 + 有内容 ≠ 业务正常 —— 实测被判为健康的一个硬错误。

    petsite 把 AgentCore 的错误体原样透传成 HTTP 200：

        {"error": "Agent is already processing a request. ...",
         "error_type": "ConcurrencyException",
         "message": "An error occurred during streaming"}

    180 字符、不含任何已知兜底文案，于是旧判据
    （`st == 200 and len >= 40 and not fallback`）判为 `value=1`。

    这一条尤其危险：`SERVICE_PROBES` 里三个 AgentRuntime 服务把 `probe_waggle`
    当作**唯一**证据通道，判据有洞就会在合规产物上落下「依赖完好」的假结论。
    """
    src = pathlib.Path("chaos/code/runner/business_probes.py").read_text(
        encoding="utf-8")
    body = _body_of(src, "def probe_waggle")
    assert "error_type" in body, (
        "probe_waggle 没有识别结构化错误体 —— ConcurrencyException 会被判为健康")
    assert "json.loads" in body, "没有真的解析应答体，只靠子串匹配会漏判"
    # 空流与 ServiceException 两条兜底也必须在判据里
    for marker in ("couldn't generate a response", "service is currently busy"):
        assert marker in body, (
            "probe_waggle 漏判兜底文案 %r —— petsite 有三条兜底路径，"
            "只认其中一条会把另两条判为健康。" % marker)


def test_t74_12_waggle会话必须每次独立():
    """固定 SessionId 会与并发调用方互撞，实测 5 连发全部 ConcurrencyException。

    AgentCore 按 `runtimeSessionId` 串行。原实现用常量 `chaos-probe-xxxx…`，
    于是任意两个并发使用本探针的人共用一个会话、互相顶掉 —— 而那个错误体
    又恰好被旧判据判为健康，两个缺陷叠起来就是一个静默的假阳性。
    """
    src = pathlib.Path("chaos/code/runner/business_probes.py").read_text(
        encoding="utf-8")
    body = _body_of(src, "def probe_waggle")
    assert '"x" * 40' not in body, "SessionId 仍是常量 —— 会与并发调用方互撞"
    assert "uuid" in body, "SessionId 不是每次调用独立生成"
    assert "import uuid" in src, "用了 uuid 但没导入 —— 运行时 NameError"

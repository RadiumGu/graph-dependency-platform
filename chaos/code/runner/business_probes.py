"""业务功能探针注册表 —— 每条被测边匹配它所支撑的业务功能。

## 为什么必须按业务功能分，而不是一个通用探针

2026-09-13 实测的教训链：

第一版只有一个探针（petsite 首页还能返回多少宠物）。它对 `petsearch ->
DynamoDBTable` 是对的 —— 首页确实靠它。但 28 条待验边里**只有 4 条在搜索路径
上**。拿首页探针去测 `petsite -PublishesTo-> SQSQueue`，会得到「注入生效
（边成功率归零）但业务正常（首页照常 26 个宠物）」→ 判定写成
`inconclusive: 消费方存在降级路径`。

那个结论是**假的**：SQS 断了确实不影响首页，但它影响领养提交。用错探针不是
「测不出」，是**得出一个反向的结论**并写进合规报告。

## 判据：探针 = 源服务提供的业务功能

被验证的命题是「这个服务失去这个依赖后，**它的**业务输出坏不坏」。所以探针由
**源服务**决定，与目标类型无关：

    petsite            入口服务，同时提供 4 种功能 → 4 个探针全跑
    payforadoption     领养支付      → adopt
    petlistadoptions   领养列表      → list
    pethistory         领养历史      → history
    petsearch          宠物搜索      → home

petsite 必须跑全部四个：它的某条依赖可能只坏其中一个功能（SQS 断了领养挂、
首页照常）。只跑首页会漏掉，且漏掉的形状是「业务正常」——一个假结论。

## 每个探针必须有稳定的基线

判定靠「基线稳定 → 故障期退化」。基线本身抖动的探针不能用 ——
2026-09-13 实测首页探针稳态 0/40 归零，可用；其余探针在首次使用前
必须同样测出基线假阳性率，否则它报出的退化无法与噪声区分。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PETSITE_URL = os.environ.get(
    "PETSITE_INTERNAL_URL",
    "http://internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com")

_UA = {"User-Agent": "kirocrew-business-probe/1.0"}
_PETID_RE = re.compile(r'class="ps-petid">Pet #([^<]+)<')
#: 取 <img src>。刻意用**非贪婪**并只到下一个引号 —— 页面里 S3 预签名 URL 很长
#: 且含 & 转义，贪婪匹配会把相邻多个 img 连成一条。
_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')

#: 首页每张卡片的起始标记。按它切开才能把「宠物 id」「类型」「可用性」
#: 三件事归到同一只宠物上 —— 三个独立的 findall 只能得到三个等长列表，
#: 一旦某只宠物缺了某个字段，三者就错位，而错位是静默的。
_CARD_SPLIT_RE = re.compile(r'<div class="ps-cardbody">')

#: 宠物类型从图片的 aria-label 取（`View full size photo of brown bunny`）。
#: **不能硬编码 `puppy`**：实测 26 只里有 15 只 puppy、7 只 kitten、4 只 bunny，
#: 而 `/Payment/MakePayment` 的 pettype 必须与宠物实际类型一致，否则支付不成功。
_TYPE_RE = re.compile(r'aria-label="View full size photo of ([a-z]+ )?([a-z]+)"')

#: 已被领养的卡片没有提交按钮，只有这个标记。
_UNAVAIL_MARK = 'ps-unavailable'

#: 探针专用 userId 前缀。与合成流量 cron 的 userId 分开 ——
#: 两者同时跑时不能互相污染状态（cron 会 housekeeping 清理它自己的 userId）。
_UID = "chaos-probe"

#: 领养探针最多连试几只宠物。>1 是因为单只宠物可能在两次请求之间被别人领走
#: （合成流量 cron、压测 TGB、另一个实验同时在跑），一只失败不足以断言功能坏。
_ADOPT_TRIES = 3


def parse_pets(html: str) -> list[dict]:
    """把首页 HTML 解析成 `[{"id", "pettype", "available"}, …]`。

    ## 为什么必须解析可用性和类型，而不是取第一个 petId

    2026-09-13 实测两个各自独立、都会产出**假业务故障**的缺陷：

    1. `probe_adopt` 原先取 `ids[0]`，而那只宠物**可能已被领养**
       （卡片上是 `ps-unavailable`，没有提交按钮）。此时支付返回 HTTP 200
       但只是渲染领养表单页（10,117 字符，无成功标记），探针于是报
       `value=0` = 「领养功能坏了」—— 而当时另外 25 只都能正常领养。
    2. `pettype` 原先硬编码 `"puppy"`，只是碰巧 `ids[0]` 是狗才一直有效。
       实测 025/026 是 bunny，用 `puppy` 提交必然不成功；用真实类型
       立刻成功（11,172 字符）。

    两者都会让判定逻辑读到「业务退化」并写出**假 confirmed** ——
    比未评估危险得多，因为它会被 DR 影响面分析当成结论。

    可用性判断只看卡片**前段**：`ps-unavailable` 只出现在卡片自己的
    cardfoot 里，但整页搜索会让一只不可用的宠物污染它后面所有的卡片。
    """
    pets: list[dict] = []
    for chunk in _CARD_SPLIT_RE.split(html)[1:]:
        m = _PETID_RE.search(chunk)
        if not m:
            continue
        seg = chunk[:1200]
        tm = _TYPE_RE.search(chunk) or _TYPE_RE.search(html)
        pets.append({
            "id": m.group(1).strip(),
            # aria-label 形如「black puppy」，类型是最后一个词（颜色在前）。
            "pettype": (tm.group(2) if tm else "puppy"),
            "available": _UNAVAIL_MARK not in seg,
        })
    return pets


def _get(path: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(PETSITE_URL + path, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post(path: str, fields: dict, timeout: int = 45) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        PETSITE_URL + path, data=body, headers={
            **_UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ── 各业务功能的探针 ────────────────────────────────────────────────────────
#
# 约定：返回 dict，至少含
#   ok      本次采集是否成功（False 表示采不到，**不表示业务坏了**）
#   value   业务输出的可比数值（越大越好）
#   detail  人类可读细节
# `ok=False` 与 `value=0` 必须区分 —— 前者是探针失败，后者是业务失败。
# 这两者合并就是本仓库反复踩的那个坑（metrics fallback 成 100%/0 requests）。


def probe_home() -> dict:
    """宠物搜索：首页能列出多少宠物。覆盖 petsearch 与 petsite→petsearch。"""
    try:
        st, html = _get("/?userId=%s-home" % _UID)
    except Exception as e:
        return {"ok": False, "value": None, "detail": "请求失败 %r" % e}
    if st != 200:
        return {"ok": False, "value": None, "detail": "HTTP %d" % st}
    n = len(_PETID_RE.findall(html))
    return {"ok": True, "value": n, "detail": "首页宠物 %d 个" % n}


def probe_pet_images(sample: int = 3) -> dict:
    """宠物图片能不能真的取到 —— 覆盖 **S3 的数据路径**。

    ## 为什么需要一个单独的探针

    `probe_home` 数的是首页 HTML 里的宠物条目数（稳定在 26）。**S3 全断它也不动**
    —— 因为图片是 `<img src>` 指向 S3 的**预签名 URL**，页面照样渲染得出，
    只是图挂掉。也就是说现有探针对「S3 不可用」完全不敏感。

    ## 为什么边级成功率通道也用不了

    2026-09-17 实测 `petsearch -> S3` 这条边：

        X-Ray 近 15min: total=634  ok=0  err=634  fault=0
        解码 trace: S3 op=CreateBucket status=409 BucketAlreadyOwnedByYouException

    成功率**恒为 0%**，`verify_via_iam_deny` 的基线闸门因此正确拒绝开跑
    （拿 0% 的基线算退化 delta 无意义）。该边已标 `precondition_unmet`。

    所以要验 S3 这一层，只剩业务通道 —— 而业务通道必须**真的去取图**。

    ## 判据：预签名 URL 的 GET 结果，不是页面里有没有 URL

    实测首页有 28 个 `<img src=...s3...>`，全部带
    `X-Amz-Signature` / `X-Amz-Security-Token` / `X-Amz-Expires=300`。
    预签名是本地密码学操作、**不调用 S3 API**，所以：

      - 签发这些 URL **不需要** S3 可达，页面永远能渲染出来；
      - 浏览器（或本探针）拿 URL 去 GET 时，才第一次真正命中 S3，
        且**按签名者的权限求值** —— 这一步才是 S3 依赖的真实断点。

    ⚠️ `X-Amz-Expires=300`：URL 只有 5 分钟有效。**必须每轮重新抓页面取新 URL**，
    不能缓存 —— 否则过期的 403 会被误读成 S3 故障。

    ## 一个已知的归属问题（读结果时要当心）

    `petsearch` 的 IRSA 角色对该桶**只有 `s3:CreateBucket`**（实查
    `ServicesEks2-searchserviceServiceAccountRole588AF64-...`，无 GetObject）。
    所以对 petsearch 加 `deny s3:*` 很可能**不会**让本探针退化 ——
    它断掉的只是那个永远 409 的建桶调用。

    那种情况下正确结论是「`petsearch -> S3` 是**真实但不承重**的依赖」，
    **不是**「这条边不存在」—— 该边有观测（X-Ray 看得见），
    按本项目纪律，注入后调用方无反应对**有观测**的边只能推出 soft dependency。

    本探针真正的用武之地是 `petsite -> S3` 那条（真实数据路径）。

    返回值 `value` = 成功取到的图片数（0 表示 S3 数据路径断了）。
    """
    try:
        st, html = _get("/?userId=%s-img" % _UID)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "value": None, "detail": "抓页面失败 %r" % e}
    if st != 200:
        return {"ok": False, "value": None, "detail": "抓页面 HTTP %d" % st}

    # 只取指向 S3 且带签名的 URL。不带签名的（占位图、CDN）不算 S3 数据路径。
    urls = [u for u in _IMG_SRC_RE.findall(html)
            if "amazonaws.com" in u and "X-Amz-Signature" in u]
    if not urls:
        return {"ok": False, "value": None,
                "detail": "页面里没有预签名的 S3 图片 URL —— "
                          "可能是页面结构变了，或图片改走别的通道；"
                          "先查清再把这解释成 S3 故障"}

    # HTML 属性里的 & 是转义过的，取 URL 前必须还原，否则签名参数名会带 amp; 前缀。
    import html as _htmlmod
    picked = [_htmlmod.unescape(u) for u in urls[:max(1, int(sample))]]

    ok_n, detail = 0, []
    for u in picked:
        try:
            req = urllib.request.Request(u, headers=_UA, method="GET")
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read(2048)          # 只读头部几 KB，够判成功
                if r.status == 200 and body:
                    ok_n += 1
                else:
                    detail.append("HTTP %d/%dB" % (r.status, len(body)))
        except urllib.error.HTTPError as e:
            detail.append("HTTP %d" % e.code)
        except Exception as e:                          # noqa: BLE001
            detail.append(repr(e)[:40])

    return {"ok": True, "value": ok_n,
            "detail": "预签名图片取到 %d/%d 个%s"
                      % (ok_n, len(picked),
                         ("（失败: %s）" % ", ".join(detail[:3])) if detail else "")}


def probe_adopt(pet: dict | None = None) -> dict:
    """领养支付：能否完成一次领养提交。

    覆盖 payforadoption、SQSQueue、StepFunction、以及 petsite 侧的
    DynamoDBTable —— 支付链路会写这些。

    ## `pet` 参数：把支付路径与发现路径隔离开

    默认从首页现取宠物。但**首页本身调 petsearch** —— 验证
    `payforadoption -> Microservice(petsearch)` 这条边时，一旦切断 petsearch，
    探针会卡在「取不到 petId」这个前置条件上返回 `ok=False`，
    于是**测不到支付本身是否还能成**，而那正是要验证的东西。

    传入故障**之前**预取好的宠物即可绕开这个耦合：此时探针只问一件事 ——
    「支付这一步还能不能成」。这不是放宽判据，而是把两条路径分开测：
    发现路径由 `probe_home` 负责，支付路径由这里负责。

    ## 选哪只宠物是这个探针的正确性关键

    必须挑**可用**的宠物、并用**它自己的类型**提交，理由见 `parse_pets`
    的 docstring：取 `ids[0]` + 硬编码 `puppy` 会在宠物已被领养或不是狗时
    报出 `value=0`，把「前置条件不满足」伪装成「领养功能坏了」，
    进而让切断实验写出假 `confirmed`。

    连试至多 `_ADOPT_TRIES` 只：单只宠物可能在两次请求之间被别人领走
    （合成流量 cron、压测 TGB、另一个实验都在跑），一只失败不足以断言功能坏。

    一只都没可用时返回 `ok=False`（前置条件不满足）而**不是** `value=0`
    —— 这两者的区别正是这个探针存在的意义。
    """
    uid = "%s-adopt" % _UID
    try:
        if pet is not None:
            usable, pets = [pet], [pet]
        else:
            st, html = _get("/?userId=%s" % uid)
            if st != 200:
                return {"ok": False, "value": None,
                        "detail": "前置失败：首页 HTTP %d" % st}
            pets = parse_pets(html)
            usable = [p for p in pets if p["available"]]
        if not usable:
            return {"ok": False, "value": None,
                    "detail": "前置失败：%d 只宠物全部已被领养，无可领养对象"
                              % len(pets)}
        tried = []
        for pet in usable[:_ADOPT_TRIES]:
            st2, body = _post("/Payment/MakePayment",
                              {"petId": pet["id"], "pettype": pet["pettype"],
                               "userId": uid})
            good = st2 == 200 and ("Thank" in body or "txStatus" in body)
            tried.append("%s/%s=%s" % (pet["id"], pet["pettype"],
                                       "成功" if good else "HTTP %d" % st2))
            if good:
                return {"ok": True, "value": 1,
                        "detail": "领养成功（%s；可用 %d/%d 只）"
                                  % (tried[-1], len(usable), len(pets))}
        return {"ok": True, "value": 0,
                "detail": "连试 %d 只均未成功（%s；可用 %d/%d 只）"
                          % (len(tried), "、".join(tried), len(usable), len(pets))}
    except Exception as e:
        return {"ok": False, "value": None, "detail": "请求失败 %r" % e}
    finally:
        try:
            _get("/housekeeping?userId=%s" % uid)   # 清理，避免库存单调消耗
        except Exception:
            pass


def probe_list() -> dict:
    """领养列表：页面能否正常返回。覆盖 petlistadoptions 与其 RDS 依赖。

    这个探针的 value 是「页面是否渲染出列表容器」而不是「有几条领养」——
    列表为空是正常业务状态（没人领养过），不是故障。
    """
    try:
        st, html = _get("/PetListAdoptions?userId=%s-list" % _UID)
    except Exception as e:
        return {"ok": False, "value": None, "detail": "请求失败 %r" % e}
    if st != 200:
        return {"ok": False, "value": None, "detail": "HTTP %d" % st}
    # 正常页面必含标题；后端挂掉时 petsite 渲染的是错误/空壳页
    good = "Adopted Pet List" in html
    return {"ok": True, "value": 1 if good else 0,
            "detail": "列表页标题 %s，HTML %d 字符" % (good, len(html))}


def probe_history() -> dict:
    """领养历史：页面能否正常返回。覆盖 pethistory 与其 RDS 依赖。"""
    try:
        st, html = _get("/PetHistory?userId=%s-hist" % _UID)
    except Exception as e:
        return {"ok": False, "value": None, "detail": "请求失败 %r" % e}
    if st != 200:
        return {"ok": False, "value": None, "detail": "HTTP %d" % st}
    good = len(html) > 12000        # 空壳页约 10.7KB，正常页明显更大
    return {"ok": True, "value": 1 if good else 0,
            "detail": "HTML %d 字符（阈值 12000）" % len(html)}


def probe_waggle() -> dict:
    """AI 问答：Waggle 能否给出实质回答。覆盖 AgentRuntime 与 agent 委派链。

    ## agent 层的观测通道必须是语义的

    HTTP 调用无论委派成功与否都返回 200 —— petsite 在 AgentCore 调用失败时
    返回 200 + 兜底文案。只看状态码永远看不出 agent 依赖断没断，
    只有**回答内容**会变。这是 agent 依赖唯一可行的观测通道。

    ## ⚠️ 两个已实测的缺陷（2026-09-15 修）

    ### 1. 固定 SessionId 会与**别的调用方**自撞

    原实现用常量 `chaos-probe-xxxx…`。AgentCore 按 `runtimeSessionId` **串行**，
    于是任意两个并发使用本探针的人共用一个会话、互相顶掉：

        {"error": "Agent is already processing a request.
                   Concurrent invocations are not supported.",
         "error_type": "ConcurrencyException", ...}

    实测 5 连发全部 0.2s 返回该错误（正常调用 20~22 秒）—— 因为另一个会话
    正在用同一个常量 SessionId 跑实验。**每次调用必须用独立会话。**

    ### 2. 判据把 JSON 错误体当成真实回答

    原判据是：

        good = st == 200 and len(text.strip()) >= 40 and not fallback

    上面那个 `ConcurrencyException` 体：状态码 200 ✓、180 字符 ≥ 40 ✓、
    不含 `connection was interrupted` / `please try again` ✗
    —— 于是**一个硬错误被判为健康**（`ok=True, value=1`）。

    这一条尤其危险：`SERVICE_PROBES` 里三个 AgentRuntime 服务把本探针当作
    **唯一**证据通道（`_semantic_only` 那条路径），判据有洞就会在合规产物上
    落下「依赖完好」的假结论。语义判据的门槛不能只是「不是那句兜底文案」，
    必须是「**确实是一个回答**」。

    与本模块另两处同族缺陷一致（`probe_adopt` 把表单页当成功、
    首页错误页被当成空目录页）：**200 + 有内容 ≠ 业务正常。**
    """
    # 每次调用独立会话 —— 既避免自撞，也避免与并发实验共用会话。
    # 仍须 >= 33 字符（petsite 原样透传给 runtimeSessionId，不做校验）。
    sid = "chaos-probe-%s" % uuid.uuid4().hex          # 12 + 32 = 44 字符
    body = json.dumps({"Message": "Which dogs are available for adoption?",
                       "SessionId": sid}).encode()
    req = urllib.request.Request(
        PETSITE_URL + "/Waggle/SendMessage", data=body,
        headers={**_UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            st, text = r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "value": None, "detail": "请求失败 %r" % e}
    stripped = text.strip()
    fallback = any(m in text.lower() for m in
                   ("connection was interrupted", "please try again",
                    "couldn't generate a response",     # 空流兜底，此前漏判
                    "service is currently busy"))       # ServiceException 兜底
    # 结构化错误体：petsite 把它透传成 200，只看长度与兜底文案看不出来
    err = None
    if stripped.startswith("{"):
        try:
            d = json.loads(stripped)
            if isinstance(d, dict) and (d.get("error") or d.get("error_type")):
                err = str(d.get("error_type") or d.get("error"))[:60]
        except ValueError:
            pass
    good = (st == 200 and len(stripped) >= 40 and not fallback and not err)
    return {"ok": True, "value": 1 if good else 0,
            "detail": "HTTP %d，%d 字符，兜底文案 %s%s"
                      % (st, len(text), fallback,
                         ("，错误体 %s" % err) if err else "")}


#: 源服务 → 该服务提供的业务功能探针。**声明式**，由 test_74 校验。
#:
#: petsite 挂四个：它的某条依赖可能只坏其中一个功能。少挂一个，
#: 漏掉的形状是「业务正常」—— 一个假结论，比测不出更糟。
SERVICE_PROBES: dict[str, tuple] = {
    # `images` 探针覆盖 **S3 数据路径**（预签名 URL 的实际 GET）。
    #
    # 注册给 petsite 而**不是** petsearch，依据是 2026-09-17 实查的 IAM 权限：
    #
    #   petsearch 的 IRSA 角色（ServicesEks2-searchserviceServiceAccountRole588AF64-…）
    #     对该桶**只有 s3:CreateBucket**，无 GetObject
    #     → 它观测到的全部 S3 流量就是那个永远 409 的建桶调用
    #       （X-Ray: total=634 / ok=0 / err=634，全是 BucketAlreadyOwnedByYouException）
    #     → deny s3:* 对它只断掉这个无用调用，本探针不会退化
    #
    # 给 petsearch 挂 images 会制造一个**测不到目标现象**的探针 ——
    # 那比没有探针更糟：它会让「无退化」看起来像一次有效的否证。
    #
    # ⚠️ 若将来给 petsearch 加了 GetObject（或查明预签名 URL 确由它签发），
    #    再把 images 加到它名下，并在提交说明里写清依据。
    "petsite": (("home", probe_home), ("adopt", probe_adopt),
                ("list", probe_list), ("waggle", probe_waggle),
                ("images", probe_pet_images)),
    "petsearch": (("home", probe_home),),
    "payforadoption": (("adopt", probe_adopt),),
    "petlistadoptions": (("list", probe_list),),
    "pethistory": (("history", probe_history),),
    # ── AgentCore 运行时（2026-09-15 补入）────────────────────────────
    #
    # 2026-09-15 给 IAM deny 补了 AgentCore 执行角色的解析
    # （`_agentcore_role_for`，读 `get-agent-runtime` 的 roleArn），
    # 于是 `Delegates AgentRuntime -> AgentRuntime` 变成可注入。
    # 但 `verify_via_iam_deny.py` 会在**没有业务探针时拒绝开跑** ——
    # 「没有业务证据的 confirmed 是过度声称：只证明了调用失败，
    # 没证明业务受损」。所以那三条边当时仍然跑不起来：
    # 角色解析补了，业务观测没补，**只做了一半**。
    #
    # 挂 `probe_waggle` 而不是别的：它是唯一走 agent 委派链的探针，
    # 而且它的判据是**语义的**（回答内容），不是状态码 ——
    # petsite 在 AgentCore 调用失败时返回 200 + 兜底文案，
    # 只看状态码永远看不出 agent 依赖断没断。
    #
    # 编排者与被委派者都挂同一个探针：切断 Orchestrator 到任一子 agent 的
    # 委派，表现都是 Waggle 给不出实质回答。这不是偷懒 ——
    # 用户可见的业务功能就是这一个。
    "WaggleAIOrchestrator": (("waggle", probe_waggle),),
    "WaggleAINutrition": (("waggle", probe_waggle),),
    "WaggleAIAdoption": (("waggle", probe_waggle),),
    "WaggleAIOrdering": (("waggle", probe_waggle),),
}


def probes_for(service: str) -> tuple:
    """取该源服务的探针组。没有登记的服务返回空 —— 调用方必须据此拒绝开跑。

    返回空**不等于**「用默认探针」。没有匹配的业务探针时，任何退化数字都
    无法归因到业务影响，此时应拒绝出 confirmed 判定。
    """
    return SERVICE_PROBES.get(service, ())


def run_probes(service: str, n: int = 3, gap: float = 3.0,
               probe_kwargs: dict | None = None) -> dict:
    """跑该服务的全部探针各 n 次，返回 {探针名: [结果…]}。

    `probe_kwargs` 形如 `{"adopt": {"pet": {...}}}`，按探针名透传额外参数。

    ## 为什么需要透传

    验证 `payforadoption -> Microservice(petsearch)` 时，`probe_adopt` 默认
    从首页取宠物，而**首页本身调 petsearch** —— 切断 petsearch 后探针会卡在
    「取不到 petId」这个前置条件上返回 `ok=False`，于是测不到支付本身。
    调用方在故障**之前**预取好宠物、经这里传进去，才能把发现路径与支付路径
    分开测。2026-09-15 第一版给 `probe_adopt` 加了 `pet` 参数却**没接到调用链上**，
    结果整轮实验的业务通道全是「无有效采样」，白跑一轮。
    """
    kw = probe_kwargs or {}
    out: dict[str, list] = {}
    for name, fn in probes_for(service):
        out[name] = []
        for i in range(n):
            out[name].append(fn(**kw.get(name, {})))
            if i < n - 1:
                time.sleep(gap)
    return out

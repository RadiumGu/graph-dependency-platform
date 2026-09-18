#!/usr/bin/env python3
"""对「源在集群内、目标是集群外托管服务」的依赖边跑注入实验。

## 为什么既有脚本不够用

`scripts/verify_edges_chaos.sh` 只支持 **Pod 到 Pod** 的切断
（`target.selector.labelSelectors.app`）。而本轮前置检查挑出的可验边
目标都在集群外（DynamoDB 表、S3 桶），需要 `externalTargets`。

## 观测方为什么用 DeepFlow 而不是 HTTP 探测

既有脚本用 8 次 HTTP 探测量退化。对 `petsearch -> DynamoDB` 这条边，
更强的信号是**上游对 petsearch 的调用成功率**：

    petsite -> petsearch   近 15min 约 20,000 次调用

两万次调用的成功率变化，比 8 次探测的样本量高三个数量级。而且它天然是
「A 依赖 B」的证据链：切 petsearch→DynamoDB，若 petsite→petsearch
的成功率塌下来，说明这条依赖成立。

## 注入生效性怎么确认（这是本脚本最关键的一道）

仓库有一次血的教训：FIS `disrupt-connectivity scope=s3` 判了
`petsearch -> s3` 为 refuted，但拿 X-Ray 按故障窗口复核发现窗口内
仍有 10 次与 13 次**成功**的 S3 调用 —— 注入根本没生效，
那个 refuted 是凭空证伪。按 DoD-10 累计两次 refuted 就删边。

所以本脚本用 X-Ray 直接量**被测那条边自己**在注入期的调用数：
调用数没掉 = 注入没生效 = 什么都没验到（不下结论）。
这比只看 Chaos Mesh 的 AllInjected 强：AllInjected 只说明 iptables
规则装上了，不说明它拦住了目标流量。

## 自动恢复

`spec.duration` 到期由 Chaos Mesh 自己撤销规则，满足「故障注入必须
可自动恢复」的硬约束。脚本收尾额外 delete 一次并等 AllRecovered，
且 **finally 块里无条件清理** —— 脚本中途异常退出也不留故障状态。

## 用法

    python3 scripts/verify_external_target_edges.py --list      # 只列候选
    python3 scripts/verify_external_target_edges.py --dry-run   # 打印将施加的注入
    python3 scripts/verify_external_target_edges.py --run       # 真的注入
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT / 'chaos' / 'code', ROOT / 'rca', ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

KUBECTL = os.environ.get('KUBECTL', str(pathlib.Path.home() / 'bin' / 'kubectl'))
NS = 'petadoptions'

#: 注入窗口。必须容纳「规则生效等待 + 观测窗口」。
#: DeepFlow 的 l7_flow_log 有约 30-60s 的写入延迟，所以观测窗口不能太短，
#: 否则注入期的流量还没落库就去查了 —— 那会读成「零流量」。
SETTLE_SECONDS = 45           # 规则生效 + 让流量打进去
OBSERVE_SECONDS = 120         # 观测窗口

#: 复合模式（--compound）由 main() 置位。它会在故障期间删 Pod，
#: 所以观测窗必须容纳「Pod 重建 + 新 Pod 在故障下建连失败」的全过程，
#: 否则观测会落在旧 Pod 还在服务的那一段 —— 看不到退化，又是一次假 refuted。
COMPOUND = False
COMPOUND_OBSERVE_SECONDS = 180


def _observe_seconds() -> int:
    return COMPOUND_OBSERVE_SECONDS if COMPOUND else OBSERVE_SECONDS


def _duration() -> str:
    return f'{SETTLE_SECONDS + _observe_seconds() + 90}s'

#: 图谱目标类型 -> DNSChaos 的域名匹配模式。
#:
#: ## 为什么是 DNSChaos 而不是 NetworkChaos+externalTargets（2026-09-09 实测）
#:
#: 第一版用 `NetworkChaos` + `externalTargets: dynamodb.<region>.amazonaws.com`。
#: **完全没生效**，而且差点写出错误结论：
#:
#:   · Chaos Mesh 在 apply 时把域名解析成 IP 再装 iptables 规则。
#:     AWS 区域端点有**多个轮换 IP**（实测 pod 内解析到 35.71.114.102，
#:     而 SDK 后续解析会拿到别的），规则只封住其中一个。
#:   · S3 更糟：它是 **Gateway 端点**（com.amazonaws.<region>.s3），
#:     靠路由表 + 前缀列表转发，封单个 IP 根本不在路径上。
#:
#: 旁证：注入期 `petsite -> search-service` 响应码全 200、
#: 平均延迟 190-350ms、p99 恒定 ~3008ms —— 完全平坦。
#: 而仓库早有同类记载：FIS `disrupt-connectivity scope=s3` 也没切断这条路径。
#:
#: DNSChaos 在**名字解析**层动手，不关心目标有几个 IP、走不走 Gateway ——
#: SDK 每次建连都要解析域名，解析失败就连不上。
_TARGET_DNS_PATTERNS = {
    'DynamoDBTable': ['dynamodb.*'],
    'S3Bucket': ['s3.*', '*.s3.*'],
}


def _kubectl(*args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run([KUBECTL, *args], capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def _restart_pods(label: str) -> tuple[bool, str]:
    """在故障生效期间重建 Pod，让连接与 DNS 在故障下重新建立。

    ## 为什么复合实验是这类边的唯一验证途径（2026-09-09 实测）

    单纯切断对 `petsearch -> DynamoDB` **完全无效**，两条路都试过：

      · NetworkChaos + externalTargets —— AWS 区域端点有多个轮换 IP，
        Chaos Mesh 只在 apply 时解析一次，规则只封住其中一个；
        S3 更是 Gateway 端点，封 IP 根本不在路径上。
      · DNSChaos —— AllInjected=True 但依然没打断：应用用**长连接 +
        DNS 缓存**，4.7 次/秒全跑在 keep-alive 连接上，
        2 分钟窗口内 DNS 从不重查。

    所以必须让连接**重新建立**：删掉 Pod，新 Pod 启动时要重新解析域名、
    重新建连，此时 DNS 故障才真正拦在路径上。

    这与那 6 条 `Microservice -DependsOn-> ECRRepository` 是同一形状 ——
    `injectability` 把 ECR 判为 `needs_compound_experiment` 的理由
    （"稳态无退化，配合删 Pod 就是可验证的复合实验"）在这里同样成立，
    只是被切断的东西不同。

    用 `delete pod` 而不是 `rollout restart`：后者是滚动更新，
    会**等新 Pod Ready 才删旧的**，于是故障期间始终有健康的旧 Pod 在服务，
    观测方看不到任何退化 —— 那会重演一次假 refuted。
    """
    rc, out = _kubectl('delete', 'pod', '-n', NS, '-l', f'app={label}',
                       '--wait=false', timeout=180)
    return rc == 0, out.strip()[:200]


def _pods_ready(label: str) -> tuple[int, int]:
    """返回 (ready 数, 总数)。用于恢复确认。"""
    rc, out = _kubectl(
        'get', 'pod', '-n', NS, '-l', f'app={label}', '-o',
        'jsonpath={range .items[*]}{.status.containerStatuses[0].ready}{"\\n"}{end}')
    if rc != 0:
        return 0, 0
    vals = [x.strip() for x in out.splitlines() if x.strip()]
    return sum(1 for v in vals if v == 'true'), len(vals)


def _all_injected(name: str) -> str:
    rc, out = _kubectl(
        'get', 'dnschaos', name, '-n', NS, '-o',
        'jsonpath={range .status.conditions[?(@.type=="AllInjected")]}'
        '{.status}{end}')
    return out.strip() if rc == 0 else '?'


def _candidates() -> list[dict]:
    """有边级流量、源在集群内、目标是集群外托管服务的待验边。

    复合模式下**额外纳入 `inconclusive`**：它的含义是「试过但没得出结论」，
    而复合实验正是为了用更强的手法重试。上一轮那两条就是因为
    「切断了但应用用长连接 + DNS 缓存，注入没真正拦在路径上」而 inconclusive ——
    不重试它们，复合实验就没有对象。

    `confirmed` / `refuted` 不纳入：那是已有结论的边，重跑要另走复核流程
    （且 refuted 累计两次会删边，不能顺手触发）。
    """
    from neptune import neptune_client as nc
    retryable = ("['inconclusive']" if COMPOUND else "[]")
    rows = nc.results(f"""
MATCH (a:Microservice)-[r]->(b)
WHERE (r.verify_status IS NULL OR r.verify_status = 'untested'
       OR r.verify_status IN {retryable})
  AND r.verify_blocked_class IS NULL
  AND labels(b)[0] IN ['DynamoDBTable','S3Bucket']
RETURN id(r) AS eid, type(r) AS edge, a.name AS src, labels(b)[0] AS dl,
       b.name AS dst, properties(r) AS props
""")
    return rows


def _observer_of(src: str) -> str | None:
    """挑一个上游作为观测方：它对 src 的调用成功率就是退化信号。

    选流量最大的那个上游 —— 样本量直接决定判据的分辨力。
    """
    from neptune import neptune_client as nc
    rows = nc.results("""
MATCH (u:Microservice)-[:Calls]->(s:Microservice {name:$src})
RETURN u.name AS up
""", {'src': src})
    from runner.metrics import DeepFlowMetrics
    m = DeepFlowMetrics()
    best, best_n = None, 0
    for r in rows:
        snap = m.collect_edge_flow(r['up'], src, window_seconds=900)
        if snap.ok and snap.total_requests > best_n:
            best, best_n = r['up'], snap.total_requests
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--list', action='store_true')
    g.add_argument('--dry-run', action='store_true')
    g.add_argument('--run', action='store_true')
    ap.add_argument('--compound', action='store_true',
                    help='复合实验：在故障生效期间删 Pod，让连接与 DNS 重建')
    args = ap.parse_args()
    global COMPOUND
    COMPOUND = bool(args.compound)

    os.environ.setdefault(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
    region = os.environ.setdefault('REGION', 'ap-northeast-1')

    from runner import service_names
    from runner.metrics import DeepFlowMetrics
    from runner.xray_metrics import XRayEdgeMetrics

    cands = _candidates()
    print(f'候选边（目标在集群外、有边级流量、尚无结论）: {len(cands)} 条\n')

    plans = []
    for c in cands:
        pats = _TARGET_DNS_PATTERNS.get(c['dl']) or []
        if not pats:
            print(f"  ⏭  {c['edge']} {c['src']} -> {c['dst']}："
                  f"目标类型 {c['dl']} 没有已知的 DNS 模式映射")
            continue
        observer = _observer_of(c['src'])
        if not observer:
            print(f"  ⏭  {c['edge']} {c['src']} -> {c['dst']}："
                  f"找不到有流量的上游观测方 —— 无退化信号可读")
            continue
        plan = {
            **c,
            'k8s_label': service_names.to_k8s(c['src']),
            'dns_patterns': list(pats),
            'observer': observer,
        }
        plans.append(plan)
        print(f"  ✅ {c['edge']} {c['src']} -> {c['dst'][:40]}")
        print(f"       切断: app={plan['k8s_label']} ✂ DNS {plan['dns_patterns']}")
        print(f"       观测: {observer} -> {c['src']} 的成功率")

    if args.list or not plans:
        return 0

    print(f'\n注入窗口 duration={_duration()}'
          f'（生效等待 {SETTLE_SECONDS}s + 观测 {_observe_seconds()}s + 余量 90s）'
          + ('  [复合模式：故障期间删 Pod]' if COMPOUND else ''))
    if args.dry_run:
        for p in plans:
            print('\n' + _manifest(p))
        print('\n（dry-run，未施加。加 --run 真的注入）')
        return 0

    df, xr = DeepFlowMetrics(), XRayEdgeMetrics()
    results = []
    for p in plans:
        results.append(_run_one(p, df, xr))

    out = ROOT / 'todo' / (
        'chaos-external-edge-run_'
        + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
        + '.json')
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                   encoding='utf-8')
    print(f'\n结果已写: {out.relative_to(ROOT)}')
    return 0


def _chaos_name(p: dict) -> str:
    raw = f"vx-{p['src']}-{p['dl']}".lower()
    return ''.join(ch if ch.isalnum() or ch == '-' else '-' for ch in raw)[:50]


def _manifest(p: dict) -> str:
    """DNSChaos manifest。

    `action: error` 让匹配到的域名解析直接失败（NXDOMAIN 风格），
    比 `random` 更适合验证依赖：random 会返回随机 IP，客户端可能
    连到别处并得到一个**不同的**错误，混淆归因。
    """
    # 必须加引号：裸 `*.s3.*` 里的 `*` 会被 YAML 当成锚点引用而报错
    pats = '\n'.join(f'    - "{t}"' for t in p['dns_patterns'])
    return f"""apiVersion: chaos-mesh.org/v1alpha1
kind: DNSChaos
metadata:
  name: {_chaos_name(p)}
  namespace: {NS}
spec:
  action: error
  mode: all
  duration: {_duration()}
  selector:
    namespaces: [{NS}]
    labelSelectors:
      app: {p['k8s_label']}
  patterns:
{pats}"""


def _run_one(p: dict, df, xr) -> dict:
    """跑一条边。三道纪律：注入前后都断言生效，且用 X-Ray 量边自身的退化。"""
    name = _chaos_name(p)
    edge_desc = f"{p['edge']} {p['src']} -> {p['dst']}"
    print(f"\n{'═'*66}\n▶ {edge_desc}")
    rec: dict = {'edge': edge_desc, 'chaos': name}

    # ── 基线 ─────────────────────────────────────────────────────────
    #
    # ⚠️ 基线窗与注入窗**必须等长**（2026-09-09 第二次踩同一个坑）。
    #
    # 第一版基线用 900s、注入用 180s，于是传给 verify_edge 的
    # `observer_baseline_requests=21051` 与 `observer_injected_requests=1010`
    # 根本不可比 —— 归一化后是 23.4/s vs 5.6/s（降 76%），
    # 但原始计数的比例是 95%，凭空放大了退化。
    #
    # 这与 `xray_metrics.took_effect` 那次是**同一个错误**：
    # 跨窗口比较前先问两个窗口一样长吗。既然判定链拿到的是计数，
    # 那就把两个窗口做成一样长，让计数本身可比。
    obs_window = _observe_seconds()
    obs_base = df.collect_edge_flow(p['observer'], p['src'],
                                    window_seconds=obs_window)
    edge_base = xr.collect_edge_flow(p['src'], p['dst'], window_seconds=3600)
    print(f"  基线  观测方 {p['observer']}->{p['src']}: "
          f"{obs_base.total_requests} 次/{obs_window}s "
          f"（{obs_base.total_requests / obs_window:.1f}/s）"
          f" / 成功率 {obs_base.success_rate}%")
    print(f"        被测边自身(X-Ray 1h): {edge_base.total_requests} 次")
    rec['observer_window_seconds'] = obs_window
    rec['baseline'] = {'observer_requests': obs_base.total_requests,
                       'observer_success_rate': obs_base.success_rate,
                       'edge_calls': edge_base.total_requests}
    if not obs_base.ok or obs_base.total_requests < 20:
        rec['verdict'] = 'skipped'
        rec['reason'] = (f'观测方基线流量不足（{obs_base.total_requests} 次，需 >= 20）'
                         f' —— 退化判据失去意义')
        print(f"  → 跳过：{rec['reason']}")
        return rec

    manifest = ROOT / 'todo' / f'.{name}.yaml'
    try:
        manifest.write_text(_manifest(p) + '\n', encoding='utf-8')
        rc, out = _kubectl('apply', '-f', str(manifest))
        if rc != 0:
            rec['verdict'] = 'skipped'
            rec['reason'] = f'apply 失败: {out.strip()[:200]}'
            print(f"  → 跳过：{rec['reason']}")
            return rec
        print(f"  已施加 DNSChaos/{name}，等 {SETTLE_SECONDS}s 生效…")
        time.sleep(SETTLE_SECONDS)

        inj = _all_injected(name)
        rec['all_injected_before'] = inj
        if inj != 'True':
            rc2, ev = _kubectl('get', 'events', '-n', NS,
                               '--field-selector',
                               f'involvedObject.name={name}')
            rec['verdict'] = 'inconclusive'
            rec['reason'] = f'注入未生效 AllInjected={inj}'
            rec['events'] = ev.strip()[-300:]
            print(f"  → 不下结论：{rec['reason']}")
            return rec

        print(f"  注入生效，观测 {_observe_seconds()}s…")
        if COMPOUND:
            # ── 复合步骤：在故障生效期间重建 Pod ──
            ok, msg = _restart_pods(p['k8s_label'])
            rec['pods_deleted'] = ok
            print(f"  ⟳ 复合步骤：删除 app={p['k8s_label']} 的 Pod"
                  f"（{'成功' if ok else '失败: ' + msg}）"
                  f" —— 让连接与 DNS 在故障下重建")
            if not ok:
                rec['verdict'] = 'skipped'
                rec['reason'] = f'复合步骤失败，未能重建 Pod: {msg}'
                print(f"  → 跳过：{rec['reason']}")
                return rec
            # ── 判别信号：新 Pod 在故障下能否 Ready ──
            #
            # 观测方吞吐在复合模式下**没有判别力**：删了 Pod，
            # 上游对它的调用当然会塌 —— 这个信号分不清
            # 「连不上被切断的目标」与「Pod 正在重启」。
            # 第一版用它判出了一个 soft，已撤回（第二次）。
            #
            # 有判别力的是：新 Pod 在目标不可达时能不能起来并就绪。
            #   起不来  -> 目标是**启动期硬依赖**
            #   起得来  -> 目标至少不是启动期硬依赖
            # 这个信号不受「Pod 重启」本身干扰，因为重启是两种情况的共同前提。
            time.sleep(_observe_seconds())
            ready, total = _pods_ready(p['k8s_label'])
            rec['pods_ready_under_fault'] = f'{ready}/{total}'
            print(f"  🔍 故障下 Pod 就绪: {ready}/{total}")
        else:
            time.sleep(_observe_seconds())

        obs_inj = df.collect_edge_flow(p['observer'], p['src'],
                                       window_seconds=_observe_seconds())
        # 边自身：X-Ray 的注入窗口用真实起止，不用「近 N 秒」
        eff, eff_why = xr.took_effect(p['src'], p['dst'],
                                     injection_seconds=_observe_seconds(),
                                     baseline_seconds=1800)
        still = _all_injected(name)
        rec['all_injected_after'] = still
        rec['injected'] = {'observer_requests': obs_inj.total_requests,
                           'observer_success_rate': obs_inj.success_rate}
        rec['injection_confirmed'] = eff
        rec['injection_confirmed_why'] = eff_why
        print(f"  注入期 观测方: {obs_inj.total_requests} 次 / "
              f"成功率 {obs_inj.success_rate}%  (结束时 AllInjected={still})")
        print(f"        生效性: {eff} —— {eff_why}")

        if still != 'True':
            rec['verdict'] = 'inconclusive'
            rec['reason'] = ('观测跑出了故障窗口，后半程打在已恢复的系统上 —— '
                             '这个坑真实发生过，会让 confirmed 翻成假 refuted')
            print(f"  → 不下结论：{rec['reason']}")
            return rec

        deg = obs_base.success_rate - obs_inj.success_rate
        rec['observer_degradation_pct'] = round(deg, 2)
        print(f"  观测方退化(成功率): {deg:.2f}%")

        # ── 复合模式：观测方信号被 Pod 重启混淆，**不得**据此写判定 ──
        #
        # 这道拒绝是硬的，不是保守。删 Pod 会让上游对它的调用必然塌陷，
        # 于是「吞吐降了」既可以读成「依赖被切断」也可以读成「Pod 在重启」。
        # 拿它喂判定链已经产出过一次 soft 并被撤回（2026-09-09，第二次同类错误）。
        #
        # 复合模式的产出是**结构化观察**而不是 verdict：
        # 由人看 pods_ready_under_fault 决定下一步，
        # 或者补一个对照臂（同样删 Pod 但不切断）之后再自动判。
        if COMPOUND:
            ready_s = rec.get('pods_ready_under_fault', '?')
            rec['verdict'] = 'observation_only'
            rec['reason'] = (
                f'复合模式：删 Pod 使观测方信号失去判别力（删了 Pod，'
                f'上游调用必然塌陷，分不清「连不上目标」与「Pod 在重启」）。'
                f'刻意不写 verify_status。'
                f'故障下 Pod 就绪={ready_s}；'
                f'注入生效性={eff}（{eff_why}）')
            print(f"  → 只记观察，不写判定：{rec['reason']}")
            print(f"     要自动判定需补一个对照臂：同样删 Pod 但**不**切断目标，"
                  f"对比两次的就绪时间与错误率")
            return rec

        from runner.edge_verification import verify_edge, write_verdict
        v = verify_edge(
            # `verify_edge` 要的是 candidate_edges() 的那套字段名。
            # 缺 eid 会 KeyError（实测踩到）—— 它是 write_verdict 精确定位
            # 那一条边的唯一依据，没有它就会写给所有同名出入边。
            edge={'eid': p['eid'], 'label': p['edge'],
                  'observer': p['observer'], 'target': p['dst'],
                  'props': p.get('props') or {}},
            observer_baseline_requests=obs_base.total_requests,
            observer_injected_requests=obs_inj.total_requests,
            observer_degradation_pct=float(deg),
            experiment_id=f'chaosmesh/{name}',
            evidence_channel='both',
            injection_confirmed=eff,
            edge_baseline_calls=edge_base.total_requests or None,
        )
        rec['verdict'] = v.get('status')
        rec['reason'] = v.get('reason')
        rec['dependency_class'] = v.get('dependency_class')
        rec['verdict_full'] = v
        print(f"  → 判定: {rec['verdict']} —— {rec['reason']}")
        print(f"     强度分级: {v.get('dependency_class') or 'unclassified'}"
              f" —— {v.get('dependency_class_reason') or ''}")
        ok = write_verdict(v)
        rec['written'] = ok
        print(f"     写回图谱: {'成功' if ok else '失败'}")
        return rec
    finally:
        # ── 无条件清理：脚本中途异常也不得留下故障状态 ──
        _kubectl('delete', 'dnschaos', name, '-n', NS)
        manifest.unlink(missing_ok=True)
        for _ in range(8):
            rc, out = _kubectl(
                'get', 'dnschaos', name, '-n', NS, '-o', 'name')
            if rc != 0:            # 已删掉
                break
            time.sleep(10)
        print(f"  已清理 DNSChaos/{name}")
        if COMPOUND:
            # ── 复合实验删过 Pod，必须确认服务真的恢复了 ──
            # 「故障注入必须可自动恢复」这条硬约束，在复合实验里不只是
            # 撤销故障规则 —— 被删掉的 Pod 也必须重新起来并 Ready。
            # 不等它就退出，会把一个降级中的服务留给下一条实验当基线。
            for i in range(20):
                ready, total = _pods_ready(p['k8s_label'])
                if total > 0 and ready == total:
                    print(f"  ✓ Pod 已恢复: {ready}/{total} Ready")
                    break
                time.sleep(15)
            else:
                ready, total = _pods_ready(p['k8s_label'])
                print(f"  ⚠️  Pod 未在 5 分钟内全部恢复: {ready}/{total} Ready"
                      f" —— 请人工检查 app={p['k8s_label']}")


if __name__ == '__main__':
    raise SystemExit(main())

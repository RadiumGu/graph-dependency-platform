"""test_60_dr_az_scope_finds_services.py — AZ 范围必须能算出受影响服务。

## 这条门禁挡的是什么

2026-09-07 实测：`--scope az` 生成的计划里 **受影响服务恒为 0**。

根因是图模型与查询差一跳。`Microservice` **从不**直接 `LocatedIn` 一个 AZ ——
实测 1 跳可达服务数为 0。挂在 AZ 上的是 `Pod`（808 条边）、`Subnet`、
`LoadBalancer`、`EC2Instance`、`RDSInstance`、`NeptuneInstance`。真实路径是两跳：

    AZ <-[:LocatedIn]- Pod <-[:RunsOn]- Microservice

所以老查询返回几百个锚点，而 `plan_generator.affected_services`
（按 `type in ("Microservice", "K8sService")` 过滤）什么都没拿到。

**一份说「受影响服务 0」的 AZ 故障计划比没有计划更糟** ——
它主动告诉运维「这个 AZ 掉了不影响任何服务」。而且它不报错、有计划 ID、
有 RTO、有阶段，唯独没有内容。

## 还挡「全停」与「降级」被混为一谈

服务不是「位于」某个 AZ，而是「部分在」。实测：

    petsite            1a 有 96 个 pod，1c 有 160 个   -> 掉一个 AZ 是降级
    trafficgenerator   只有 1 个 pod，只在 1c          -> 1c 掉了它就没了

把两者混在同一份「受影响服务」清单里，等于让运维在「7 个服务受影响」和
「1 个服务彻底没了」之间自己猜。所以计划必须单独给出 `fully_lost_services`。
"""
import os
import sys
from pathlib import Path

import pytest

from paths import PROJECT_ROOT

_DR = Path(PROJECT_ROOT) / 'dr-plan-generator'
ONLINE = bool(os.environ.get('NEPTUNE_ENDPOINT'))


def _queries():
    if str(_DR) not in sys.path:
        sys.path.insert(0, str(_DR))
    from graph import queries  # type: ignore
    return queries


def test_m01_源码里必须走完Pod那一跳():
    """静态判据：不依赖 Neptune 可达，任何环境都能挡住回归。"""
    src = (_DR / 'graph' / 'queries.py').read_text(encoding='utf-8')
    assert 'RunsOn' in src, (
        'graph/queries.py 里没有 RunsOn。Microservice 不直接 LocatedIn AZ，'
        '少了 `Pod <-[:RunsOn]- Microservice` 这一跳，'
        'AZ 范围的受影响服务会恒为 0。')
    assert '_services_hosted_in_az' in src, '缺少 AZ→服务的解析函数'
    assert '_services_hosted_in_region' in src, '缺少 region→服务的解析函数'


def test_m02_计划模型必须能区分全停与降级():
    if str(_DR) not in sys.path:
        sys.path.insert(0, str(_DR))
    import models  # type: ignore
    fields = {f.name for f in models.dataclasses.fields(models.DRPlan)}
    assert 'fully_lost_services' in fields, (
        'DRPlan 没有 fully_lost_services。「掉一半容量」与「整体消失」'
        '必须分开报，否则运维只能自己猜。')
    assert 'service_az_pods' in fields, (
        'DRPlan 没有 service_az_pods。上面那个判断的原始依据要留着，'
        '让人能自己核对而不必信结论。')


def test_m03_生成器必须填充这两个字段():
    src = (_DR / 'planner' / 'plan_generator.py').read_text(encoding='utf-8')
    assert 'az_exposure' in src, (
        'plan_generator 没有读 az_exposure —— 查询给了标记但计划把它丢了。')
    assert 'fully_lost_services=' in src, 'plan_generator 没有填 fully_lost_services'


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m04_实测AZ范围必须查出服务():
    """光有代码不够 —— 得真的在活图谱上查出东西来。"""
    q = _queries()
    found = {}
    for az in ('ap-northeast-1a', 'ap-northeast-1c'):
        rows = q.q12_az_dependency_tree(az)
        svc = [r for r in rows
               if r.get('type') in ('Microservice', 'K8sService')]
        found[az] = len(svc)
    assert all(v > 0 for v in found.values()), (
        f'AZ 范围查不出受影响服务：{found}\n'
        'Microservice 不直接 LocatedIn AZ —— 路径是 '
        'AZ <-LocatedIn- Pod <-RunsOn- Microservice。'
        '缺这一跳的计划会说「受影响服务 0」，比没有计划更糟。')


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m05_跨AZ的服务不得被标成全停():
    """多 AZ 服务掉一个 AZ 是降级。标成全停会让 DR 计划夸大故障。"""
    q = _queries()
    rows = q.q12_az_dependency_tree('ap-northeast-1a')
    for r in rows:
        exp, pods = r.get('az_exposure'), r.get('az_pod_counts') or {}
        if exp is None:
            continue
        live = [a for a, n in pods.items() if n]
        if exp == 'single-az':
            assert len(live) == 1, (
                f'{r["name"]} 被标成 single-az，但 pod 分布在 {live} —— '
                '这会让计划把降级说成全停。')
        else:
            assert len(live) > 1, (
                f'{r["name"]} 被标成 multi-az，但 pod 只在 {live} —— '
                '这会让计划漏报一次真正的全停。')


@pytest.mark.skipif(not ONLINE, reason='需要 NEPTUNE_ENDPOINT')
def test_m06_region范围不得给az暴露度标记():
    """整个 region 失守时所有 pod 都在范围内，「跨 AZ 所以只是降级」不成立。"""
    q = _queries()
    rows = q.q12_az_dependency_tree_by_region('ap-northeast-1')
    svc = [r for r in rows if r.get('type') in ('Microservice', 'K8sService')]
    assert svc, 'region 范围也查不出服务 —— 同一个少一跳的问题'
    tagged = [r['name'] for r in svc if r.get('az_exposure')]
    assert not tagged, (
        f'region 范围给这些服务标了 az_exposure：{tagged}\n'
        'region 失守时所有 AZ 都在范围内，标 multi-az 会被读成「只是降级」，'
        '那是错的。')

"""
EC2 节点身份键与 NFM 粒度的守门测试。

对应 todo/tokyo-vpc-deployment-status_20260828-1440.md 附录 D 记录的两个缺陷。
"""
import os
import sys
import warnings

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..'))
_ETL_AWS = os.path.join(_ROOT, 'infra', 'lambda', 'etl_aws')


def _src(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


# ── N-01/02：EC2 身份键（阻塞） ──────────────────────────────────────────

def test_n01_ec2_upsert_uses_immutable_identity():
    """
    EC2Instance 必须以 **instance_id**（不可变）作身份键，不能用 name。

    name 来自 Name 标签（`collect_ec2_instances` 里
    `next((t['Value'] for t in tags if t['Key']=='Name'), instance_id)`）——
    可变。标签一加/改，mergeV 匹配不到旧节点就新建一个，旧节点永远孤立。

    实测后果（2026-08-29）：14 个 EC2Instance 里 **4 个是重复实体**，
    4 台 EKS 工作节点各有两份 —— 一份在实例还没打 Name 标签时以实例 ID 命名、
    88 天前冻结，一份以 Name 标签命名。任何「有几台工作节点」
    「哪些实例 RTT 高」的查询都会数两次。

    注意**修 name 的取值规则防不住这件事** —— 那条规则本来就是对的
    （Name 标签优先、缺失回落 ID）。问题在于拿一个可变属性当身份。
    """
    h = _src('infra/lambda/etl_aws/handler.py')
    assert "upsert_vertex('EC2Instance'" in h, "EC2 写入点变了，测试需同步更新"
    # 定位 EC2 的 upsert 调用块，断言它带 identity_prop='instance_id'
    i = h.index("upsert_vertex('EC2Instance'")
    block = h[i:i + 1400]
    assert "identity_prop='instance_id'" in block, (
        "EC2Instance 的 upsert 没有传 identity_prop='instance_id' —— "
        "会退回以可变的 name 作身份键，Name 标签一改就产生重复节点")


def test_n02_identity_prop_falls_back_and_updates_name():
    """
    identity_prop 的两条语义必须都在：

    1. 值为空时**回落到 name** 而不是抛错 —— 一个拿不到 instance_id 的实例
       仍然应该进图谱，只是退回旧的（有缺陷的）身份语义，比整轮 ETL 失败好。
    2. 以非 name 作身份时，name 必须进 **onMatch** —— 否则标签改名后
       图谱里仍留着旧名字，等于只是把「重复」换成了「陈旧」。
    """
    nc = _src('infra/lambda/etl_aws/neptune_client.py')
    fn = nc.split('def upsert_vertex', 1)[1].split('\ndef ', 1)[0]
    assert 'identity_prop' in fn, "upsert_vertex 缺少 identity_prop 支持"
    assert "id_key, id_val = 'name', n" in fn, \
        "缺少回落到 name 的默认值 —— identity_prop 缺失时行为必须与原先一致"
    assert "if id_key != 'name':" in fn, \
        "以非 name 作身份时没有把 name 写进 onMatch —— 改名后图谱会留旧名字"
    # 默认路径（不传 identity_prop）必须仍以 name 匹配，保证其余 30+ 类型不受影响
    assert "'{id_key}': '{id_val}'" in fn or '{id_key}' in fn, \
        "mergeV 的身份键没有参数化"


# ── N-03/04/05：NFM 粒度（阻塞） ────────────────────────────────────────

def test_n03_vpc_aggregate_is_not_broadcast_to_instances():
    """
    监视器级（= VPC 级）聚合指标**不得**逐个复制给 VPC 内每个 EC2 实例。

    原实现：
        for inst in ec2_instances:
            if inst.get('vpc_id') in vpc_ids or not vpc_ids:
                ec2_nfm[inst['name']] = {...}      # ← 同一份聚合值抄 N 遍

    实测后果：7 个 EC2 节点的 net_rtt_avg_ms 全部等于 38.25，
    即整个 VPC 的平均值被当成了每台机器各自的 RTT ——「某台机器的 RTT」是假数据。
    这与把泛化的 X-Ray `S3` 节点当成某个具体 bucket 是同一类错误：
    **把粗粒度观测归属到细粒度实体**。

    正解：聚合写 VPC 节点（update_vpc_nfm_metrics），
    实例级用 per-flow（fetch_nfm_per_flow_metrics）。
    """
    h = _src('infra/lambda/etl_aws/handler.py')
    assert 'update_vpc_nfm_metrics' in h, \
        "handler 没有把 VPC 级聚合写到 VPC 节点"
    assert 'fetch_nfm_per_flow_metrics' in h, \
        "handler 没有采集 per-flow 数据"
    # 广播路径必须已从 handler 移除
    assert 'map_nfm_metrics_to_ec2' not in h, \
        "handler 仍在调用已废弃的广播函数 map_nfm_metrics_to_ec2"


def test_n04_no_blanket_fallback_when_scope_unknown():
    """
    `or not vpc_ids` 这个兜底必须消失。

    get_monitor 一旦失败，vpc_ids 为空集，原代码于是给**账号内所有实例**
    写上这份指标，无论它们在哪个 VPC。静默、无报错，
    写进去的数据与真实数据在图谱里**无法区分**。

    拿不到监控范围时宁可不写。

    ## 为什么用 ast 而不是子串匹配

    本测试第一版是 `assert 'or not vpc_ids' not in source` —— 它立刻误报了：
    源码的 docstring 里**引用**了这段旧代码来解释为什么删掉它，
    子串匹配分不清「代码里有」和「文档里引用」。

    按 ast 检查真实的 BoolOp 条件，才是对「代码是否真的这么写」的断言。
    """
    import ast
    path = os.path.join(_ETL_AWS, 'cloudwatch.py')
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        for operand in node.values:
            # 匹配 `not vpc_ids` 这种形态：一元 not 作用在名字上
            if (isinstance(operand, ast.UnaryOp)
                    and isinstance(operand.op, ast.Not)
                    and isinstance(operand.operand, ast.Name)
                    and 'vpc_id' in operand.operand.id):
                offenders.append(
                    f"{path}:{getattr(node, 'lineno', '?')} "
                    f"`... or not {operand.operand.id}`")

    assert not offenders, (
        "发现「范围拿不到就写给所有实例」的兜底条件：\n  "
        + "\n  ".join(offenders)
        + "\n拿不到监控范围时必须不写，而不是广播假数据。")


def test_n05_per_flow_separates_inter_az():
    """
    per-flow 必须**单独统计 INTER_AZ**。

    实测 INTER_AZ 有数据且有重传（petsite-nfm-monitor，2026-08-29：
    INTRA_AZ 5 条 / INTER_AZ 5 条 / UNCLASSIFIED 5 条，
    INTER_VPC 与 AMAZON_* 均 0 条）。

    本项目的故障边界模型是 fault_boundary='az'，跨 AZ 的网络质量
    正是它最关心的量 —— 与 INTRA_AZ 混在一起就丢掉了这个区分。
    """
    cw = _src('infra/lambda/etl_aws/cloudwatch.py')
    assert 'INTER_AZ' in cw, "per-flow 查询没有覆盖 INTER_AZ"
    assert 'net_retrans_inter_az' in cw, \
        "没有单独统计跨 AZ 重传 —— 与 INTRA_AZ 混在一起会丢掉故障边界维度"
    assert 'nfm_scope' in cw, \
        "缺少 nfm_scope 标注 —— 下游无法判断数字的真实粒度"


# ── N-06：活图谱状态（**只告警**） ──────────────────────────────────────

def test_n06_live_graph_has_no_duplicate_ec2(neptune_rca):
    """
    活图谱里不应有重复的 EC2Instance（同一 instance_id 两个节点）。

    **只告警不阻塞**：清理脚本是手动跑的
    （`infra/merge_duplicate_ec2_nodes.py --apply`），
    而写入侧修复需要部署到 etl_aws 才生效。让它阻塞会训练人忽略失败 ——
    真正的写侧回归就此被掩盖。先例：test_26 与 test_31 的 L-01。
    """
    rows = neptune_rca.results("""
        MATCH (n:EC2Instance) WHERE n.instance_id IS NOT NULL
        WITH n.instance_id AS iid, count(n) AS c WHERE c > 1
        RETURN count(iid) AS dupes
    """, {})
    dupes = int(rows[0]['dupes']) if rows else 0
    if dupes:
        warnings.warn(
            f"活图谱里有 {dupes} 个 instance_id 对应多个 EC2Instance 节点。"
            f"清理命令：python3.11 infra/merge_duplicate_ec2_nodes.py --apply。"
            f"注意必须先部署 etl_aws 的 identity_prop 修复，否则清理后会再生。",
            UserWarning)

    # 以实例 ID 作为 name 的节点也告警：说明那些实例还没打 Name 标签，
    # 一旦补上标签就会（在未修复的写入侧上）产生重复。
    idn = neptune_rca.results("""
        MATCH (n:EC2Instance) WHERE n.name STARTS WITH 'i-'
        RETURN count(n) AS n
    """, {})
    cnt = int(idn[0]['n']) if idn else 0
    if cnt:
        warnings.warn(
            f"有 {cnt} 个 EC2Instance 以实例 ID 作为 name（缺 Name 标签）。"
            f"identity_prop 修复后不会再因此产生重复，但这些实例在图谱里"
            f"缺少可读名字。", UserWarning)

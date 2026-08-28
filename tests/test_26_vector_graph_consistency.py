"""
test_26_vector_graph_consistency.py — S3 Vectors 索引 ↔ Neptune 图谱一致性

覆盖测试清单:V-01 ~ V-02

## 为什么需要这个测试

2026-08-28 实测:向量索引 18 条中 **10 条是孤儿**(56%)—— 对应的 Incident
节点在图谱里根本不存在。

这不是卫生问题而是正确性问题:`search_similar` 的输出会作为
「语义相似历史案例」注入 RCA 提示词。孤儿向量代表一个图谱里已不存在的故障,
等于给 RCA 喂**无法核实的先例** —— 与此前删掉的那个 Bedrock KB
(在 1 篇文档的语料上返回"相似度 89%"的编造先例)属同一类问题。

孤儿的来源是**写入与清理不对称**:`index_incident` 写向量,但在 2026-08-28
之前**没有对应的删除函数**;集成测试只 `DETACH DELETE` Neptune 节点,
向量留下来单调累积。已补 `delete_incident_vectors` 与
`conftest.cleanup_incident`,本测试是防止复发的守门。

副作用之一也在此暴露:那 10 条孤儿里有 5 条文本完全相同(同一个测试历次
运行留下的),嵌入相同 → 得分完全并列 0.7597。于是
`test_i06_vector_search_finds_incident` 用 `top_k=5` 断言时,6 条同分抢 5 个
位置,命中与否是抛硬币 —— 表现为该测试时好时坏。

## 断言方向刻意不对称

- **向量有、图谱无 → 硬失败**:这是会污染 RCA 推理的方向。
- **图谱有、向量无 → 只告警**:Incident 未被向量化只是检索召回少一条,
  不会让 RCA 得出错误结论;且补向量需要 Bedrock 调用,不该由测试强制。
"""
import os

import pytest

from paths import PROJECT_ROOT  # noqa: F401  (确保 conftest 的路径推导已生效)


def _vector_incident_ids() -> set:
    """列出索引中所有 incident_id。取不到时返回 None 以便 skip。"""
    try:
        from search.incident_vectordb import _get_client, BUCKET, INDEX
        client = _get_client()
        resp = client.list_vectors(vectorBucketName=BUCKET, indexName=INDEX)
    except Exception as e:  # 无凭证 / 无网络 / 桶不存在
        pytest.skip(f"S3 Vectors 不可用，跳过一致性校验: {e}")
    return {v['key'].split('.chunk-')[0] for v in resp.get('vectors', [])}


@pytest.mark.neptune
def test_v01_no_orphan_vectors(neptune_rca):
    """V-01: 索引里的每个 incident_id 都必须在图谱中有对应 Incident 节点。

    孤儿向量会被 search_similar 检索到并注入 RCA 提示词,
    让 RCA 引用一个图谱中不存在、无法核实的"历史先例"。
    """
    if not os.environ.get('NEPTUNE_ENDPOINT'):
        pytest.skip('未设置 NEPTUNE_ENDPOINT')

    vec_ids = _vector_incident_ids()
    if not vec_ids:
        pytest.skip('索引为空，无可校验内容')

    rows = neptune_rca.results(
        "MATCH (i:Incident) WHERE i.id IN $ids RETURN i.id AS id",
        {'ids': sorted(vec_ids)},
    )
    graph_ids = {r['id'] for r in rows}
    orphans = sorted(vec_ids - graph_ids)

    assert not orphans, (
        f"发现 {len(orphans)}/{len(vec_ids)} 个孤儿向量（图谱中无对应 Incident 节点）:\n"
        + "\n".join(f"  {o}" for o in orphans)
        + "\n\n这些向量会被 search_similar 检索并注入 RCA 提示词，"
          "使 RCA 引用无法核实的历史先例。\n"
          "清理方式: from search.incident_vectordb import delete_incident_vectors\n"
          "根因通常是写入/清理不对称 —— 删 Incident 节点时必须同时调用 "
          "delete_incident_vectors()（测试里用 conftest.cleanup_incident）。"
    )


@pytest.mark.neptune
def test_v02_recent_incidents_are_indexed(neptune_rca):
    """V-02: 图谱中较新的 Incident 应当已被向量化（缺失只告警,不失败）。

    未向量化只影响检索召回,不会让 RCA 得出错误结论;且补向量需要 Bedrock 调用,
    不该由测试强制。故这一方向只告警。
    """
    if not os.environ.get('NEPTUNE_ENDPOINT'):
        pytest.skip('未设置 NEPTUNE_ENDPOINT')

    vec_ids = _vector_incident_ids()
    rows = neptune_rca.results(
        "MATCH (i:Incident) RETURN i.id AS id ORDER BY i.id DESC LIMIT 20", {},
    )
    recent = [r['id'] for r in rows]
    missing = [i for i in recent if i not in vec_ids]
    if missing:
        import warnings
        warnings.warn(
            f"最近 {len(recent)} 个 Incident 中有 {len(missing)} 个未被向量化，"
            f"语义检索召回会偏低: {missing[:5]}",
            UserWarning,
        )
    # 只要不是全部缺失即可 —— 全缺通常说明向量化链路整体坏了
    assert not (recent and len(missing) == len(recent)), (
        f"最近 {len(recent)} 个 Incident **全部**未向量化，"
        f"疑似 index_incident 链路失效（检查 Bedrock 权限与 embed 调用）"
    )
